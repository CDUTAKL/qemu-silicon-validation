"""tests/conftest.py — pytest 公共配置与共享辅助.

职责:
  1. 把项目根目录注入 sys.path (harness 是项目根下的包目录, 未安装为发行包);
  2. 设定默认被测固件 (QSV_KERNEL) 与启动横幅正则 (QSV_BOOT_BANNER),
     须在 import harness.fixture 之前完成 (该模块 import 时读取环境变量);
  3. 重新导出 harness.fixture 的 board / board_session fixture
     (conftest 命名空间内的 fixture 对本目录全体用例可见);
  4. 提供文本命令下发 / STAT? 解析 / ACK 帧 payload 兼容等共享辅助。

兼容 pytest-html: 不注册任何自定义 hook, 报告由插件直接采集。
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIRMWARE_ELF = PROJECT_ROOT / "firmware" / "build" / "firmware.elf"

os.environ.setdefault("QSV_KERNEL", str(FIRMWARE_ELF))
os.environ.setdefault("QSV_BOOT_BANNER", r"BOOT")

import pytest  # noqa: E402

from harness import asserts, uart  # noqa: E402,F401
from harness.fixture import board, board_session  # noqa: E402,F401  # fixture 注册
from harness.uart import (  # noqa: E402,F401
    TYPE_ACK,
    TYPE_ALERT,
    TYPE_DATA,
    TYPE_HEARTBEAT,
    TYPE_REQUEST,
    build_frame,
    crc16_ccitt,
)

# 启动横幅: 固件 main.c 打印 "BOOT v1.0.0 tasks=4 q:<len>"
BOOT_PATTERN = r"BOOT"

# STAT? 回显: "OK STAT tasks=N qdepth=D run=R hb=H seq=S"
STAT_PATTERN = r"OK STAT tasks=(\d+) qdepth=(\d+) run=(\d+) hb=(\d+) seq=(\d+)"
STAT_RE = re.compile(STAT_PATTERN)


def pytest_sessionstart(session: pytest.Session) -> None:
    """被测固件缺失时给出清晰错误, 而不是让 QEMU 启动失败报一串日志."""
    if not FIRMWARE_ELF.is_file():
        raise pytest.UsageError(
            f"被测固件不存在: {FIRMWARE_ELF} (请先构建 firmware, "
            f"或用 QSV_KERNEL 指向其他 ELF)"
        )


def uart_write(board, data: bytes) -> None:
    """直接向串口写字节 (文本命令行或二进制请求帧).

    harness 的 UartChannel.send_command 以裸 "OK"/"ERR" 行为应答结束标志,
    而固件应答均为 "OK <detail>" 形式 (如 "OK STAT ...", "OK FAULT OV"),
    故文本命令一律走 写字节 + wait_text 的组合。
    """
    ser = board.uart._ser  # noqa: SLF001  UartChannel 未暴露公共 write API
    assert ser is not None, "串口未打开"
    ser.write(data)
    ser.flush()


def send_text_command(board, cmd: str) -> None:
    """下发一行文本命令 (\\n 结尾), 不等待应答 (应答用 wait_text 取)。"""
    uart_write(board, cmd.encode("ascii") + b"\n")


def stat_query(board, timeout: float = 5.0) -> str:
    """发 STAT? 并等待 'OK STAT ...' 回显行, 返回整行文本."""
    send_text_command(board, "STAT?")
    return board.uart.wait_text(STAT_RE, timeout=timeout)


@contextmanager
def ack_payload_compat():
    """0x04/0x05 帧 payload 长度的固件/harness 口径差异兼容.

    固件 protocol/frame.h 规定 ACKREQ(0x04)/ACK(0x05) payload 均为 2B
    (u16 BE: 请求码 / 状态码); harness.uart.PAYLOAD_LEN 记为 0B。
    以 DUT (固件) 为准, 在本上下文内临时修正解析表, 退出后恢复,
    不影响其它用例的解析行为。
    """
    saved = dict(uart.PAYLOAD_LEN)
    uart.PAYLOAD_LEN[uart.TYPE_REQUEST] = 2
    uart.PAYLOAD_LEN[uart.TYPE_ACK] = 2
    try:
        yield
    finally:
        uart.PAYLOAD_LEN.clear()
        uart.PAYLOAD_LEN.update(saved)
