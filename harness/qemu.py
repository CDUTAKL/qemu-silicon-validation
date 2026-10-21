"""QEMU 进程管理: 拉起/监控/清理 stm32vldiscovery 仿真实例.

实测依据 (QEMU 11.0.3, homebrew/macOS):
  * `-serial pty` 的 pty 分配信息由 QEMU 打印为
        char device redirected to /dev/ttys000 (label serial0)
    打到 **stdout**; 某些构建/平台会打到 stderr, 因此本实现同时
    轮询 stdout/stderr 两个日志文件, 用正则兼容 /dev/pts/N 与 /dev/ttysN。
  * 若 guest ELF 异常 (如向量表缺少 thumb 位), QEMU 会打印
    ``qemu: fatal: Lockup ...`` 到 stderr 后 **直接 abort**,
    start() 必须检测进程提前退出并抛出带日志的错误。
"""

from __future__ import annotations

import atexit
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import IO

def _find_qemu() -> str:
    """跨平台定位 qemu-system-arm：环境变量 > PATH > macOS Homebrew 默认路径。"""
    env = os.environ.get("QSV_QEMU_BIN")
    if env:
        return env
    from shutil import which
    found = which("qemu-system-arm")
    if found:
        return found
    return "/opt/homebrew/bin/qemu-system-arm"


DEFAULT_QEMU_BIN = _find_qemu()
DEFAULT_MACHINE = "stm32vldiscovery"

# QEMU chardev 重定向通告: 兼容 stdout/stderr、Linux pts 与 macOS ttys
_PTY_RE = re.compile(r"char device redirected to (/dev/\S+)")
# QEMU 致命错误前缀 (Lockup / panic 等)
_FATAL_RE = re.compile(r"qemu: fatal:")

# 存活实例登记表: atexit 兜底清理, 防止僵尸 QEMU 进程
_LIVE_BOARDS: set["QemuBoard"] = set()
_LIVE_LOCK = threading.Lock()


class QemuError(RuntimeError):
    """QEMU 进程相关错误的基类。"""


class QemuStartError(QemuError):
    """QEMU 启动失败 (进程提前退出 / 超时未就绪)。"""


