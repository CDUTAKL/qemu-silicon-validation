"""命令级故障注入封装 (薄层): 经串口文本命令触发固件内建故障路径.

固件命令语法 (firmware/Core/diag_task.c, 大小写敏感, \\n 结尾)::

    FAULT OV <v>      -> OK FAULT OV      过压: 先发告警帧 fault_id=1 level=2
    FAULT OC <i>      -> OK FAULT OC      过流: 先发告警帧 fault_id=2 level=2
    FAULT DISCONN     -> OK FAULT DISCONN 断开: 暂停数据帧上报 (心跳继续)
    FAULT CONN        -> OK FAULT CONN    重连: 恢复数据帧上报
    FAULT HANG <task> -> OK FAULT HANG    挂起任务 (sensor|comm|diag|watchdog)
    RESUME <task>     -> OK RESUME        恢复任务
    STAT?             -> OK STAT tasks=N qdepth=D run=R hb=H seq=S

注意: 固件应答是 ``OK FAULT OV`` 这类**带后缀的文本行**, 而非裸 ``OK``,
因此本模块不走 ``UartChannel.send_command`` (它只认裸 OK/ERR 会超时),
而是直接写串口 + ``wait_text`` 匹配应答行 —— 与 firmware/manual_check.py
的实测口径一致。

所有方法返回固件应答行文本 (如 ``"OK FAULT OV"``); strict=True (默认) 时
应答为 ERR 或超时即抛 :class:`FwFaultError`。

用法::

    fi = FwFaultInjector(uart)              # uart: harness.uart.UartChannel
    fi.inject_overvoltage(4.2)              # -> "OK FAULT OV"
    frame = uart.wait_frame(type=TYPE_ALERT)
    stats = fi.stat()                       # -> {"tasks": 4, "seq": 123, ...}
"""

from __future__ import annotations

import re
from typing import Any

VALID_TASKS = ("sensor", "comm", "diag", "watchdog")

_STAT_RE = re.compile(
    r"^OK STAT tasks=(?P<tasks>\d+) qdepth=(?P<qdepth>\d+)"
    r" run=(?P<run>\d+) hb=(?P<hb>\d+) seq=(?P<seq>\d+)$"
)


class FwFaultError(RuntimeError):
    """固件拒绝注入命令 (ERR) 或应答超时。"""


class FwFaultInjector:
    """命令级故障注入器: 每个注入动作对应一条固件文本命令。

    Args:
        uart: 已打开的 ``harness.uart.UartChannel``。
        timeout: 单条命令应答超时 (秒)。
        strict: True (默认) 时 ERR/超时应答抛 :class:`FwFaultError`;
            False 时原样返回应答文本, 由调用方判读。
    """

    def __init__(self, uart: Any, *, timeout: float = 5.0, strict: bool = True) -> None:
        self.uart = uart
        self.timeout = timeout
        self.strict = strict
        self.command_count = 0          # 已发命令总数 (成功+失败)
        # 命令应答等待期间顺带泵到的帧 (固件常把告警帧排在 OK 应答之前,
        # 不存下来就会被应答等待的泵吞掉, 调用方再 wait_frame 必丢)
        self.stashed_frames: list[Any] = []

    # ------------------------------------------------------------------ #
    def _exec(self, cmd: str) -> str:
        """发送命令, 等待 ``OK ...``/``ERR ...`` 应答行, 返回该行文本。

        写侧保护: guest 被打挂 (如比特级注入) 停止消费 UART 后, pty 写
        缓冲满会让 pyserial 永久阻塞 —— 尤其 ``flush()`` 底层 tcdrain
        不吃 write_timeout, 故本层绝不调 flush (pty 的 write 本身直达
        内核, 无需刷); write 则经 write_timeout 兜底。
        """
        import time as _time

        ser = self.uart._ser                     # noqa: SLF001  同生态内复用
        assert ser is not None, "串口未打开"
        if ser.write_timeout is None:
            ser.write_timeout = self.timeout
        try:
            ser.write(cmd.encode("ascii") + b"\n")
        except Exception as e:                   # SerialTimeoutException/OSError
            self.command_count += 1
            if self.strict:
                raise FwFaultError(f"命令 {cmd!r} 串口写失败: {e}") from e
            return f"TIMEOUT ({cmd})"
        self.command_count += 1
        deadline = _time.monotonic() + self.timeout
        while _time.monotonic() < deadline:
            self.stashed_frames.extend(self.uart._pump())   # noqa: SLF001
            line = self.uart.parser.pop_text_line()
            if line is not None and re.match(r"^(OK|ERR)\b", line):
                if line.startswith("ERR") and self.strict:
                    raise FwFaultError(f"命令 {cmd!r} 被固件拒绝: {line!r}")
                return line
            _time.sleep(self.uart.poll_interval)
        if self.strict:
            raise FwFaultError(f"命令 {cmd!r} 应答超时 ({self.timeout}s)")
        return f"TIMEOUT ({cmd})"

    @staticmethod
    def _check_task(task: str) -> None:
        if task not in VALID_TASKS:
            raise ValueError(f"未知任务 {task!r}, 合法值: {VALID_TASKS}")

    # ------------------------------------------------------------------ #
    # 故障注入原语
    # ------------------------------------------------------------------ #
    def inject_overvoltage(self, v: float) -> str:
        """过压注入: 强制下一数据帧电压为 ``v``, 并先发出告警帧 (fault_id=1)。"""
        return self._exec(f"FAULT OV {float(v):g}")

    def inject_overcurrent(self, i: float) -> str:
        """过流注入: 强制下一数据帧电流为 ``i``, 并先发出告警帧 (fault_id=2)。"""
        return self._exec(f"FAULT OC {float(i):g}")

    def disconnect(self) -> str:
        """断开注入: 暂停数据帧上报 (心跳帧不受影响)。"""
        return self._exec("FAULT DISCONN")

    def reconnect(self) -> str:
        """重连: 恢复数据帧上报。"""
        return self._exec("FAULT CONN")

    def hang(self, task: str) -> str:
        """挂起指定 RTOS 任务 (vTaskSuspend)。"""
        self._check_task(task)
        return self._exec(f"FAULT HANG {task}")

    def resume(self, task: str) -> str:
        """恢复被挂起的任务 (vTaskResume)。"""
        self._check_task(task)
        return self._exec(f"RESUME {task}")

    # ------------------------------------------------------------------ #
    # 状态查询
    # ------------------------------------------------------------------ #
    def stat(self) -> dict[str, int]:
        """查询固件运行状态.

        Returns:
            dict: ``{"tasks": 存活任务数, "qdepth": TX 队列深度,
            "run": 运行计数, "hb": 心跳计数, "seq": 当前帧序号}``。

        Raises:
            FwFaultError: 应答超时/ERR 或应答格式不符。
        """
        line = self._exec("STAT?")
        if line.startswith(("ERR", "TIMEOUT")):
            raise FwFaultError(f"STAT? 应答异常: {line!r}")
        m = _STAT_RE.match(line)
        if not m:
            raise FwFaultError(f"STAT? 应答格式不符: {line!r}")
        return {k: int(v) for k, v in m.groupdict().items()}
