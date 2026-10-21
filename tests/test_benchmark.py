"""性能基准用例 TC-BM-001~004 + 基准趋势存储 (report/benchmark_history.jsonl).

重要设计说明 —— 为什么断言阈值这么"松":
    被测固件跑在 QEMU TCG 软件仿真上, 实测耗时受 **宿主机负载** 显著影响
    (CI 机器、笔记本省电模式、并行编译都会让数字漂移数倍)。因此本文件的
    断言只做 **量级断言** (例如启动时间 p95 < 5s、上报周期均值在
    50~200ms 之间), 用于拦截"功能坏了/数量级错了"级别的失效;
    真正的版本间性能回归检测由 **趋势机制** 承担: 每次运行把各用例的
    统计指标追加写入 ``report/benchmark_history.jsonl``, 由
    ``tests/benchmark_compare.py`` 对比最近两次运行, 任一指标 P50
    退化超过 20% 即退出码 1 (供 CI 报警)。

用例 ↔ 需求映射:
    TC-BM-001  启动时间基准   QMP system_reset -> 收到 BOOT 横幅, x10, P50/P95  (NFR-06)
    TC-BM-002  上报周期基准   收 300 条 0x01 数据帧, period_stats 均值/std/P50/P99 (NFR-04)
    TC-BM-003  命令往返延迟   STAT? 发出 -> 收到 OK 回显的 RTT, x100, P50/P95      (NFR-04)
    TC-BM-004  心跳周期基准   1s 心跳帧 (0x03) 实际间隔分布                        (NFR-04)

注意: 冻结规格中 ``STAT?`` 的回显是 ``OK STAT tasks=... seq=...`` (OK 后带
内容), 而 ``UartChannel.send_command`` 只识别裸 ``OK``/``ERR`` 行, 会等到超时;
因此 TC-BM-003 直接写串口字节并用 ``wait_text`` 匹配 ``OK`` 开头的行。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# --- 路径与环境兜底 ------------------------------------------------------- #
# conftest.py 由另一代理提供; 此处做幂等兜底, 保证本文件单独可跑:
# 若 conftest 已配置 QSV_KERNEL / QSV_BOOT_BANNER, setdefault 不会覆盖。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QSV_KERNEL", str(ROOT / "firmware" / "build" / "firmware.elf"))
os.environ.setdefault("QSV_BOOT_BANNER", r"BOOT")

from harness import asserts                      # noqa: E402
from harness.fixture import board  # noqa: E402,F401  (注册 fixture)
from harness.uart import TYPE_DATA, TYPE_HEARTBEAT  # noqa: E402
from conftest import send_text_command           # noqa: E402  conftest 共享辅助

# --- 基准趋势存储 --------------------------------------------------------- #
HISTORY_PATH = ROOT / "report" / "benchmark_history.jsonl"
RUN_ID = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def _git_commit() -> str:
    """当前 git 短 commit; 非 git 仓库或失败时返回 "local"。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "local"


def record_benchmark(case_id: str, metrics: dict) -> None:
    """把一组基准结果追加写入 report/benchmark_history.jsonl (每行一条 JSON).

    字段: run_id (本次 pytest 运行标识) / commit / timestamp (UTC ISO) /
    case (用例 ID) / metrics (指标名 -> 统计 dict, 至少含 p50)。
    在断言 **之前** 调用, 即使断言失败也留下本次实测数据。
    """
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "run_id": RUN_ID,
        "commit": _git_commit(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "case": case_id,
        "metrics": metrics,
    }
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --- 工具 ----------------------------------------------------------------- #
def _drain_text(board) -> None:
    """丢弃串口解析器中积压的文本行, 避免匹配到上一轮的陈旧回显。"""
    while board.uart.parser.pop_text_line() is not None:
        pass