class QemuBoard:
    """封装一个 ``qemu-system-arm -M stm32vldiscovery`` 进程。

    用法::

        board = QemuBoard(kernel="firmware.elf", qmp_socket="/tmp/qmp.sock")
        board.start()
        print(board.pty_path)          # e.g. /dev/ttys003
        ...                            # 跑测试, 用 qmp.py / uart.py 交互
        board.stop()

    也可作为上下文管理器使用。所有已启动实例在解释器退出时经
    atexit 强制清理 (SIGTERM -> 等待 -> SIGKILL)。
    """

    def __init__(
        self,
        kernel: str | Path,
        qmp_socket: str | Path,
        *,
        snapshot: bool = False,
        plugin: str | Path | None = None,
        plugin_args: list[str] | None = None,
        debug_flags: str | None = "guest_errors",
        qemu_bin: str = DEFAULT_QEMU_BIN,
        machine: str = DEFAULT_MACHINE,
        log_dir: str | Path | None = None,
        start_timeout: float = 15.0,
        extra_args: list[str] | None = None,
    ) -> None:
        self.kernel = str(kernel)
        self.qmp_socket = str(qmp_socket)
        self.snapshot = snapshot
        self.plugin = str(plugin) if plugin else None
        self.plugin_args = list(plugin_args or [])
        self.debug_flags = debug_flags
        self.qemu_bin = qemu_bin
        self.machine = machine
        self.start_timeout = start_timeout
        self.extra_args = list(extra_args or [])

        if log_dir is None:
            log_dir = Path(tempfile.mkdtemp(prefix="qsv-qemu-log-"))
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._proc: subprocess.Popen[bytes] | None = None
        self._stdout_f: IO[bytes] | None = None
        self._stderr_f: IO[bytes] | None = None
        self.pty_path: str | None = None

    # ------------------------------------------------------------------ #
    # 命令行组装
    # ------------------------------------------------------------------ #
    def build_cmd(self) -> list[str]:
        """组装完整 qemu 命令行 (列表形式, 便于打印与测试)。"""
        cmd = [
            self.qemu_bin,
            "-M", self.machine,
            "-kernel", self.kernel,
            "-serial", "pty",                       # 分配 pty, 路径见日志
            "-qmp", f"unix:{self.qmp_socket},server=on,wait=off",
            "-nographic",
        ]
        if self.snapshot:
            cmd.append("-snapshot")
        if self.plugin:
            # TCG 插件: -plugin <file> [,arg=val...]; 参数原样追加
            cmd += ["-plugin", self.plugin, *self.plugin_args]
        if self.debug_flags:
            cmd += ["-d", self.debug_flags]
        cmd += self.extra_args
        return cmd

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> str:
        """启动 QEMU 进程并等待 pty 路径与 QMP socket 就绪。

        Returns:
            分配到的 pty 从设备路径 (如 ``/dev/ttys003``)。

        Raises:
            QemuStartError: 进程提前退出 (fatal/lockup) 或等待超时。
        """
        if self._proc is not None:
            raise QemuError("QemuBoard.start() 重复调用; 请先 stop()")

        # 清理旧 socket 文件, 避免 QEMU bind 失败
        if os.path.exists(self.qmp_socket):
            os.unlink(self.qmp_socket)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stdout_f = open(self.log_dir / "qemu_stdout.log", "wb")
        self._stderr_f = open(self.log_dir / "qemu_stderr.log", "wb")

        cmd = self.build_cmd()
        self._proc = subprocess.Popen(
            cmd,
            stdout=self._stdout_f,
            stderr=self._stderr_f,
            stdin=subprocess.DEVNULL,
            start_new_session=True,     # 脱离进程组, 避免测试进程信号波及
        )
        with _LIVE_LOCK:
            _LIVE_BOARDS.add(self)

        try:
            self.pty_path = self._await_pty(self.start_timeout)
            self._await_qmp_socket(self.start_timeout)
        except QemuStartError:
            self.stop(force=True)
            raise
        return self.pty_path

    def _await_pty(self, timeout: float) -> str:
        """轮询 stdout/stderr 日志, 提取 ``char device redirected to ...``。"""
        deadline = time.monotonic() + timeout
        stdout_log = self.log_dir / "qemu_stdout.log"
        stderr_log = self.log_dir / "qemu_stderr.log"
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise QemuStartError(
                    f"QEMU 进程提前退出 (rc={self._proc.returncode}): "
                    f"{self._tail(stderr_log)}"
                )
            for log in (stdout_log, stderr_log):
                m = _PTY_RE.search(self._read_all(log))
                if m:
                    return m.group(1)
            time.sleep(0.05)
        raise QemuStartError(
            f"等待 pty 路径超时 ({timeout}s); 日志尾部: {self._tail(stderr_log)}"
        )

    def _await_qmp_socket(self, timeout: float) -> None:
        """等待 QMP unix socket 文件出现。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self.qmp_socket):
                return
            if self._proc is not None and self._proc.poll() is not None:
                raise QemuStartError("QEMU 进程在 QMP socket 就绪前退出")
            time.sleep(0.05)
        raise QemuStartError(f"等待 QMP socket 超时: {self.qmp_socket}")

    @staticmethod
    def _read_all(path: Path) -> str:
        try:
            return path.read_text(errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _tail(path: Path, nbytes: int = 2048) -> str:
        try:
            data = path.read_bytes()[-nbytes:]
        except OSError:
            return "<no log>"
        text = data.decode(errors="replace").strip()
        return text if text else "<empty>"

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def stop(self, *, force: bool = False, grace: float = 3.0) -> None:
        """停止 QEMU 进程: 先 SIGTERM, 宽限期内不退则 SIGKILL。

        force=True 时直接 SIGKILL (用于异常路径清理)。
        幂等: 重复调用安全。
        """
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            if not force:
                proc.terminate()            # SIGTERM
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        with _LIVE_LOCK:
            _LIVE_BOARDS.discard(self)
        for f in (self._stdout_f, self._stderr_f):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        self._stdout_f = self._stderr_f = None
        self._proc = None

    # 上下文管理器
    def __enter__(self) -> "QemuBoard":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __del__(self) -> None:                      # 兜底 (不依赖用户调 stop)
        try:
            self.stop(force=True)
        except Exception:
            pass


def _cleanup_all_boards() -> None:
    """atexit 钩子: 强制清理所有存活 QEMU 实例。"""
    with _LIVE_LOCK:
        boards = list(_LIVE_BOARDS)
    for board in boards:
        try:
            board.stop(force=True)
        except Exception:
            pass


atexit.register(_cleanup_all_boards)
