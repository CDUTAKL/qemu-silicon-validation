"""总报告生成：汇总各层 JSON 产物 → report/final_report.html（自包含单文件）。

数据源（全部可选，缺失时对应章节显示"数据缺失"而非报错）：
- report/junit.xml                  pytest 全量结果（用例明细/通过率）
- report/alert_stats.json           TC-FLT-003 告警响应时间统计
- report/fault_injection_stats.json 故障注入战役统计（三档）
- report/coverage_summary.json      gcov 覆盖率汇总（POSIX port 双轨）
- report/benchmark_history.jsonl    基准历史（按 run_id 分组，最近两次对比）
- report/rtm.json                   RTM 需求追踪矩阵

仅依赖标准库。各层 schema 按本仓库实际产物解析（schema-aware），
同时保留递归键名兜底，字段缺失时标 N/A。

运行：python report/final_report.py [--report-dir report] [--out PATH]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html as _html
import json
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_DIR = PROJECT_ROOT / "report"

# 与 tests/benchmark_compare.py 对齐：P50 相对退化阈值
REGRESSION_THRESHOLD = 0.20


def _esc(s: object) -> str:
    return _html.escape(str(s), quote=True)


def _load_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _find(obj: object, *aliases: str) -> object | None:
    """在嵌套 dict/list 中按别名递归查找第一个匹配的键（大小写不敏感）。"""
    want = {a.lower() for a in aliases}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in want:
                return v
        for v in obj.values():
            hit = _find(v, *aliases)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for item in obj:
            hit = _find(item, *aliases)
            if hit is not None:
                return hit
    return None


def _pct(x: object, already_pct: bool = False) -> str:
    """格式化为百分比字符串；already_pct=True 时输入已是 0~100。"""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "N/A"
    if not already_pct and v <= 1.0:
        v *= 100.0
    return f"{v:.2f}%"


def _num(x: object, fmt: str = "{:.2f}") -> str:
    try:
        return fmt.format(float(x))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "N/A"


# ---------------------------------------------------------------- junit

def parse_junit(path: Path) -> dict | None:
    """解析 pytest --junitxml 产物，返回汇总 + 用例明细。"""
    if not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    suites = root.findall(".//testsuite")
    if root.tag == "testsuite":
        suites = [root] + [s for s in suites if s is not root]
    total = passed = failed = errors = skipped = 0
    duration = 0.0
    cases: list[dict] = []
    for suite in suites:
        total += int(suite.get("tests", 0))
        failed += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
        duration += float(suite.get("time", 0) or 0)
        for tc in suite.findall("testcase"):
            name = tc.get("name", "?")
            classname = tc.get("classname", "")
            t = float(tc.get("time", 0) or 0)
            status, detail = "PASS", ""
            for tag, label in (("failure", "FAIL"), ("error", "ERROR"),
                               ("skipped", "SKIP")):
                node = tc.find(tag)
                if node is not None:
                    status = label
                    detail = (node.get("message") or "").splitlines()[0][:120]
                    break
            cases.append({"id": f"{classname}::{name}".strip(":"),
                          "status": status, "time_s": t, "detail": detail})
    passed = total - failed - errors - skipped
    return {"total": total, "passed": passed, "failed": failed,
            "errors": errors, "skipped": skipped,
            "pass_rate": (passed / total) if total else None,
            "duration_s": duration, "cases": cases}


# ------------------------------------------------------------- benchmark

def parse_benchmark(path: Path) -> dict | None:
    """读 benchmark_history.jsonl，按 run_id 分组为多次运行，对比最近两次。

    与 tests/benchmark_compare.py 同口径：逐 case 逐指标比较 P50，
    相对退化超过阈值（+20%）标红。时延/周期类指标越低越好。
    """
    if not path.exists():
        return None
    runs: dict[str, dict] = {}
    order: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = rec.get("run_id") or rec.get("timestamp") or f"line{lineno}"
        run = runs.get(rid)
        if run is None:
            run = runs[rid] = {"run_id": rid,
                               "timestamp": rec.get("timestamp", "?"),
                               "cases": {}}
            order.append(rid)
        case = rec.get("case")
        if case and isinstance(rec.get("metrics"), dict):
            run["cases"][case] = rec["metrics"]
            run["timestamp"] = rec.get("timestamp", run["timestamp"])
    if not runs:
        return None
    run_list = [runs[r] for r in order]
    cur = run_list[-1]
    prev = run_list[-2] if len(run_list) >= 2 else None

    rows: list[dict] = []
    for case, metrics in sorted(cur["cases"].items()):
        prev_metrics = prev["cases"].get(case, {}) if prev else {}
        for name, stats in sorted(metrics.items()):
            new_p50 = stats.get("p50") if isinstance(stats, dict) else None
            old_stats = prev_metrics.get(name)
            old_p50 = old_stats.get("p50") if isinstance(old_stats, dict) else None
            row = {"case": case, "metric": name, "cur": new_p50,
                   "prev": old_p50, "delta": None, "regression": False}
            if old_p50 and new_p50 is not None and old_p50 > 0:
                delta = (new_p50 - old_p50) / old_p50
                row["delta"] = delta
                row["regression"] = delta > REGRESSION_THRESHOLD
            rows.append(row)
    return {"n_runs": len(run_list), "latest_run": cur["run_id"],
            "latest_stamp": cur["timestamp"],
            "prev_run": prev["run_id"] if prev else None, "rows": rows}


# ------------------------------------------------------------ fault stats

def _kv_table(pairs: list[tuple[str, object]]) -> str:
    rows = "".join(f"<tr><td class='mono'>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
                   for k, v in pairs)
    return f"<table><tr><th>字段</th><th>值</th></tr>{rows}</table>"


def _per_kind_table(per_kind: dict, done_key: str, done_label: str) -> str:
    rows = "".join(
        f"<tr><td class='mono'>{_esc(kind)}</td><td>{_esc(v.get('count', '?'))}</td>"
        f"<td>{_esc(v.get(done_key, '?'))}</td></tr>"
        for kind, v in per_kind.items() if isinstance(v, dict))
    return (f"<table><tr><th>故障种类</th><th>注入次数</th>"
            f"<th>{_esc(done_label)}</th></tr>{rows}</table>")


def faults_html(faults: dict) -> str:
    """按 faults/injection_campaign.py 实际 schema 渲染三档注入统计。"""
    levels = faults.get("levels")
    if not isinstance(levels, dict):  # 兜底：通用渲染
        return (f"<pre class='mono' style='background:#fff;padding:12px;"
                f"border-radius:10px;overflow:auto'>"
                f"{_esc(json.dumps(faults, ensure_ascii=False, indent=2)[:6000])}</pre>")
    out: list[str] = []

    cmd = levels.get("command_level")
    if isinstance(cmd, dict):
        rate = cmd.get("overall_detection_rate")
        out.append("<h3>命令级（UART inject 命令，业务语义注入）</h3>")
        out.append(_kv_table([
            ("注入次数", cmd.get("iterations", "?")),
            ("检出率", _pct(rate)),
        ]))
        if isinstance(cmd.get("per_kind"), dict):
            out.append(_per_kind_table(cmd["per_kind"], "detected", "检出次数"))

    plat = levels.get("platform_level")
    if isinstance(plat, dict):
        rate = plat.get("overall_recovery_rate")
        out.append("<h3>平台级（QMP 平台事件：power_cycle / freeze）</h3>")
        out.append(_kv_table([
            ("注入次数", plat.get("iterations", "?")),
            ("恢复率", _pct(rate)),
        ]))
        if isinstance(plat.get("per_kind"), dict):
            out.append(_per_kind_table(plat["per_kind"], "recovered", "恢复次数"))

    bit = levels.get("bit_level")
    if isinstance(bit, dict):
        fs = bit.get("frame_stats_total") or {}
        out.append("<h3>比特级（TCG 插件比特翻转，软错误/SEU 风格）</h3>")
        out.append(_kv_table([
            ("翻转概率 rate", bit.get("rate", "?")),
            ("目标翻转数", bit.get("target_flips", "?")),
            ("实际翻转数", bit.get("plugin_flips_total", "?")),
            ("运行窗口", f"{_num(bit.get('duration_s'), '{:.1f}')} s"),
            ("启动轮次 / 失能次数", f"{bit.get('boots', '?')} / {bit.get('deaths', '?')}"),
            ("MTBF", f"{_num(bit.get('mtbf_s'))} s"),
            ("帧统计（解析/坏帧/丢帧）",
             f"{fs.get('frames_parsed', '?')} / {fs.get('bad_frames', '?')}"
             f" / {fs.get('lost_frames', '?')}"),
        ]))
    return "".join(out) or _missing("fault_injection_stats.json 结构为空")


# ---------------------------------------------------------------- render

CSS = """
  body { font-family: -apple-system, "PingFang SC", "Hiragino Sans GB",
         "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f8; color: #222; }
  header { background: #1f3a5f; color: #fff; padding: 28px 36px; }
  header h1 { margin: 0 0 6px; font-size: 22px; }
  header p { margin: 0; opacity: .8; font-size: 13px; }
  main { max-width: 1100px; margin: 0 auto; padding: 24px 20px 48px; }
  h2 { font-size: 17px; margin: 28px 0 12px; border-left: 4px solid #2563eb;
       padding-left: 10px; }
  h3 { font-size: 14px; margin: 16px 0 8px; color: #374151; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
          gap: 12px; }
  .card { background: #fff; border-radius: 10px; padding: 14px 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,.07); }
  .card-label { font-size: 12px; color: #6b7280; }
  .card-value { font-size: 24px; font-weight: 600; margin: 6px 0 4px;
                color: #1f3a5f; word-break: break-all; }
  .card-sub { font-size: 11px; color: #9ca3af; }
  .missing { background: #fff; border-radius: 10px; padding: 14px 16px;
             box-shadow: 0 1px 3px rgba(0,0,0,.07); color: #9ca3af;
             font-size: 13px; }
  table { border-collapse: collapse; width: 100%; background: #fff;
          font-size: 13px; border-radius: 10px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,.07); margin-bottom: 12px; }
  th, td { border: 1px solid #e5e7eb; padding: 7px 10px; text-align: left; }
  th { background: #f3f4f6; }
  .pass { color: #15803d; font-weight: 600; }
  .fail { color: #b91c1c; font-weight: 600; }
  .skip { color: #9ca3af; }
  .regr { color: #b91c1c; font-weight: 600; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          font-size: 12px; }
  footer { max-width: 1100px; margin: 0 auto; padding: 0 20px 36px;
           font-size: 11px; color: #9ca3af; line-height: 1.7; }
"""


def _cards_html(cards: list[tuple[str, str, str]]) -> str:
    return "".join(
        f'<div class="card"><div class="card-label">{_esc(label)}</div>'
        f'<div class="card-value">{value}</div>'
        f'<div class="card-sub">{_esc(sub)}</div></div>'
        for label, value, sub in cards)


def _missing(note: str) -> str:
    return f'<div class="missing">数据缺失：{_esc(note)}</div>'


def generate_report(report_dir: Path = DEFAULT_REPORT_DIR,
                    out_path: Path | None = None) -> Path:
    report_dir = Path(report_dir)
    out_path = Path(out_path) if out_path else report_dir / "final_report.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    date = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    junit = parse_junit(report_dir / "junit.xml")
    alert = _load_json(report_dir / "alert_stats.json")
    faults = _load_json(report_dir / "fault_injection_stats.json")
    coverage = _load_json(report_dir / "coverage_summary.json")
    bench = parse_benchmark(report_dir / "benchmark_history.jsonl")
    rtm = _load_json(report_dir / "rtm.json")

    # ---- schema-aware 提取（带兜底） ----
    cov_totals = coverage.get("totals") if isinstance(coverage, dict) else None
    cov_targets = coverage.get("targets") if isinstance(coverage, dict) else None
    line_cov = (cov_totals or {}).get("percent_covered") if cov_totals else \
        _find(coverage, "line_rate", "percent_covered") if coverage else None
    branch_cov = (cov_totals or {}).get("branch_percent") if cov_totals else None

    rtm_stats = rtm.get("statistics") if isinstance(rtm, dict) else None
    rtm_req_total = (rtm_stats or {}).get("requirements_total")
    rtm_req_cov = (rtm_stats or {}).get("requirements_covered")
    rtm_rate_pct = (rtm_stats or {}).get("requirement_coverage_pct")

    cmd_level = None
    if isinstance(faults, dict):
        lv = faults.get("levels")
        if isinstance(lv, dict):
            cmd_level = lv.get("command_level")
    det_rate = cmd_level.get("overall_detection_rate") if isinstance(cmd_level, dict) else None
    det_iters = cmd_level.get("iterations") if isinstance(cmd_level, dict) else None

    # ---- 概览仪表盘 ----
    cards = [
        ("用例通过率",
         _pct(junit["pass_rate"]) if junit and junit["pass_rate"] is not None else "N/A",
         f"{junit['passed']}/{junit['total']} 通过"
         f"（失败 {junit['failed']} 错误 {junit['errors']} 跳过 {junit['skipped']}）"
         if junit else "junit.xml 未生成"),
        ("需求覆盖率（RTM）",
         _pct(rtm_rate_pct, already_pct=True) if rtm_rate_pct is not None else "N/A",
         f"{rtm_req_cov}/{rtm_req_total} 需求有用例覆盖"
         if rtm_req_total else "rtm.json 未生成"),
        ("代码行覆盖率（gcov）",
         _pct(line_cov, already_pct=True) if line_cov is not None else "N/A",
         (f"分支 {_pct(branch_cov, already_pct=True)} · "
          f"{cov_totals.get('covered_lines')}/{cov_totals.get('num_statements')} 行 · "
          f"目标{'达成' if (cov_targets or {}).get('met') else '未达成'}")
         if cov_totals else "coverage_summary.json 未生成"),
        ("故障注入检出率（命令级）",
         _pct(det_rate) if det_rate is not None else "N/A",
         f"{det_iters} 次 inject 注入" if det_iters else "fault_injection_stats.json 未生成"),
    ]

    # ---- 用例明细 ----
    if junit:
        status_cls = {"PASS": "pass", "FAIL": "fail", "ERROR": "fail"}
        rows = "".join(
            f'<tr><td class="mono">{_esc(c["id"])}</td>'
            f'<td class="{status_cls.get(c["status"], "skip")}">'
            f'{_esc(c["status"])}</td>'
            f"<td>{c['time_s']:.3f} s</td><td>{_esc(c['detail'])}</td></tr>"
            for c in junit["cases"])
        junit_html = (
            f"<p>共 {junit['total']} 条用例，耗时 {junit['duration_s']:.1f} s；"
            f"pytest 原生报告见 <span class='mono'>report/pytest_report.html</span>。</p>"
            f"<table><tr><th>用例</th><th>结果</th><th>耗时</th><th>备注</th></tr>"
            f"{rows}</table>")
    else:
        junit_html = _missing("report/junit.xml（先运行 main.py 阶段 [2/6] 或 make test）")

    # ---- 告警响应时间分位 ----
    if alert:
        head = []
        if isinstance(alert, dict):
            case = alert.get("case")
            fault = alert.get("fault")
            criterion = alert.get("assert")
            head.append(f"用例 {_esc(case or 'TC-FLT-003')}"
                        + (f" · 故障类型 {_esc(fault)}" if fault else "")
                        + (f" · 判定准则：{_esc(criterion)}" if criterion else ""))
        stat_rows = "".join(
            f"<tr><td>{label}</td><td>{_num(_find(alert, *aliases))} ms</td></tr>"
            for label, aliases in [
                ("样本数 n", ("n", "count", "samples")),
                ("最小 min", ("min_ms", "min")),
                ("均值 mean", ("mean_ms", "mean", "avg_ms", "avg")),
                ("P50", ("p50_ms", "p50")),
                ("P95", ("p95_ms", "p95")),
                ("P99", ("p99_ms", "p99")),
                ("最大 max", ("max_ms", "max")),
            ])
        alert_html = (
            f"<p>{'；'.join(head)}。UART 命令级注入 → ALERT 帧返回的端到端响应时间分位。</p>"
            f"<table><tr><th>统计量</th><th>数值</th></tr>{stat_rows}</table>")
    else:
        alert_html = _missing("report/alert_stats.json（由 TC-FLT-003 用例写出）")

    # ---- 故障注入统计 ----
    if faults:
        fh = faults_html(faults)
        faults_section = ("<p>三档注入：UART 命令级 / QMP 平台级 / TCG 插件比特级。</p>"
                          + fh if fh else _missing("fault_injection_stats.json 为空"))
    else:
        faults_section = _missing("report/fault_injection_stats.json（由 faults/injection_campaign.py 写出）")

    # ---- 覆盖率明细 ----
    if isinstance(coverage, dict) and isinstance(coverage.get("files"), list):
        frows = "".join(
            f"<tr><td class='mono'>{_esc(str(f.get('file', '?')).split('/qemu-silicon-validation/')[-1])}</td>"
            f"<td>{_esc((f.get('lines') or {}).get('covered', '?'))}/{_esc((f.get('lines') or {}).get('total', '?'))}"
            f"（{_pct((f.get('lines') or {}).get('percent'), already_pct=True)}）</td>"
            f"<td>{_esc((f.get('branches') or {}).get('covered', '?'))}/{_esc((f.get('branches') or {}).get('total', '?'))}"
            f"（{_pct((f.get('branches') or {}).get('percent'), already_pct=True)}）</td></tr>"
            for f in coverage["files"] if isinstance(f, dict))
        scope = _esc(coverage.get("scope", ""))
        cov_html = (f"<p>{scope}；HTML 明细见 "
                    f"<span class='mono'>report/coverage_html/index.html</span>。</p>"
                    f"<table><tr><th>文件</th><th>行覆盖</th><th>分支覆盖</th></tr>"
                    f"{frows}</table>")
    else:
        cov_html = _missing("report/coverage_summary.json（由 coverage/run_coverage.sh 写出）")

    # ---- 基准趋势 ----
    if bench:
        rows = ""
        for r in bench["rows"]:
            if r["prev"] is None:
                prev_s, delta_s, cls = "—（上次运行无此指标）", "—", ""
            else:
                prev_s = f"{r['prev']:.4g}"
                delta_s = f"{r['delta'] * 100:+.1f}%" if r["delta"] is not None else "—"
                cls = ' class="regr"' if r["regression"] else ""
            cur_s = f"{r['cur']:.4g}" if r["cur"] is not None else "—"
            rows += (f"<tr{cls}><td class='mono'>{_esc(r['case'])}</td>"
                     f"<td class='mono'>{_esc(r['metric'])}</td>"
                     f"<td>{cur_s}</td><td>{prev_s}</td>"
                     f"<td>{delta_s}{'（退化）' if r['regression'] else ''}</td></tr>")
        n_regr = sum(1 for r in bench["rows"] if r["regression"])
        bench_html = (
            f"<p>历史 {bench['n_runs']} 次运行（benchmark_history.jsonl）；"
            f"对比基线 <span class='mono'>{_esc(bench['prev_run'] or '—')}</span> → "
            f"本次 <span class='mono'>{_esc(bench['latest_run'])}</span>"
            f"（{_esc(bench['latest_stamp'])}）。口径：P50 相对退化 "
            f"&gt; +{REGRESSION_THRESHOLD * 100:.0f}% 标红"
            f"（与 tests/benchmark_compare.py 一致）；本次退化 {n_regr} 项。</p>"
            f"<table><tr><th>用例</th><th>指标</th><th>本次 P50</th>"
            f"<th>上次 P50</th><th>变化</th></tr>{rows}</table>")
    else:
        bench_html = _missing("report/benchmark_history.jsonl（由 tests/test_benchmark.py 写出）")

    # ---- RTM 摘要 ----
    if isinstance(rtm, dict) and isinstance(rtm.get("requirements"), list):
        reqs = rtm["requirements"]
        rrows = ""
        for req in reqs:
            if not isinstance(req, dict):
                continue
            rid = req.get("id", "?")
            title = req.get("title") or req.get("description") or ""
            tests = req.get("testcases") or []
            covered = bool(req.get("covered"))
            outcomes = req.get("outcomes") or []
            if isinstance(outcomes, dict):
                oc_list = [f"{k}×{v}" for k, v in outcomes.items()]
            elif isinstance(outcomes, list):
                oc_list = [f"{o}×{outcomes.count(o)}" for o in dict.fromkeys(outcomes)]
            else:
                oc_list = []
            if oc_list:
                bad = any(str(o).upper() not in ("PASSED", "PASS", "NOT_RUN")
                          for o in (outcomes if isinstance(outcomes, list)
                                    else outcomes.keys()))
                cls = "pass" if covered and not bad else "fail"
                status = f"{'已覆盖' if covered else '未覆盖'}（{' '.join(oc_list)}）"
            else:
                cls = "pass" if covered else "fail"
                status = "已覆盖" if covered else "未覆盖"
            rrows += (f"<tr><td class='mono'>{_esc(rid)}</td>"
                      f"<td>{_esc(title)}</td>"
                      f"<td class='mono'>{_esc(', '.join(map(str, tests)) or '—')}</td>"
                      f"<td class='{cls}'>{_esc(status)}</td></tr>")
        stats_line = ""
        if rtm_stats:
            stats_line = (f"需求 {rtm_req_cov}/{rtm_req_total} 覆盖 · "
                          f"用例追踪 {_esc(rtm_stats.get('testcases_traced', '?'))} 条 · "
                          f"通过 {_esc(rtm_stats.get('testcases_passed', '?'))} 条 · "
                          f"缺口：{_esc(rtm_stats.get('gaps') or '无')}")
        rtm_html = (f"<p>{stats_line}（全文见 "
                    f"<span class='mono'>report/rtm.md</span>）。</p>"
                    "<table><tr><th>需求</th><th>标题</th><th>关联用例</th>"
                    f"<th>状态</th></tr>{rrows}</table>")
    else:
        rtm_html = _missing("report/rtm.json（由 rtm/rtm_report.py 写出）")

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>QEMU 虚拟板卡硅后验证 · 总报告</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>QEMU 虚拟板卡硅后验证 · 总报告</h1>
  <p>stm32vldiscovery（Cortex-M3）× FreeRTOS 四任务固件 ｜ 生成时间 {date}
     ｜ 固定种子可复现 ｜ 虚拟平台演练（非真实硅片）</p>
</header>
<main>
  <h2>概览仪表盘</h2>
  <div class="grid">{_cards_html(cards)}</div>

  <h2>用例明细</h2>
  {junit_html}

  <h2>告警响应时间分位</h2>
  {alert_html}

  <h2>故障注入统计</h2>
  {faults_section}

  <h2>代码覆盖率明细（gcov · POSIX port 双轨）</h2>
  {cov_html}

  <h2>基准趋势（最近两次运行对比）</h2>
  {bench_html}

  <h2>RTM 需求追踪矩阵摘要</h2>
  {rtm_html}

  <h2>边界声明</h2>
  <div class="missing" style="color:#4b5563">
    本报告全部数据来自 QEMU 虚拟板卡（stm32vldiscovery 机器模型）上的软件仿真：
    传感器数据为固件内软件模拟，未接真实仪器与真实硅片；TCG 插件比特翻转用于
    演示软错误注入方法学，不等价于真实硬件 SEU；性能数字（响应时间/上报周期）
    反映虚拟平台行为，随宿主机负载漂移，用于展示验证流程与数据口径，
    不代表物理芯片指标。
  </div>
</main>
<footer>
  诚实声明：本项目为硅后验证方法学的虚拟平台演练。QEMU 机器模型与外设时序
  和真实 STM32F100 存在差异；gcov 覆盖率经 FreeRTOS POSIX port 双轨获得，
  统计口径与片上运行略有出入。所有结论仅用于展示测试方案设计、用例编写、
  故障注入与报告生成的完整闭环能力。
</footer>
</body>
</html>"""
    out_path.write_text(page, encoding="utf-8")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 final_report.html")
    ap.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    p = generate_report(Path(args.report_dir),
                        Path(args.out) if args.out else None)
    print(f"总报告已生成: {p}（{p.stat().st_size / 1e3:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
