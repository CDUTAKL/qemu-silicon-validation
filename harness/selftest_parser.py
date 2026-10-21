#!/usr/bin/env python3
"""selftest_parser.py —— 帧解析层与统计函数的合成数据自测 (不依赖 QEMU/固件).

构造: 合法帧 (数据/告警/心跳/请求/ACK) + 坏 CRC 帧 + 未知类型帧 +
伪帧头错位 + 文本回显行 + 垃圾字节 + 序号跳变/重复, 一次性喂给
FrameParser 验证解析、分流与统计; 再用 FakeSerial 驱动 UartChannel
验证 send_command / wait_frame / collect_frames; 最后覆盖 asserts 模块
三个统计函数。

用法:  python harness/selftest_parser.py
退出码: 0 = 全部通过。
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import asserts
from harness.uart import (
    Frame,
    FrameParser,
    UartChannel,
    build_alert_frame,
    build_data_frame,
    build_frame,
    build_heartbeat_frame,
    crc16_ccitt,
)

RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# --------------------------------------------------------------------- #
# 1. CRC 单元测试
# --------------------------------------------------------------------- #
def test_crc() -> None:
    # CRC-16/CCITT-FALSE 标准测试向量 "123456789" -> 0x29B1
    report("crc.vector_123456789", crc16_ccitt(b"123456789") == 0x29B1,
           f"got {crc16_ccitt(b'123456789'):#06x}")
    # build_frame 自洽: 改任一帧字节 CRC 应失配
    fr = build_alert_frame(7, 3, 2, 1500)
    body = bytearray(fr)
    body[6] ^= 0xFF                            # 破坏 payload
    report("crc.tamper_detect", crc16_ccitt(bytes(body[:-2])) != (body[-2] << 8 | body[-1]))


# --------------------------------------------------------------------- #
# 2. FrameParser 合成流
# --------------------------------------------------------------------- #
def test_parser() -> None:
    p = FrameParser()
    stream = bytearray()

    # 文本命令 + 回显 (与二进制帧混在同一条流里) + 垃圾字节
    stream += b"\r\nnoise inject 3 2 1500\r\nOK\r\n" + bytes([0x00, 0x01, 0xFF])
    # 合法帧 seq 1..5 (含数据/告警/心跳/请求/ACK)
    stream += build_data_frame(1, 220.5, 1.25, 36.6)
    stream += build_alert_frame(2, fault_id=3, level=2, value=1500)
    stream += build_heartbeat_frame(3, task_bitmap=0x05, stack_hwm=372)
    stream += build_frame(4, 0x04)
    stream += build_frame(5, 0x05)
    # 坏 CRC 帧 (seq 6)
    bad = bytearray(build_alert_frame(6, 1, 1, 0))
    bad[-1] ^= 0xFF
    stream += bytes(bad)
    # 未知类型帧 (type 0xEE)
    unknown = bytes([0xAA, 0x55, 0x00, 0x07, 0xEE, 0xDE, 0xAD])
    stream += unknown
    # 伪帧头: AA 后跟非 55
    stream += b"\xAA\x00\xAA"
    # 序号跳变 seq 5 -> 8 (丢 2 帧), 再重复 8
    stream += build_heartbeat_frame(8, 0x07, 100)
    stream += build_heartbeat_frame(8, 0x07, 100)

    frames = p.feed(bytes(stream))
    st = p.seq_stats()

    report("parser.frame_count", len(frames) == 7, f"frames={len(frames)}")  # seq 1,2,3,4,5,8,8
    report("parser.types", [f.type for f in frames] == [1, 2, 3, 4, 5, 3, 3],
           f"types={[f.type for f in frames]}")
    report("parser.bad_frames", st["bad_frames"] == 2,
           f"bad={st['bad_frames']} (坏CRC+未知type)")
    report("parser.lost_frames", st["lost_frames"] == 2, f"lost={st['lost_frames']} (5->8)")
    report("parser.duplicate_frames", st["duplicate_frames"] == 1,
           f"dup={st['duplicate_frames']} (seq 8 x2)")
    report("parser.garbage", st["garbage_bytes"] >= 4,
           f"garbage={st['garbage_bytes']} (\\x00 \\x01 \\xff \\xAA-pseudo 等)")

    # 文本行提取
    lines = []
    while (line := p.pop_text_line()) is not None:
        lines.append(line)
    report("parser.text_lines", lines == ["noise inject 3 2 1500", "OK"],
           f"lines={lines!r}")

    # 分片喂入应与整包等价 (状态机无边界依赖)
    p2 = FrameParser()
    frames2: list[Frame] = []
    for i in range(0, len(stream), 3):
        frames2.extend(p2.feed(bytes(stream[i:i + 3])))
    report("parser.chunked_equivalent",
           [(f.seq, f.type) for f in frames2] == [(f.seq, f.type) for f in frames]
           and p2.seq_stats() == st,
           f"chunked={len(frames2)} frames")

    # values 解包
    d = frames[0]
    v, i_, t = d.values
    report("parser.data_values",
           abs(v - 220.5) < 1e-4 and abs(i_ - 1.25) < 1e-6 and abs(t - 36.6) < 1e-4,
           f"v={v} i={i_} t={t} (float32 精度)")
    a = frames[1]
    report("parser.alert_values", a.values == (3, 2, 1500), f"values={a.values}")
    h = frames[2]
    report("parser.heartbeat_values", h.values == (0x05, 372), f"values={h.values}")
    report("parser.empty_payload_frames", frames[3].values == () and frames[4].values == (),
           "REQUEST/ACK payload=()")


# --------------------------------------------------------------------- #
# 3. UartChannel (FakeSerial)
# --------------------------------------------------------------------- #
class FakeSerial:
    """按时间脚本吐字节的假串口 (duck-type pyserial 的 UartChannel 依赖面)."""

    def __init__(self, script: list[tuple[float, bytes]], rx: bytearray | None = None):
        self._script = script                     # [(t_offset, data)]
        self._cursor = 0
        self._rx = rx if rx is not None else bytearray()
        self.t0 = time.monotonic()
        self.writes: list[bytes] = []

    # pyserial 接口 -----------------------------------------------------
    def read(self, n: int) -> bytes:
        data = bytes(self._rx[:n])
        del self._rx[:n]
        return data

    @property
    def in_waiting(self) -> int:
        self.pump_script()                      # 时间到点的脚本字节释放入 RX
        return len(self._rx)

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        # 固件回显: 命令回显 + OK, 延迟 30ms 后出现在 RX
        # (按时间序插入脚本, 回显可能早于尚未到达的脚本帧)
        if data.endswith(b"\n"):
            echo = data + b"OK\r\n"
            due = time.monotonic() - self.t0 + 0.03
            import bisect
            offs = [t for t, _ in self._script]
            self._script.insert(bisect.bisect_right(offs, due), (due, echo))
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    # 脚本推进 ----------------------------------------------------------
    def pump_script(self) -> None:
        now = time.monotonic() - self.t0
        while self._cursor < len(self._script) and self._script[self._cursor][0] <= now:
            self._rx += self._script[self._cursor][1]
            self._cursor += 1


def test_uart_channel() -> None:
    # 帧按 50ms 间隔到达
    script = [(0.05 * (k + 1), build_heartbeat_frame(k + 1, 0x03, 200 + k))
              for k in range(5)]
    fake = FakeSerial(script)
    uart = UartChannel("/dev/fake")
    uart._ser = fake                                # noqa: SLF001 注入假串口
    uart.poll_interval = 0.005

    # send_command: 读到 OK
    status, echo = uart.send_command("inject 3 2 1500", timeout=2.0)
    report("uart.send_command", status == "OK" and echo == "inject 3 2 1500",
           f"status={status} echo={echo!r}")

    # wait_frame: 返回到达的第一帧 (seq1 于 +50ms 到达, send_command 未消耗帧)
    fr = uart.wait_frame(type=0x03, timeout=2.0)
    report("uart.wait_frame", fr.seq == 1 and fr.values == (0x03, 200),
           f"seq={fr.seq} values={fr.values}")

    # collect_frames: 限时收剩余帧 (seq 2..5 应该还在脚本里)
    got = uart.collect_frames(0.3)
    seqs = [f.seq for f in got]
    report("uart.collect_frames", seqs == [2, 3, 4, 5], f"seqs={seqs}")


# --------------------------------------------------------------------- #
# 4. asserts 统计
# --------------------------------------------------------------------- #
def test_asserts() -> None:
    frames = [
        Frame(seq=1, type=1, payload=b"", received_at=1.000),
        Frame(seq=2, type=1, payload=b"", received_at=1.100),
        Frame(seq=3, type=1, payload=b"", received_at=1.200),
        Frame(seq=6, type=1, payload=b"", received_at=1.300),   # 丢 4,5
        Frame(seq=6, type=1, payload=b"", received_at=1.400),   # 重复
    ]
    sc = asserts.seq_continuity(frames)
    report("asserts.seq_continuity",
           sc["lost_frames"] == 2 and sc["duplicate_frames"] == 1 and not sc["is_contiguous"],
           f"{sc}")

    # 告警响应: t=0/0.2/0.4 注入, 帧在 +30ms 到达 (fault_id 过滤)
    alerts = [
        Frame(seq=1, type=2, payload=struct.pack(">BBH", 3, 1, 10), received_at=0.030),
        Frame(seq=2, type=2, payload=struct.pack(">BBH", 3, 1, 20), received_at=0.230),
        Frame(seq=3, type=2, payload=struct.pack(">BBH", 9, 1, 30), received_at=0.900),  # 别的 fault
        Frame(seq=4, type=2, payload=struct.pack(">BBH", 3, 1, 40), received_at=0.430),
    ]
    samples = asserts.alert_response_times([0.0, 0.2, 0.4], alerts, fault_id=3)
    rt = asserts.response_time_stats(samples)
    report("asserts.alert_response_times", len(samples) == 3 and all(abs(s - 30.0) < 1e-6 for s in samples),
           f"samples_ms={[round(s, 2) for s in samples]}")
    report("asserts.response_time_stats",
           rt["n"] == 3 and abs(rt["mean"] - 30.0) < 1e-6 and rt["max"] == 30.0,
           f"{rt}")

    # 上报周期: 100ms 等间隔 + 一个 200ms 抖动
    ts = [0.0, 0.1, 0.2, 0.4, 0.5]
    pd = asserts.period_stats(ts)
    report("asserts.period_stats",
           pd["n"] == 4 and abs(pd["mean"] - 125.0) < 1e-6 and pd["max"] == 200.0
           and abs(pd["p50"] - 100.0) < 1e-6,
           f"mean={pd['mean']} std={pd['std']:.2f} p50={pd['p50']} p99={pd['p99']}")

    # 空输入鲁棒性
    e = asserts.response_time_stats([])
    z = asserts.period_stats([])
    report("asserts.empty_input", e["n"] == 0 and z["n"] == 0, "n=0, 无异常")


def main() -> int:
    print("== selftest_parser ==")
    test_crc()
    test_parser()
    test_uart_channel()
    test_asserts()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n== 结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ==")
    if failed:
        print("失败项:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
