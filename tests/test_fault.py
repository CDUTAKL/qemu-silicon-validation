"""故障注入用例 TC-FLT-001~010 (FR-06/FR-07/FR-08, NFR-04/NFR-05).

冻结规格要点 (本文件断言依据, 已与固件 firmware/Core/diag_task.c 对齐):
    FAULT OV <v>   -> 回显 "OK FAULT OV", 告警帧 fault_id=1 level=2,
                      value=(uint16_t)(v+0.5) (u16 四舍五入), 且下一采样点
                      电压被一次性强制为 v
    FAULT OC <i>   -> 同上, fault_id=2, 强制电流
    FAULT DISCONN  -> 停发数据帧 (心跳帧继续, 序号由 CommTask 连续打号)
    FAULT CONN     -> 恢复数据帧
    FAULT HANG <t> -> vTaskSuspend(sensor|comm|diag|watchdog)
    RESUME <t>     -> vTaskResume
    非法 FAULT     -> 回显 "ERR USAGE" / "ERR UNKNOWN CMD", 系统不死

注意: 固件回显是 "OK xxx"/"ERR xxx" 整行, UartChannel.send_command 只识别
裸 OK/ERR 会等超时, 因此本文件一律直写串口 + 自行等回显行 (与
conftest.send_text_command / test_recovery.py 同一手法)。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# --- 路径与环境兜底 (conftest.py 由另一代理提供; setdefault 幂等) --------- #
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QSV_KERNEL", str(ROOT / "firmware" / "build" / "firmware.elf"))
os.environ.setdefault("QSV_BOOT_BANNER", r"BOOT")

from harness import asserts  # noqa: E402
from harness.fixture import board  # noqa: E402,F401  (注册 fixture)
from harness.uart import (  # noqa: E402
    TYPE_ALERT,
    TYPE_DATA,
    TYPE_HEARTBEAT,
    UartTimeoutError,
)

REPLY_RE = re.compile(r"^(OK|ERR)\b")
STAT_RE = re.compile(
    r"^OK STAT tasks=(\d+) qdepth=(\d+) run=(\d+) hb=(\d+) seq=(\d+)"
)

ALERT_STATS_PATH = ROOT / "report" / "alert_stats.json"


# --------------------------------------------------------------------- #
# 工具 (与 test_recovery.py 保持同一套, 故意各自内联, 不新增共享文件)
# --------------------------------------------------------------------- #
def _pump(board, sink: list | None = None):
    """读空串口并喂解析器; sink 非空时把新帧追加进去 (跨阶段连续性跟踪)。"""
    frames = board.uart._pump()  # noqa: SLF001  harness 内既有用法
    if sink is not None:
        sink.extend(frames)
    return frames


def _drain(board) -> None:
    """丢弃积压的帧与文本行, 避免匹配到上一阶段的陈旧回显。"""
    _pump(board)
    while board.uart.parser.pop_text_line() is not None:
        pass


def _send_line(board, cmd: str) -> float:
    """直写一行文本命令 (\\n 结尾), 返回发出时刻 (time.monotonic)。"""
    ser = board.uart._ser  # noqa: SLF001
    assert ser is not None, "串口未打开"
    ser.write(cmd.encode("ascii") + b"\n")
    ser.flush()
    return time.monotonic()


def _wait_reply(board, timeout: float = 3.0, sink: list | None = None) -> str:
    """等待 OK/ERR 开头的回显行; 期间收到的帧进 sink。"""
    uart = board.uart
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _pump(board, sink)
        while True:
            line = uart.parser.pop_text_line()
            if line is None:
                break
            if REPLY_RE.match(line):
                return line
        time.sleep(uart.poll_interval)
    raise UartTimeoutError(f"等待命令回显超时 ({timeout}s)")


def _cmd(board, cmd: str, timeout: float = 3.0, sink: list | None = None):
    """发命令并等回显, 返回 (回显行, 发出时刻)。"""
    t_sent = _send_line(board, cmd)
    return _wait_reply(board, timeout, sink), t_sent


def _stat(board, timeout: float = 3.0) -> dict:
    """STAT? -> 解析为 dict(tasks/qdepth/run/hb/seq)。"""
    line, _ = _cmd(board, "STAT?", timeout=timeout)
    m = STAT_RE.match(line)
    assert m, f"STAT? 回显格式异常: {line!r}"
    return {
        "tasks": int(m.group(1)),
        "qdepth": int(m.group(2)),
        "run": int(m.group(3)),
        "hb": int(m.group(4)),
        "seq": int(m.group(5)),
    }


def _wait_frame(board, ftype: int | None, timeout: float = 3.0, sink=None):
    """等待一帧指定类型 (None=任意); 期间其他帧也进 sink。

    先扫描 sink 中已收集的帧 —— 目标帧可能在此前 _cmd 等回显期间就已
    随同一批字节进 sink (harness wait_text/wait_frame 同批互吞问题的
    另一面, 见 test_recovery.py 模块 docstring), 只看新 pump 的帧会漏。
    """
    if sink is not None:
        for fr in sink:
            if ftype is None or fr.type == ftype:
                return fr
    uart = board.uart
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for fr in _pump(board, sink):
            if ftype is None or fr.type == ftype:
                return fr
        time.sleep(uart.poll_interval)
    raise UartTimeoutError(f"等待帧超时 ({timeout}s, type={ftype})")


def _wait_alert(board, fault_id: int, timeout: float = 3.0, sink=None):
    """等待指定 fault_id 的告警帧 (先扫 sink 已收帧, 再 pump 等新帧)。"""
    if sink is not None:
        for fr in sink:
            if fr.type == TYPE_ALERT and fr.values[0] == fault_id:
                return fr
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for fr in _pump(board, sink):
            if fr.type == TYPE_ALERT and fr.values[0] == fault_id:
                return fr
        time.sleep(board.uart.poll_interval)
    raise UartTimeoutError(f"等待告警帧超时 ({timeout}s, fault_id={fault_id})")


def _collect(board, duration: float, sink: list | None = None) -> list:
    """限时收帧; sink 非空时同步追加 (返回值是本阶段子集)。"""
    out = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        out.extend(_pump(board, sink))
        time.sleep(board.uart.poll_interval)
    return out


# --------------------------------------------------------------------- #
# TC-FLT-001  FAULT OV 告警帧 (FR-06)
# --------------------------------------------------------------------- #
def test_tc_flt_001_fault_ov_alert(board):
    """
    需求: FR-06

    注入 FAULT OV 15: 2s 内收到 fault_id=1 level=2 value=15 告警帧.

    附带验证固件的一次性覆盖语义: 之后 1s 内恰有一帧数据的电压 == 15.0
    (正常电压自然范围约 10~14.2, 15.0 只可能来自故障覆盖)。
    注意: 告警帧与被强制的下一帧数据都可能在等待 OK 回显期间就到达
    (同一批字节), 因此全程共用同一 sink 统计, 不能分阶段丢弃。
    """
    _drain(board)
    sink: list = []                                  # 注入后全量帧
    reply, _ = _cmd(board, "FAULT OV 15", sink=sink)
    assert reply.startswith("OK"), f"FAULT OV 回显异常: {reply!r}"

    fr = _wait_frame(board, TYPE_ALERT, timeout=2.0, sink=sink)
    fault_id, level, value = fr.values
    assert (fault_id, level) == (1, 2), f"告警字段错误: fid={fault_id} lvl={level}"
    assert value == 15, f"告警 value 应=注入值 15 (u16), 实际 {value}"

    _collect(board, 1.0, sink=sink)
    data = [f for f in sink if f.type == TYPE_DATA]
    forced = [f for f in data if abs(f.values[0] - 15.0) < 1e-6]
    assert len(forced) == 1, f"应有恰好 1 帧被强制为 15.0V, 实际 {len(forced)}"


# --------------------------------------------------------------------- #
# TC-FLT-002  FAULT OC 告警帧 (FR-06)
# --------------------------------------------------------------------- #
def test_tc_flt_002_fault_oc_alert(board):
    """
    需求: FR-06

    注入 FAULT OC 2.5: 2s 内收到 fault_id=2 level=2 告警帧.

    注: 告警 value 为 u16 四舍五入 ((uint16_t)(2.5+0.5)=3), 规格只对 OC
    要求 fault_id/level; 电流覆盖值以数据帧 float 为准 (恰一帧 i==2.5,
    自然电流范围约 1.0~2.05, 2.5 只可能来自故障覆盖)。
    与 001 同理: 告警帧与被强制数据帧可能随 OK 回显同批到达, 全程共用 sink。
    """
    _drain(board)
    sink: list = []
    reply, _ = _cmd(board, "FAULT OC 2.5", sink=sink)
    assert reply.startswith("OK"), f"FAULT OC 回显异常: {reply!r}"

    fr = _wait_frame(board, TYPE_ALERT, timeout=2.0, sink=sink)
    fault_id, level, value = fr.values
    assert (fault_id, level) == (2, 2), f"告警字段错误: fid={fault_id} lvl={level}"
    assert value == 3, f"value 应为 u16 四舍五入后的 3, 实际 {value}"

    _collect(board, 1.0, sink=sink)
    data = [f for f in sink if f.type == TYPE_DATA]
    forced = [f for f in data if abs(f.values[1] - 2.5) < 1e-6]
    assert len(forced) == 1, f"应有恰好 1 帧被强制为 2.5A, 实际 {len(forced)}"


# --------------------------------------------------------------------- #
# TC-FLT-003  告警响应时间统计 (FR-06/NFR-04)
# --------------------------------------------------------------------- #
def test_tc_flt_003_alert_response_time(board):
    """
    需求: FR-06/NFR-04

    循环注入 50 次 OV (交替 15/16), 统计 命令发出->告警帧到达 P50/P95.

    断言 P95 < 500ms; 统计结果写 report/alert_stats.json (断言前落盘)。
    """
    _drain(board)
    sent_at: list[float] = []
    alerts: list = []
    for k in range(50):
        v = 15 if k % 2 == 0 else 16
        t_sent = _send_line(board, f"FAULT OV {v}")
        fr = _wait_frame(board, TYPE_ALERT, timeout=2.0)
        fault_id, level, value = fr.values
        assert (fault_id, level, value) == (1, 2, v), (
            f"第 {k} 次告警字段错误: fid={fault_id} lvl={level} val={value}"
        )
        sent_at.append(t_sent)
        alerts.append(fr)

    samples = asserts.alert_response_times(sent_at, alerts, fault_id=1)
    stats = asserts.response_time_stats(samples)

    # 50 次命令回显在此期间陆续到达, 顺便验证无命令丢失
    ok_replies = 0
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and ok_replies < 50:
        _pump(board)
        while True:
            line = board.uart.parser.pop_text_line()
            if line is None:
                break
            if line == "OK FAULT OV":
                ok_replies += 1
        time.sleep(board.uart.poll_interval)

    ALERT_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATS_PATH.write_text(
        json.dumps(
            {
                "case": "TC-FLT-003",
                "fault": "OV",
                "iterations": 50,
                "ok_replies": ok_replies,
                "assert": "p95 < 500ms",
                "stats_ms": stats,
                "samples_ms": [round(s, 3) for s in samples],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    assert ok_replies == 50, f"50 次注入仅 {ok_replies} 条 OK 回显"
    assert stats["n"] == 50, f"配对样本数 {stats['n']} != 50"
    assert stats["p95"] < 500.0, f"告警响应 P95={stats['p95']:.1f}ms 超 500ms"


# --------------------------------------------------------------------- #
# TC-FLT-004  DISCONN 丢帧检测 + CONN 恢复 (FR-07)
# --------------------------------------------------------------------- #
def test_tc_flt_004_disconn_frame_gap(board):
    """
    需求: FR-07

    DISCONN 后 3s 窗口: 数据帧数=0 且心跳帧仍到达; CONN 后数据帧恢复,
    且从注入前到恢复后的全量帧 seq 连续衔接 (DISCONN 期间心跳帧继续打号,
    因此整条流不应出现序号缺口)。
    """
    sink: list = []
    _drain(board)
    _collect(board, 0.5, sink)                      # 注入前基线

    reply, _ = _cmd(board, "FAULT DISCONN", sink=sink)
    assert reply.startswith("OK"), f"DISCONN 回显异常: {reply!r}"

    paused = _collect(board, 3.0, sink)
    n_data = sum(1 for f in paused if f.type == TYPE_DATA)
    n_hb = sum(1 for f in paused if f.type == TYPE_HEARTBEAT)
    assert n_data == 0, f"DISCONN 期间仍收到 {n_data} 帧数据"
    assert n_hb >= 2, f"DISCONN 期间心跳帧也应继续, 3s 仅 {n_hb} 帧"

    reply, _ = _cmd(board, "FAULT CONN", sink=sink)
    assert reply.startswith("OK"), f"CONN 回显异常: {reply!r}"

    resumed = _collect(board, 1.5, sink)
    n_data2 = sum(1 for f in resumed if f.type == TYPE_DATA)
    assert n_data2 >= 5, f"CONN 后 1.5s 仅恢复 {n_data2} 帧数据 (<5)"

    sc = asserts.seq_continuity(sink)
    assert sc["is_contiguous"], f"DISCONN/CONN 前后序号不连续: {sc}"


# --------------------------------------------------------------------- #
# TC-FLT-005  DISCONN/CONN 往返 10 次稳定性 (FR-07)
# --------------------------------------------------------------------- #
def test_tc_flt_005_disconn_conn_roundtrip(board):
    """
    需求: FR-07

    DISCONN/CONN 往返 10 次: 每次断开期数据帧=0 且心跳在, 恢复后数据帧
    回升; 结束后 STAT? 正常应答 (系统稳定)。

    竞态处理: DISCONN 的 OK 回显与"故障生效前最后一帧在途数据"可能同批
    到达, 故 OK 后先静置 0.2s (>1 个帧周期) 并 drain, 再开干净窗口断言
    数据帧=0; 窗口取 1.5s (>1s 心跳周期 + QEMU 调度抖动余量)。
    """
    _drain(board)
    for i in range(10):
        reply, _ = _cmd(board, "FAULT DISCONN")
        assert reply.startswith("OK"), f"第 {i} 轮 DISCONN 回显: {reply!r}"
        time.sleep(0.2)                     # 等在途数据帧落地
        _drain(board)                       # 丢弃故障生效前的残余帧
        paused = _collect(board, 1.5)
        n_data = sum(1 for f in paused if f.type == TYPE_DATA)
        n_hb = sum(1 for f in paused if f.type == TYPE_HEARTBEAT)
        assert n_data == 0, f"第 {i} 轮 DISCONN 期间收到 {n_data} 帧数据"
        assert n_hb >= 1, f"第 {i} 轮 DISCONN 期间心跳丢失"

        reply, _ = _cmd(board, "FAULT CONN")
        assert reply.startswith("OK"), f"第 {i} 轮 CONN 回显: {reply!r}"
        resumed = _collect(board, 0.8)
        n_data2 = sum(1 for f in resumed if f.type == TYPE_DATA)
        assert n_data2 >= 3, f"第 {i} 轮 CONN 后 0.8s 仅 {n_data2} 帧数据"

    st = _stat(board)
    assert st["tasks"] == 4, f"10 轮往返后任务数异常: {st}"


# --------------------------------------------------------------------- #
# TC-FLT-006  FAULT HANG sensor (FR-08)
# --------------------------------------------------------------------- #
def test_tc_flt_006_hang_sensor(board):
    """
    需求: FR-08

    挂起 sensor: 数据帧停止; STAT? 可见 tasks 4->3, 心跳位图 bit0 清零;
    hb 持续递增 (挂的是 sensor 不是 watchdog, 不触发复位 —— 实测语义,
    与 firmware/Core/watchdog_task.c 一致: hb_count 仅由 WatchdogTask 递增)。
    """
    _drain(board)
    st0 = _stat(board)
    assert st0["tasks"] == 4, f"注入前 tasks 应为 4: {st0}"

    reply, _ = _cmd(board, "FAULT HANG sensor")
    assert reply.startswith("OK"), f"HANG 回显异常: {reply!r}"

    win = _collect(board, 1.5)
    n_data = sum(1 for f in win if f.type == TYPE_DATA)
    hbs = [f for f in win if f.type == TYPE_HEARTBEAT]
    assert n_data == 0, f"HANG sensor 后仍收到 {n_data} 帧数据"
    assert hbs, "HANG sensor 后心跳帧也应继续"
    assert hbs[-1].values[0] & 0x01 == 0, (
        f"心跳位图 bit0(sensor) 应清零, bitmap=0x{hbs[-1].values[0]:02X}"
    )

    st1 = _stat(board)
    assert st1["tasks"] == 3, f"HANG sensor 后 tasks 应为 3: {st1}"
    time.sleep(1.3)
    st2 = _stat(board)
    assert st2["hb"] > st1["hb"], (
        f"hb 应持续递增 (watchdog 活着, 不复位): {st1['hb']} -> {st2['hb']}"
    )

    reply, _ = _cmd(board, "RESUME sensor")         # 善后, 恢复现场
    assert reply.startswith("OK")


# --------------------------------------------------------------------- #
# TC-FLT-007  RESUME sensor 恢复 (FR-08)
# --------------------------------------------------------------------- #
def test_tc_flt_007_resume_sensor(board):
    """
    需求: FR-08

    HANG sensor 后 RESUME sensor: 数据帧恢复, tasks 回到 4, 位图 bit0 置位。"""
    _drain(board)
    reply, _ = _cmd(board, "FAULT HANG sensor")
    assert reply.startswith("OK")
    _collect(board, 0.5)                            # 确认挂起已生效

    reply, _ = _cmd(board, "RESUME sensor")
    assert reply.startswith("OK"), f"RESUME 回显异常: {reply!r}"

    win = _collect(board, 1.2)
    n_data = sum(1 for f in win if f.type == TYPE_DATA)
    hbs = [f for f in win if f.type == TYPE_HEARTBEAT]
    assert n_data >= 5, f"RESUME 后 1.2s 仅恢复 {n_data} 帧数据 (<5)"
    if hbs:
        assert hbs[-1].values[0] & 0x01 == 1, (
            f"心跳位图 bit0(sensor) 应恢复, bitmap=0x{hbs[-1].values[0]:02X}"
        )
    st = _stat(board)
    assert st["tasks"] == 4, f"RESUME 后 tasks 应为 4: {st}"


# --------------------------------------------------------------------- #
# TC-FLT-008  非法命令 (NFR-05)
# --------------------------------------------------------------------- #
def test_tc_flt_008_invalid_command(board):
    """
    需求: NFR-05

    发 `FAULT XX`: 收到 ERR 回显; 系统不死 —— 之后数据帧照常, STAT? 正常。"""
    _drain(board)
    reply, _ = _cmd(board, "FAULT XX")
    assert reply.startswith("ERR"), f"非法命令应回显 ERR, 实际: {reply!r}"

    win = _collect(board, 1.0)
    n_data = sum(1 for f in win if f.type == TYPE_DATA)
    assert n_data >= 5, f"非法命令后数据帧异常: 1s 仅 {n_data} 帧"
    st = _stat(board)
    assert st["tasks"] == 4, f"非法命令后 tasks 异常: {st}"


# --------------------------------------------------------------------- #
# TC-FLT-009  命令洪泛 (NFR-05)
# --------------------------------------------------------------------- #
def test_tc_flt_009_command_flood(board):
    """
    需求: NFR-05

    1s 内连发 100 条 STAT?: 系统不崩, 之后帧序号仍连续.

    实现: 600 字节命令流分块写入串口 (块间顺带 pump, 避免一次性写满
    pty 主机侧缓冲导致 pyserial 阻塞写挂起; 5 块总耗时仍远小于 1s,
    洪泛语义不变), 然后统计 OK STAT 回显行数。固件 RX 环 128B +
    DiagTask 最高优先级逐字节消费, 正常无丢失;
    阈值取 >=95 以容忍 QEMU/pty 偶发字节丢弃, 丢字节时固件会回 ERR 而非崩溃。
    所有等待均有硬上限 (回显收集 12s, STAT? 3s), 不会无限挂起。
    """
    _drain(board)
    before = _collect(board, 0.5)

    frames_during: list = []
    ser = board.uart._ser  # noqa: SLF001
    payload = b"STAT?\n" * 100
    for off in range(0, len(payload), 128):
        ser.write(payload[off:off + 128])
        ser.flush()
        _pump(board, frames_during)

    n_ok = 0
    deadline = time.monotonic() + 12.0                # 硬上限, 防洪泛后死等
    while time.monotonic() < deadline and n_ok < 100:
        _pump(board, frames_during)
        while True:
            line = board.uart.parser.pop_text_line()
            if line is None:
                break
            if line.startswith("OK STAT"):
                n_ok += 1
        time.sleep(board.uart.poll_interval)

    after = _collect(board, 1.0)

    st = _stat(board, timeout=5.0)                    # 洪泛后仍能正常应答 (有界)
    assert st["tasks"] == 4, f"洪泛后 tasks 异常: {st}"
    assert n_ok >= 95, f"100 条 STAT? 仅 {n_ok} 条 OK 回显 (<95)"

    sc = asserts.seq_continuity(before + frames_during + after)
    assert sc["is_contiguous"], f"洪泛前后帧序号不连续: {sc}"


# --------------------------------------------------------------------- #
# TC-FLT-010  组合故障 (FR-06/FR-07)
# --------------------------------------------------------------------- #
def test_tc_flt_010_combined_faults(board):
    """
    需求: FR-06/FR-07

    OV+OC+DISCONN 同时注入, 逐条恢复后系统回到正常.

    流程: OV 15 -> 告警(1,2,15); OC 3 -> 告警(2,2,3); DISCONN -> 数据停;
    CONN -> 数据恢复; 全程 seq 连续; 最后 STAT? 正常。

    固件语义 (firmware/Core/sensor_task.c): 告警帧由 SensorTask 在"下一个
    实际发出的采样点"随被强制数据帧一起入队, 采样被 DISCONN 暂停期间
    挂起的覆盖不会被消费。因此每条 FAULT 注入后必须先等到对应告警帧,
    再发 DISCONN, 否则后注入的 OC 告警会被暂停语义吞掉 (测试顺序问题,
    非固件缺陷)。DISCONN 的 OK 回显与故障生效前最后一帧在途数据可能
    同批到达, 故断开窗口只统计 OK 时刻 +0.15s 之后收到的数据帧。
    """
    sink: list = []
    _drain(board)
    _collect(board, 0.4, sink)

    reply, _ = _cmd(board, "FAULT OV 15", sink=sink)
    assert reply.startswith("OK"), reply
    a1 = _wait_alert(board, 1, timeout=2.0, sink=sink)
    assert a1.values == (1, 2, 15), f"OV 告警字段错误: {a1.values}"

    reply, _ = _cmd(board, "FAULT OC 3", sink=sink)
    assert reply.startswith("OK"), reply
    a2 = _wait_alert(board, 2, timeout=2.0, sink=sink)
    assert a2.values == (2, 2, 3), f"OC 告警字段错误: {a2.values}"

    reply, _ = _cmd(board, "FAULT DISCONN", sink=sink)
    assert reply.startswith("OK"), reply
    t_ok = time.monotonic()

    paused = _collect(board, 1.5, sink)
    n_data = sum(
        1 for f in paused
        if f.type == TYPE_DATA and f.received_at > t_ok + 0.15
    )
    n_hb = sum(1 for f in paused if f.type == TYPE_HEARTBEAT)
    assert n_data == 0, f"DISCONN 生效后仍收到 {n_data} 帧数据"
    assert n_hb >= 1, "DISCONN 期间心跳应继续"

    reply, _ = _cmd(board, "FAULT CONN", sink=sink)
    assert reply.startswith("OK"), reply
    resumed = _collect(board, 1.2, sink)
    n_data2 = sum(1 for f in resumed if f.type == TYPE_DATA)
    assert n_data2 >= 5, f"组合故障恢复后 1.2s 仅 {n_data2} 帧数据"

    sc = asserts.seq_continuity(sink)
    assert sc["is_contiguous"], f"组合故障全程序号不连续: {sc}"

    st = _stat(board)
    assert st["tasks"] == 4, f"恢复后 tasks 异常: {st}"
