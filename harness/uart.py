"""串口帧解析层: 二进制帧状态机 + 文本回显混合流处理.

帧格式 (冻结规格)::

    | 0xAA | 0x55 | seq u16 BE | type u8 | payload | crc16 u16 BE |

    CRC = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, 无反射无 xorout),
    覆盖除 CRC 字段外的全部字节 (从 0xAA 到 payload 末尾)。

    type 0x01 数据帧:   payload 12B = 3 x float32 BE (v, i, t)
    type 0x02 告警帧:   payload  4B = fault_id u8 | level u8 | value u16 BE
    type 0x03 心跳帧:   payload  3B = 任务位图 u8 | 堆栈高水位 u16 BE
    type 0x04 请求帧:   payload  0B (测试端发出)
    type 0x05 ACK 帧:   payload  0B

同一字节流会混杂二进制帧与可打印文本回显: 解析器按字节分类 ——
0xAA 视为候选帧头进入定长收包; 可打印 ASCII / \\r \\n 收进文本行缓冲;
其余计入垃圾字节。坏 CRC / 未知 type 的候选帧计入坏帧并丢弃其帧头继续扫描。
"""

from __future__ import annotations

import re
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import serial  # pyserial
except ImportError as e:                     # 延迟到使用时报清晰错误
    serial = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

# --------------------------------------------------------------------- #
# 帧协议常量
# --------------------------------------------------------------------- #
SOF1 = 0xAA
SOF2 = 0x55

TYPE_DATA = 0x01
TYPE_ALERT = 0x02
TYPE_HEARTBEAT = 0x03
TYPE_REQUEST = 0x04
TYPE_ACK = 0x05

PAYLOAD_LEN: dict[int, int] = {
    TYPE_DATA: 12,       # 3 x float32 BE
    TYPE_ALERT: 4,       # u8 + u8 + u16 BE
    TYPE_HEARTBEAT: 3,   # u8 + u16 BE
    TYPE_REQUEST: 0,
    TYPE_ACK: 0,
}

