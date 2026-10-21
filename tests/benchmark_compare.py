#!/usr/bin/env python3
"""基准趋势对比: 读 report/benchmark_history.jsonl, 对比最近两次运行.

版本间性能回归报警 (供 CI 使用):
    按 run_id 把历史记录分组为多次"运行", 取最近一次与上一次运行,
    对每个用例的每个指标比较 P50 —— 任一指标 P50 退化超过阈值
    (默认 +20%) 即打印退化表并以退出码 1 结束; 否则退出码 0。

为什么阈值是 P50 相对退化而不是绝对值:
    QEMU TCG 软件仿真的绝对耗时随宿主机负载漂移, 绝对阈值在 CI 上
    不可靠; 同一台机器相邻两次运行的相对比较更能反映代码变更引入的
    真实回归。宿主机负载整体偏高会同时抬高两次运行的基数, 因此被
    自然抵消 (趋势对比的前提: 两次运行在同构环境完成)。

用法:
    .venv/bin/python tests/benchmark_compare.py [--history PATH] [--threshold 0.20]

历史文件格式 (每行一条 JSON, 由 tests/test_benchmark.py 追加):
    {"run_id": ..., "commit": ..., "timestamp": ..., "case": "TC-BM-001",
     "metrics": {"boot_time_ms": {"n":.., "mean":.., "p50":.., "p95":.., ...}}}

退出码:
    0  无退化, 或历史记录不足两次运行 (无法对比, 不视为失败)
    1  至少一项指标 P50 退化超过阈值
    2  用法/文件错误
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY = ROOT / "report" / "benchmark_history.jsonl"


def load_runs(path: Path) -> list[dict]:
    """读 jsonl, 按 run_id 分组为运行列表 (按各运行首条记录时间排序).

    返回: [{"run_id":..., "commit":..., "timestamp":...,
            "cases": {case_id: {metric_name: stats_dict}}}]
    同一 run_id 内同一 case 出现多条时保留最后一条 (后写覆盖)。
    """
    runs: dict[str, dict] = {}
    order: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                print(f"警告: 跳过坏行 {path}:{lineno}", file=sys.stderr)
                continue
            rid = rec.get("run_id") or rec.get("timestamp") or f"line{lineno}"
            run = runs.get(rid)
            if run is None:
                run = runs[rid] = {
                    "run_id": rid,
                    "commit": rec.get("commit", "?"),
                    "timestamp": rec.get("timestamp", "?"),
                    "cases": {},
                }
                order.append(rid)
            case = rec.get("case")
            if case and isinstance(rec.get("metrics"), dict):
                run["cases"][case] = rec["metrics"]
                # 以最新记录的时间/commit 代表该次运行
                run["timestamp"] = rec.get("timestamp", run["timestamp"])
                run["commit"] = rec.get("commit", run["commit"])
    return [runs[rid] for rid in order]


def compare_runs(prev: dict, cur: dict, threshold: float) -> tuple[list[dict], list[str]]:
    """逐 case 逐指标比较 P50, 返回 (全部对比行, 退化描述列表)。"""
    rows: list[dict] = []
    regressions: list[str] = []
    for case, metrics in sorted(cur["cases"].items()):
        prev_metrics = prev["cases"].get(case, {})
        for name, stats in sorted(metrics.items()):
            new_p50 = stats.get("p50") if isinstance(stats, dict) else None
            old_stats = prev_metrics.get(name)
            old_p50 = old_stats.get("p50") if isinstance(old_stats, dict) else None
            row = {"case": case, "metric": name,
                   "prev_p50": old_p50, "cur_p50": new_p50,
                   "delta_pct": None, "regressed": False, "note": ""}
            if old_p50 is None:
                row["note"] = "上次运行无此指标, 跳过"
            elif new_p50 is None:
                row["note"] = "本次运行缺 p50, 跳过"
            elif any(isinstance(v, float) and math.isnan(v)
                     for v in (old_p50, new_p50)):
                row["note"] = "p50 为 NaN, 跳过"
            elif old_p50 <= 0:
                row["note"] = "基线 p50 <= 0, 无法相对比较, 跳过"
            else:
                delta = (new_p50 - old_p50) / old_p50 * 100.0
                row["delta_pct"] = delta
                if delta > threshold * 100.0:
                    row["regressed"] = True
                    regressions.append(
                        f"{case}/{name}: P50 {old_p50:.2f} -> {new_p50:.2f} "
                        f"(+{delta:.1f}%, 阈值 +{threshold * 100:.0f}%)"
                    )
            rows.append(row)
    return rows, regressions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="基准趋势对比: 最近两次运行 P50 回归检测")
    ap.add_argument("--history", type=Path, default=DEFAULT_HISTORY,
                    help=f"基准历史 jsonl (默认 {DEFAULT_HISTORY})")
    ap.add_argument("--threshold", type=float, default=0.20,
                    help="P50 相对退化阈值 (默认 0.20 = +20%%)")
    args = ap.parse_args(argv)

    if not args.history.exists():
        print(f"错误: 历史文件不存在: {args.history}", file=sys.stderr)
        return 2

    runs = load_runs(args.history)
    if len(runs) < 2:
        print(f"历史记录不足两次运行 (当前 {len(runs)} 次), 无可对比基线, 通过。")
        return 0

    prev, cur = runs[-2], runs[-1]
    print(f"基线运行: {prev['run_id']} (commit={prev['commit']}, {prev['timestamp']})")
    print(f"当前运行: {cur['run_id']} (commit={cur['commit']}, {cur['timestamp']})")
    print(f"P50 退化阈值: +{args.threshold * 100:.0f}%")
    print()

    rows, regressions = compare_runs(prev, cur, args.threshold)
    if not rows:
        print("两次运行没有可对比的指标, 通过。")
        return 0

    # 对比总表
    print(f"{'CASE':<10} {'METRIC':<22} {'PREV P50':>12} {'CUR P50':>12} "
          f"{'DELTA':>9}  STATUS")
    for r in rows:
        prev_s = f"{r['prev_p50']:.2f}" if isinstance(r['prev_p50'], (int, float)) else "-"
        cur_s = f"{r['cur_p50']:.2f}" if isinstance(r['cur_p50'], (int, float)) else "-"
        delta_s = f"{r['delta_pct']:+.1f}%" if r['delta_pct'] is not None else "-"
        status = "REGRESS" if r["regressed"] else (r["note"] or "ok")
        print(f"{r['case']:<10} {r['metric']:<22} {prev_s:>12} {cur_s:>12} "
              f"{delta_s:>9}  {status}")
    print()

    if regressions:
        print(f"检测到 {len(regressions)} 项 P50 退化超过阈值:")
        for line in regressions:
            print(f"  !! {line}")
        return 1
    print("无指标 P50 退化超过阈值, 通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
