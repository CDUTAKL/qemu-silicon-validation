"""断言与统计: 序号连续性 / 响应时间 / 上报周期.

所有统计函数返回 plain dict, 便于直接写入 JSON 测试报告。
时间单位统一为 **毫秒** (输入为秒或 datetime 均可)。

Typical usage::

    stats = seq_continuity(frames)
    assert stats["lost_frames"] == 0

    samples = alert_response_times(cmd_sent_at, frames)
    report = response_time_stats(samples)

    report = period_stats([f.received_at for f in data_frames])
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any, Iterable, Sequence


# --------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------- #
def _to_ms(t: float | datetime) -> float:
    """秒 (float) 或 datetime -> 毫秒 float。"""
    if isinstance(t, datetime):
        return t.timestamp() * 1000.0
    return float(t) * 1000.0


def _percentile(sorted_vals: Sequence[float], p: float) -> float:
    """p ∈ [0,100] 线性插值分位数 (输入须已排序); 空序列返回 NaN。"""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _basic_stats(vals: Sequence[float]) -> dict[str, float | int]:
    if not vals:
        return {"n": 0, "min": float("nan"), "max": float("nan"),
                "mean": float("nan"), "p50": float("nan"),
                "p95": float("nan"), "p99": float("nan")}
    s = sorted(vals)
    return {
        "n": len(vals),
        "min": s[0],
        "max": s[-1],
        "mean": sum(s) / len(s),
        "p50": _percentile(s, 50),
        "p95": _percentile(s, 95),
        "p99": _percentile(s, 99),
    }


# --------------------------------------------------------------------- #
# 序号连续性
# --------------------------------------------------------------------- #
def seq_continuity(frames: Iterable[Any]) -> dict[str, Any]:
    """序号连续性断言统计.

    Args:
        frames: Frame 对象序列 (须有 ``.seq`` 属性), 按到达顺序。

    Returns:
        dict: total / lost_frames (序号缺口累计) / duplicate_frames /
        out_of_order_frames (回绕序号视为乱序) / first_seq / last_seq /
        is_contiguous (无丢帧且无重复)。
    """
    seqs = [int(f.seq) for f in frames]
    total = len(seqs)
    lost = dup = ooo = 0
    for prev, cur in zip(seqs, seqs[1:]):
        gap = (cur - prev) & 0xFFFF
        if gap == 0:
            dup += 1
        elif gap == 1:
            pass
        elif cur < prev and gap > 0x8000:
            # u16 序号大幅回退: 视为复位, 不算丢帧 (由调用方按场景解释)
            pass
        else:
            lost += gap - 1
            if cur < prev:
                ooo += 1
    return {
        "total": total,
        "lost_frames": lost,
        "duplicate_frames": dup,
        "out_of_order_frames": ooo,
        "first_seq": seqs[0] if seqs else None,
        "last_seq": seqs[-1] if seqs else None,
        "is_contiguous": (lost == 0 and dup == 0),
    }


# --------------------------------------------------------------------- #
# 响应时间 (告警注入 -> 告警帧)
# --------------------------------------------------------------------- #
def alert_response_times(
    cmd_sent_at: Sequence[float | datetime],
    frames: Iterable[Any],
    *,
    fault_id: int | None = None,
) -> list[float]:
    """配对"命令发出时刻 -> 之后第一帧匹配告警帧的接收时刻", 返回毫秒样本.

    Args:
        cmd_sent_at: 每次告警注入命令发出的时间戳序列 (秒或 datetime)。
        frames: 按接收顺序的 Frame 序列 (用 ``received_at``, 秒)。
        fault_id: 仅配对该 fault_id 的告警帧 (None = 任意告警帧)。
    """
    alerts = [f for f in frames
              if getattr(f, "type", None) == 0x02 and
              (fault_id is None or f.values[0] == fault_id)]
    samples: list[float] = []
    for t_sent in cmd_sent_at:
        t0 = _to_ms(t_sent)
        after = [f for f in alerts if f.received_at * 1000.0 >= t0]
        if after:
            samples.append(after[0].received_at * 1000.0 - t0)
    return samples


def response_time_stats(samples_ms: Iterable[float]) -> dict[str, Any]:
    """响应时间样本 (毫秒) -> 统计 dict: n/min/max/mean/p50/p95/p99."""
    return _basic_stats(list(samples_ms))


# --------------------------------------------------------------------- #
# 上报周期
# --------------------------------------------------------------------- #
def period_stats(timestamps: Iterable[float | datetime]) -> dict[str, Any]:
    """相邻时间戳间隔 (毫秒) 的统计: n/mean/std/min/max/p50/p99.

    Args:
        timestamps: 单调递增的时间戳序列 (秒 float 或 datetime),
            通常取数据帧的 ``received_at`` 或固件时间戳字段。
    """
    ts = [_to_ms(t) for t in timestamps]
    intervals = [b - a for a, b in zip(ts, ts[1:])]
    stats = _basic_stats(intervals)
    if intervals:
        stats["std"] = statistics.stdev(intervals) if len(intervals) > 1 else 0.0
    else:
        stats["std"] = float("nan")
    return stats