HEADER_LEN = 5          # AA 55 seq_hi seq_lo type
CRC_LEN = 2


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, 无输入/输出反射, xorout 0."""
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build_frame(seq: int, ftype: int, payload: bytes = b"") -> bytes:
    """按冻结规格构造一帧 (测试注入 / selftest 用)。"""
    if ftype not in PAYLOAD_LEN:
        raise ValueError(f"未知帧类型: 0x{ftype:02X}")
    if len(payload) != PAYLOAD_LEN[ftype]:
        raise ValueError(
            f"类型 0x{ftype:02X} payload 长度须为 {PAYLOAD_LEN[ftype]}, 实际 {len(payload)}"
        )
    body = bytes([SOF1, SOF2, (seq >> 8) & 0xFF, seq & 0xFF, ftype]) + payload
    crc = crc16_ccitt(body)
    return body + struct.pack(">H", crc)


def build_data_frame(seq: int, v: float, i: float, t: float) -> bytes:
    return build_frame(seq, TYPE_DATA, struct.pack(">fff", v, i, t))


def build_alert_frame(seq: int, fault_id: int, level: int, value: int) -> bytes:
    return build_frame(seq, TYPE_ALERT, struct.pack(">BBH", fault_id, level, value))


def build_heartbeat_frame(seq: int, task_bitmap: int, stack_hwm: int) -> bytes:
    return build_frame(seq, TYPE_HEARTBEAT, struct.pack(">BH", task_bitmap, stack_hwm))


# --------------------------------------------------------------------- #
# 帧对象
# --------------------------------------------------------------------- #
@dataclass
class Frame:
    """一帧已校验通过的串口帧."""
    seq: int
    type: int
    payload: bytes
    received_at: float = field(default_factory=time.monotonic)

    @property
    def type_name(self) -> str:
        return _TYPE_NAMES.get(self.type, f"0x{self.type:02X}")

    @property
    def values(self) -> tuple[Any, ...]:
        """按类型解包 payload.

        数据帧 -> (v, i, t) float; 告警帧 -> (fault_id, level, value);
        心跳帧 -> (task_bitmap, stack_hwm); 请求/ACK -> ()。
        """
        if self.type == TYPE_DATA:
            return struct.unpack(">fff", self.payload)
        if self.type == TYPE_ALERT:
            return struct.unpack(">BBH", self.payload)
        if self.type == TYPE_HEARTBEAT:
            return struct.unpack(">BH", self.payload)
        return ()


_TYPE_NAMES = {
    TYPE_DATA: "DATA",
    TYPE_ALERT: "ALERT",
    TYPE_HEARTBEAT: "HEARTBEAT",
    TYPE_REQUEST: "REQUEST",
    TYPE_ACK: "ACK",
}


# --------------------------------------------------------------------- #
# 帧解析状态机
# --------------------------------------------------------------------- #
class FrameParser:
    """字节流 -> Frame 列表 + 文本行缓冲.

    用法::

        parser = FrameParser()
        frames = parser.feed(chunk)     # 累积喂入字节, 返回新完成的帧
        line = parser.pop_text_line()   # 取一条文本回显 (无则 None)
        stats = parser.seq_stats()      # 序号连续性统计

    统计口径: 坏帧 = CRC 错或未知 type 的候选帧; 丢帧 = 序号缺口;
    重复帧 = 与上一帧序号相同。均为只读属性。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._text_acc = bytearray()        # 文本行累积器
        self.text_lines: list[str] = []     # 已完成文本行 (FIFO)
        # 统计
        self.frames_parsed = 0
        self.bad_frames = 0                 # CRC 错 / 未知 type
        self.lost_frames = 0                # 序号缺口累计
        self.duplicate_frames = 0           # 重复序号
        self.garbage_bytes = 0              # 既非帧头也不可打印的字节
        self._last_seq: int | None = None

    # ------------------------------------------------------------------ #
    def feed(self, data: bytes) -> list[Frame]:
        """喂入一段字节流, 返回本次新解析出的完整帧."""
        self._buf += data
        out: list[Frame] = []
        while self._drain_one(out):
            pass
        return out

    def _drain_one(self, out: list[Frame]) -> bool:
        """从缓冲头部处理一个字节/一帧; 返回是否取得了进展."""
        buf = self._buf
        if not buf:
            return False

        # --- 候选帧头: AA 55 -------------------------------------------------
        if buf[0] == SOF1:
            if len(buf) >= 2 and buf[1] != SOF2:
                # 伪帧头: AA 后不是 55, 该 AA 按普通字节处理
                self._consume_text_byte(buf[0])
                del buf[0]
                return True
            if len(buf) < 2:
                return False                       # 等第二个字节定夺
            # 已有 AA 55: 需要 type 字节才能定长
            if len(buf) < HEADER_LEN:
                return False
            ftype = buf[4]
            plen = PAYLOAD_LEN.get(ftype)
            if plen is None:
                # 未知 type: 丢弃这个伪帧头 (AA), 55 及后续重新扫描
                self.bad_frames += 1
                del buf[0]
                return True
            total = HEADER_LEN + plen + CRC_LEN
            if len(buf) < total:
                return False                       # 定长收包中
            candidate = bytes(buf[:total])
            crc_expect = (candidate[-2] << 8) | candidate[-1]
            if crc16_ccitt(candidate[:-CRC_LEN]) == crc_expect:
                seq = (candidate[2] << 8) | candidate[3]
                out.append(Frame(seq=seq, type=ftype,
                                 payload=candidate[HEADER_LEN:HEADER_LEN + plen]))
                self.frames_parsed += 1
                self._check_seq(seq)
            else:
                self.bad_frames += 1               # 坏 CRC: 丢帧头重扫
            del buf[:total]
            return True

        # --- 普通字节: 可打印 -> 文本; 否则垃圾 ------------------------------
        self._consume_text_byte(buf[0])
        del buf[0]
        return True

    def _consume_text_byte(self, b: int) -> None:
        if b in (0x0D, 0x0A):                      # \r \n: 行结束
            line = self._text_acc.decode("ascii", errors="replace").rstrip("\r")
            self._text_acc.clear()
            if line:
                self.text_lines.append(line)
        elif 0x20 <= b <= 0x7E or b == 0x09:       # 可打印 / tab
            self._text_acc.append(b)
        else:
            self.garbage_bytes += 1
            self._text_acc.clear()                 # 打断当前文本行

    # ------------------------------------------------------------------ #
    def _check_seq(self, seq: int) -> None:
        if self._last_seq is not None:
            gap = (seq - self._last_seq) & 0xFFFF
            if gap == 0:
                self.duplicate_frames += 1
            elif gap > 1:
                self.lost_frames += gap - 1
        self._last_seq = seq

    def pop_text_line(self) -> str | None:
        """取最早完成的文本行; 无则 None."""
        if self.text_lines:
            return self.text_lines.pop(0)
        return None

    def clear_stats(self) -> None:
        """重置全部统计 (帧/坏帧/丢帧/重复/垃圾字节/序号基准)。"""
        self.frames_parsed = self.bad_frames = self.lost_frames = 0
        self.duplicate_frames = self.garbage_bytes = 0
        self._last_seq = None

    def seq_stats(self) -> dict[str, int]:
        return {
            "frames_parsed": self.frames_parsed,
            "bad_frames": self.bad_frames,
            "lost_frames": self.lost_frames,
            "duplicate_frames": self.duplicate_frames,
            "garbage_bytes": self.garbage_bytes,
        }