# --------------------------------------------------------------------- #
# TC-BM-001  启动时间基准 (NFR-06)
# --------------------------------------------------------------------- #
def test_tc_bm_001_boot_time(board):
    """
    需求: NFR-06

    QMP system_reset -> 收到 BOOT 横幅的耗时, 重复 10 次, 统计 P50/P95.

    量级断言: p95 < 5000ms (QEMU 冷复位+固件初始化正常为百毫秒级,
    阈值放宽一个数量级以容忍宿主机负载波动; 回归检测靠趋势对比)。
    """
    samples_ms: list[float] = []
    for _ in range(10):
        _drain_text(board)
        t0 = time.monotonic()
        board._run(board.qmp.system_reset(resume=True))   # noqa: SLF001
        board.uart.wait_text(r"BOOT", timeout=10.0)
        samples_ms.append((time.monotonic() - t0) * 1000.0)

    stats = asserts.response_time_stats(samples_ms)
    record_benchmark("TC-BM-001", {"boot_time_ms": stats})

    assert stats["n"] == 10
    assert stats["p95"] < 5000.0, f"启动时间 p95 超量级: {stats['p95']:.1f}ms"


# --------------------------------------------------------------------- #
# TC-BM-002  上报周期基准 (NFR-04)
# --------------------------------------------------------------------- #
def test_tc_bm_002_data_period(board):
    """
    需求: NFR-04

    收 300 条 0x01 数据帧 (标称周期 100ms), period_stats 均值/std/P50/P99.

    注意 received_at 含 UartChannel 20ms 轮询粒度引入的量化抖动, 因此
    p99 不做硬断言; 量级断言: 均值落在 [50, 200]ms (标称 100ms 的一半到两倍)。
    """
    frames = []
    deadline = time.monotonic() + 90.0        # 300 帧 x 100ms ≈ 30s, 留 3x 余量
    while len(frames) < 300 and time.monotonic() < deadline:
        frames.extend(board.uart.collect_frames(1.0, type=TYPE_DATA))
    assert len(frames) >= 300, f"90s 内仅收到 {len(frames)} 条数据帧"
    frames = frames[:300]

    pd = asserts.period_stats(f.received_at for f in frames)
    record_benchmark("TC-BM-002", {"data_period_ms": pd})

    assert 50.0 < pd["mean"] < 200.0, f"上报周期均值超量级: {pd['mean']:.1f}ms"


# --------------------------------------------------------------------- #
# TC-BM-003  命令往返延迟 (NFR-04)
# --------------------------------------------------------------------- #
def test_tc_bm_003_command_rtt(board):
    """
    需求: NFR-04

    STAT? 发出 -> 收到 OK 回显 (``OK STAT tasks=... seq=...``) 的 RTT, x100.

    量级断言: p95 < 1000ms (正常为几十毫秒级, 含 20ms 轮询量化)。
    """
    samples_ms: list[float] = []
    for _ in range(100):
        _drain_text(board)
        t0 = time.monotonic()
        send_text_command(board, "STAT?")
        board.uart.wait_text(r"^OK\b", timeout=5.0)
        samples_ms.append((time.monotonic() - t0) * 1000.0)

    stats = asserts.response_time_stats(samples_ms)
    record_benchmark("TC-BM-003", {"cmd_rtt_ms": stats})

    assert stats["n"] == 100
    assert stats["p95"] < 1000.0, f"STAT? RTT p95 超量级: {stats['p95']:.1f}ms"


# --------------------------------------------------------------------- #
# TC-BM-004  心跳周期基准 (NFR-04)
# --------------------------------------------------------------------- #
def test_tc_bm_004_heartbeat_period(board):
    """
    需求: NFR-04

    收 15s 心跳帧 (0x03, 标称周期 1s), 统计实际间隔分布.

    量级断言: 均值落在 [500, 2000]ms (标称 1s 的一半到两倍)。
    """
    frames = board.uart.collect_frames(15.0, type=TYPE_HEARTBEAT)
    assert len(frames) >= 6, f"15s 内仅收到 {len(frames)} 帧心跳 (<6)"

    pd = asserts.period_stats(f.received_at for f in frames)
    record_benchmark("TC-BM-004", {"heartbeat_period_ms": pd})

    assert 500.0 < pd["mean"] < 2000.0, f"心跳周期均值超量级: {pd['mean']:.1f}ms"
