"""pytest fixture: 一键拉起 QEMU 板卡 + QMP + 串口.

提供 ``board`` (function 级, 默认) 与 ``board_session`` (session 级) 两个
fixture, 进入时拉起 QEMU 进程、建立 QMP 连接、打开串口; 退出时关闭串口、
QMP quit 关闭 QEMU (异常时强制 stop)。

配置经环境变量 (也可用 ``QSV_*`` 常量直接改):

    QSV_QEMU_BIN   QEMU 可执行文件 (默认 /opt/homebrew/bin/qemu-system-arm)
    QSV_KERNEL     被测固件 ELF (默认 harness/assets/idle.elf, 自测用空转固件)
    QSV_BOOT_BANNER  复位后期望的启动横幅正则 (默认 None = 等串口静默)

用法::

    def test_alert(board):
        board.reset()
        status, _ = board.uart.send_command("inject 3 2 1500")
        assert status == "OK"
        frame = board.uart.wait_frame(type=TYPE_ALERT, timeout=5)
        assert frame.values[0] == 3

实现注意: QmpClient 是 asyncio 的, 而 pytest 测试是同步风格, 因此 Board
持有一个专属 event loop, 所有 QMP 操作经 ``_run()`` 提交到该 loop,
避免多次 ``asyncio.run()`` 创建新 loop 导致后台 rx 任务失活的问题。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Coroutine

import pytest

from .qemu import QemuBoard
from .qmp import QmpClient
from .uart import UartChannel

_HERE = Path(__file__).resolve().parent
DEFAULT_KERNEL = os.environ.get("QSV_KERNEL", str(_HERE / "assets" / "idle.elf"))
DEFAULT_BANNER = os.environ.get("QSV_BOOT_BANNER") or None

RESET_SETTLE_TIME = 0.3      # 无横幅模式下判定"启动完成"的串口静默时长
RESET_TIMEOUT = 15.0


class Board:
    """聚合 QemuBoard + QmpClient + UartChannel 的测试板卡句柄."""

    def __init__(
        self,
        qemu: QemuBoard,
        qmp: QmpClient,
        uart: UartChannel,
        *,
        boot_banner: str | None = DEFAULT_BANNER,
    ) -> None:
        self.qemu = qemu
        self.qmp = qmp
        self.uart = uart
        self.boot_banner = boot_banner
        # 专属事件循环: QMP 的 rx 后台任务存活期与 Board 一致,
        # 同步 API 经 _run() 提交, 避免 asyncio.run() 反复建环杀死后台任务
        self._loop = asyncio.new_event_loop()

    # ------------------------------------------------------------------ #
    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """在专属事件循环上跑一个协程 (同步阻塞)。"""
        return self._loop.run_until_complete(coro)

    # ------------------------------------------------------------------ #
    def reset(self, *, timeout: float = RESET_TIMEOUT) -> dict[str, Any]:
        """复位板卡并等待重新启动完成.

        流程: QMP system_reset (自动 resume) -> 等待启动完成标志:
        若配置了启动横幅正则则等横幅文本行; 否则等串口静默
        RESET_SETTLE_TIME 秒。超时抛 UartTimeoutError / QMPTimeoutError。

        Returns:
            dict: {"banner": 匹配到的横幅行或 None, "elapsed": 耗时秒}
        """
        t0 = time.monotonic()
        self._run(self.qmp.system_reset(resume=True))
        if self.boot_banner:
            line = self.uart.wait_text(self.boot_banner, timeout=timeout)
            return {"banner": line, "elapsed": time.monotonic() - t0}
        # 无横幅: 等串口静默
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            frames = self.uart._pump()          # noqa: SLF001  同包内复用
            if frames:
                last_data = time.monotonic()
            elif time.monotonic() - last_data >= RESET_SETTLE_TIME:
                break
            time.sleep(self.uart.poll_interval)
        return {"banner": None, "elapsed": time.monotonic() - t0}

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """关闭 QMP 与串口 (QEMU 经 QMP quit 退出; 兜底强制 stop)。"""
        try:
            self._run(self.qmp.quit())
        except Exception:
            pass
        try:
            self._run(self.qmp.close())
        except Exception:
            pass
        try:
            self.uart.close()
        except Exception:
            pass
        self.qemu.stop(force=True)
        self._loop.close()


# --------------------------------------------------------------------- #
# fixture 工厂
# --------------------------------------------------------------------- #
def _make_board() -> Board:
    workdir = Path(tempfile.mkdtemp(prefix="qsv-board-"))
    qmp_sock = str(workdir / "qmp.sock")
    qemu = QemuBoard(DEFAULT_KERNEL, qmp_sock, log_dir=workdir / "logs")
    pty = qemu.start()

    uart = UartChannel(pty)
    uart.open()

    board = Board(qemu, QmpClient(qmp_sock), uart)
    board._run(board.qmp.connect())             # 在专属 loop 上握手
    return board


@pytest.fixture()
def board() -> Any:
    """function 级板卡: 每个测试一台全新 QEMU (隔离性最好, 默认)。"""
    b = _make_board()
    try:
        yield b
    finally:
        b.close()


@pytest.fixture(scope="session")
def board_session() -> Any:
    """session 级板卡: 全部测试共享一台 QEMU (快, 测试间须自行 reset)。"""
    b = _make_board()
    try:
        yield b
    finally:
        b.close()