# --------------------------------------------------------------------- #
# 串口通道
# --------------------------------------------------------------------- #
class UartTimeoutError(TimeoutError):
    """wait_frame / send_command 等待超时。"""


class UartChannel:
    """pyserial 串口通道: 文本命令 + 二进制帧收发.

    用法::

        uart = UartChannel(board.pty_path)
        uart.open()
        status, echo = uart.send_command("inject 3 2 1500")
        frame = uart.wait_frame(type=TYPE_ALERT, timeout=5)
        frames = uart.collect_frames(duration=2.0)
        uart.close()

    同字节流的文本行与帧由 FrameParser 自动分流。
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        *,
        poll_interval: float = 0.02,
        read_chunk: int = 256,
    ) -> None:
        if serial is None:
            raise ImportError(
                f"UartChannel 需要 pyserial: {_IMPORT_ERROR}"
            )
        self.port = port
        self.baudrate = baudrate
        self.poll_interval = poll_interval
        self.read_chunk = read_chunk
        self.parser = FrameParser()
        self._ser: Any = None                 # serial.Serial
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._ser is None:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=0)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> "UartChannel":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def _pump(self) -> list[Frame]:
        """读空当前可读字节并喂解析器, 返回新帧."""
        assert self._ser is not None, "串口未打开"
        frames: list[Frame] = []
        while True:
            n = getattr(self._ser, "in_waiting", 0) or 0
            if n <= 0:
                break
            data = self._ser.read(min(n, self.read_chunk))
            if not data:
                break
            frames.extend(self.parser.feed(bytes(data)))
        return frames

    def send_command(self, cmd: str, *, timeout: float = 5.0) -> tuple[str, str | None]:
        """发送文本命令行 (\\n 结尾), 读到 OK / ERR 行为止.

        Returns:
            (status, echo_text): status ∈ {"OK", "ERR", "TIMEOUT"};
            echo_text 为命令行回显 (无则 None)。
        """
        with self._lock:
            self.open()
            assert self._ser is not None
            self._ser.write(cmd.encode("ascii") + b"\n")
            self._ser.flush()
            deadline = time.monotonic() + timeout
            echo: str | None = None
            while time.monotonic() < deadline:
                self._pump()
                line = self.parser.pop_text_line()
                if line is not None:
                    if line.strip() == cmd.strip():
                        echo = line            # 命令回显
                    elif line.strip() in ("OK", "ERR"):
                        return line.strip(), echo
                    # 其余文本行忽略 (调试输出)
                time.sleep(self.poll_interval)
            return "TIMEOUT", echo

    def wait_frame(
        self, type: int | None = None, *, timeout: float = 5.0,
    ) -> Frame:
        """等待满足条件 (默认任意) 的帧, 超时抛 UartTimeoutError."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = self._pump()
            for fr in frames:
                if type is None or fr.type == type:
                    return fr
            time.sleep(self.poll_interval)
        raise UartTimeoutError(f"等待帧超时 ({timeout}s, type={type})")

    def wait_text(self, pattern: str | "re.Pattern[str]", *, timeout: float = 5.0) -> str:
        """等待匹配正则的文本行出现, 返回该行; 超时抛 UartTimeoutError."""
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._pump()
            line = self.parser.pop_text_line()
            if line is not None and rx.search(line):
                return line
            time.sleep(self.poll_interval)
        raise UartTimeoutError(f"等待文本 /{rx.pattern}/ 超时 ({timeout}s)")

    def collect_frames(self, duration: float, *, type: int | None = None) -> list[Frame]:
        """限时收帧: 持续 poll 指定时长, 返回收集到的帧列表."""
        deadline = time.monotonic() + duration
        out: list[Frame] = []
        while time.monotonic() < deadline:
            for fr in self._pump():
                if type is None or fr.type == type:
                    out.append(fr)
            time.sleep(self.poll_interval)
        return out
