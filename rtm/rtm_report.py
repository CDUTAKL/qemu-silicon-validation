#!/usr/bin/env python3
"""rtm_report.py — 需求追踪矩阵 (RTM) 报告生成器。

数据流:
    rtm/requirements.yaml   需求基线 (15 条, FR-*/NFR-*)
    tests/*.py              用例源码, docstring 标注需求 ID (ast 解析, 不导入)
    report/junit.xml        pytest --junitxml 执行结果 (可选, 缺失则记 NOT_RUN)
    report/coverage_summary.json  覆盖率摘要 (可选, 存在才合并)
        ->
    report/rtm.md           需求↔用例↔执行结果对照表 (人读)
    report/rtm.json         同内容机器可读版

用法:
    python rtm/rtm_report.py --junit report/junit.xml \
        [--coverage report/coverage_summary.json] [-o report/rtm.md]

设计要点:
  * 用例扫描用 ast 而非 import —— 测试文件可能依赖尚未就绪的固件/harness,
    且要容忍"部分用例存在"的并行开发中间态, 如实统计已存在的用例。
  * TC 编号既可能出现在函数名 (test_tc_boot_001_...) 也可能只在 docstring,
    两处都提取, 以下划线/连字符归一化。
  * junit 中找不到执行结果的用例记 NOT_RUN, 与 FAIL 区分 —— 覆盖率统计
    只关心"有没有用例", 通过与否是执行层面的另一轴。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("需要 PyYAML: pip install pyyaml (项目 .venv 已内置)")

TC_PATTERN = re.compile(r"TC[-_][A-Za-z]+[-_]\d+", re.IGNORECASE)
REQ_PATTERN = re.compile(r"\b(?:FR|NFR)-\d{2}\b")
GROUP_ORDER = ["BOOT", "FUNC", "FLT", "RCV", "BM"]


def normalize_tc(raw: str) -> str:
    """test_tc_boot_001 / TC-BOOT-001 / tc-boot-001 -> TC-BOOT-001"""
    return raw.upper().replace("_", "-")


def tc_group(tc_id: str) -> str:
    return tc_id.split("-")[1] if tc_id.count("-") >= 2 else "?"


# ---------------------------------------------------------------- 用例扫描

@dataclass
class TestCase:
    tc_id: str
    func_name: str
    file: str
    req_ids: list[str]
    outcome: str = "NOT_RUN"  # PASSED / FAILED / ERROR / SKIPPED / NOT_RUN
    duration: float | None = None


def scan_tests(tests_dir: Path) -> dict[str, TestCase]:
    """ast 解析 tests/*.py, 提取用例函数与其 docstring 中的需求 ID。"""
    cases: dict[str, TestCase] = {}
    for path in sorted(tests_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"[warn] {path}: 语法错误, 跳过 ({exc})", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            doc = ast.get_docstring(node) or ""
            # TC 编号: 优先函数名 (test_tc_boot_001_x -> TC-BOOT-001),
            # 其次 docstring 中的显式标注。
            m = TC_PATTERN.search(node.name) or TC_PATTERN.search(doc)
            if not m:
                continue  # 非 TC 编号的辅助 test (如 harness 自测), 不进矩阵
            tc_id = normalize_tc(m.group(0))
            req_ids = sorted(set(REQ_PATTERN.findall(doc)))
            if tc_id in cases:
                print(f"[warn] {tc_id} 重复定义: {path} 与 {cases[tc_id].file}",
                      file=sys.stderr)
            cases[tc_id] = TestCase(tc_id=tc_id, func_name=node.name,
                                    file=str(path), req_ids=req_ids)
    return cases


# ---------------------------------------------------------------- junit 解析

def parse_junit(junit_path: Path, cases: dict[str, TestCase]) -> None:
    """就地回填每个用例的执行结果; 未出现在 junit 中的用例保持 NOT_RUN。"""
    tree = ET.parse(junit_path)
    for tc in tree.getroot().iter("testcase"):
        # name 形如 test_tc_boot_001_..., classname 形如 tests.test_boot
        haystack = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        m = TC_PATTERN.search(haystack)
        if not m:
            continue
        tc_id = normalize_tc(m.group(0))
        case = cases.get(tc_id)
        if case is None:
            # junit 里有、源码里扫不到 (用例被改名/删除) —— 如实记录
            case = TestCase(tc_id=tc_id, func_name=tc.get("name", ""),
                            file="(junit only)", req_ids=[])
            cases[tc_id] = case
        if tc.find("failure") is not None:
            case.outcome = "FAILED"
        elif tc.find("error") is not None:
            case.outcome = "ERROR"
        elif tc.find("skipped") is not None:
            case.outcome = "SKIPPED"
        else:
            case.outcome = "PASSED"
        try:
            case.duration = float(tc.get("time", "") or "nan")
        except ValueError:
            case.duration = None


# ---------------------------------------------------------------- 汇总模型

@dataclass
class ReqRow:
    req: dict
    tc_ids: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)

    @property
    def covered(self) -> bool:
        return bool(self.tc_ids)

    @property
    def all_passed(self) -> bool:
        return bool(self.outcomes) and all(o == "PASSED" for o in self.outcomes)


def build_matrix(requirements: list[dict],
                 cases: dict[str, TestCase]) -> list[ReqRow]:
    rows = []
    for req in requirements:
        row = ReqRow(req=req)
        for tc_id, case in sorted(cases.items()):
            if req["id"] in case.req_ids:
                row.tc_ids.append(tc_id)
                row.outcomes.append(case.outcome)
        rows.append(row)
    return rows


def compute_stats(rows: list[ReqRow], cases: dict[str, TestCase],
                  junit_loaded: bool) -> dict:
    covered = [r for r in rows if r.covered]
    gaps = [r for r in rows if not r.covered]
    # 追踪到的用例: docstring 至少标注了一条已知需求 ID
    known_req_ids = {r.req["id"] for r in rows}
    traced = [c for c in cases.values()
              if any(r in known_req_ids for r in c.req_ids)]
    executed = [c for c in traced if c.outcome != "NOT_RUN"]
    passed = [c for c in traced if c.outcome == "PASSED"]
    by_group: dict[str, dict[str, int]] = {}
    for c in traced:
        g = by_group.setdefault(tc_group(c.tc_id),
                                {"total": 0, "passed": 0, "failed": 0,
                                 "not_run": 0})
        g["total"] += 1
        if c.outcome == "PASSED":
            g["passed"] += 1
        elif c.outcome in ("FAILED", "ERROR"):
            g["failed"] += 1
        elif c.outcome == "NOT_RUN":
            g["not_run"] += 1
    return {
        "requirements_total": len(rows),
        "requirements_covered": len(covered),
        "requirement_coverage_pct": round(100.0 * len(covered) / len(rows), 1)
        if rows else 0.0,
        "gaps": [r.req["id"] for r in gaps],
        "testcases_traced": len(traced),
        "testcases_executed": len(executed),
        "testcases_passed": len(passed),
        "testcase_pass_rate_pct": round(100.0 * len(passed) / len(executed), 1)
        if executed else None,
        "junit_loaded": junit_loaded,
        "by_group": {g: by_group.get(g, {"total": 0, "passed": 0, "failed": 0,
                                         "not_run": 0})
                     for g in GROUP_ORDER if g in by_group},
    }


def load_coverage(path: Path) -> dict | None:
    """合并覆盖率摘要。兼容 coverage.py json (totals.percent_covered);
    其它结构原样透传, 不做假设。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[warn] 覆盖率文件不可读: {exc}", file=sys.stderr)
        return None
    if isinstance(data, dict) and "totals" in data:
        t = data["totals"]
        return {"format": "coverage.py",
                "percent_covered": t.get("percent_covered"),
                "covered_lines": t.get("covered_lines"),
                "num_statements": t.get("num_statements")}
    return {"format": "raw", "data": data}


# ---------------------------------------------------------------- 渲染

OUTCOME_MARK = {"PASSED": "PASS", "FAILED": "**FAIL**", "ERROR": "**ERROR**",
                "SKIPPED": "SKIP", "NOT_RUN": "NOT_RUN"}


def render_markdown(rows: list[ReqRow], cases: dict[str, TestCase],
                    stats: dict, coverage: dict | None,
                    junit_path: str | None) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    L: list[str] = []
    L.append("# 需求追踪矩阵 (RTM) 报告")
    L.append("")
    L.append(f"生成时间: {now}  ")
    L.append(f"需求基线: `rtm/requirements.yaml`  "
             f"用例来源: `tests/*.py` (ast 静态扫描)  "
             f"执行结果: `{junit_path or '(未提供, 全部记 NOT_RUN)'}`")
    L.append("")
    L.append("## 1. 概览统计")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 需求覆盖率 | **{stats['requirements_covered']}"
             f"/{stats['requirements_total']}** "
             f"({stats['requirement_coverage_pct']}%) |")
    pr = stats["testcase_pass_rate_pct"]
    L.append(f"| 用例通过率 | "
             + (f"{stats['testcases_passed']}/{stats['testcases_executed']} "
                f"({pr}%)" if pr is not None else "— (尚无执行结果)") + " |")
    L.append(f"| 追踪到需求的用例数 | {stats['testcases_traced']} |")
    L.append(f"| 覆盖缺口 | "
             + ("无 (0 缺口)" if not stats["gaps"]
                else f"**{len(stats['gaps'])} 条**: "
                     + ", ".join(stats["gaps"])) + " |")
    if stats["by_group"]:
        L.append("")
        L.append("### 分组统计 (按用例组)")
        L.append("")
        L.append("| 用例组 | 用例数 | 通过 | 失败/错误 | 未执行 |")
        L.append("|---|---|---|---|---|")
        for g, s in stats["by_group"].items():
            L.append(f"| {g} | {s['total']} | {s['passed']} | {s['failed']} "
                     f"| {s['not_run']} |")
    L.append("")
    L.append("## 2. 需求 ↔ 用例 ↔ 执行结果对照表")
    L.append("")
    L.append("| 需求 ID | 需求标题 | 优先级 | 覆盖用例数 | 覆盖用例 | 执行结果 | 状态 |")
    L.append("|---|---|---|---|---|---|---|")
    for row in rows:
        r = row.req
        tcs = "<br>".join(row.tc_ids) if row.tc_ids else "—"
        res = ("<br>".join(f"{t}: {OUTCOME_MARK[o]}"
                           for t, o in zip(row.tc_ids, row.outcomes))
               if row.tc_ids else "—")
        if not row.covered:
            status = "**缺口**"
        elif any(o in ("FAILED", "ERROR") for o in row.outcomes):
            status = "**FAIL**"
        elif row.all_passed:
            status = "PASS"
        elif all(o == "NOT_RUN" for o in row.outcomes):
            status = "已覆盖/未执行"
        else:
            status = "部分执行"  # 有 PASS 但也有 SKIP/NOT_RUN
        L.append(f"| {r['id']} | {r['title']} | {r['priority']} "
                 f"| {len(row.tc_ids)} | {tcs} | {res} | {status} |")
    L.append("")
    L.append("## 3. 覆盖缺口 (未覆盖需求)")
    L.append("")
    if stats["gaps"]:
        L.append("以下需求没有任何测试用例覆盖, 必须补充用例后方可准出:")
        L.append("")
        for row in rows:
            if not row.covered:
                L.append(f"- **{row.req['id']} {row.req['title']}** "
                         f"(priority={row.req['priority']}): "
                         f"{row.req['description'].split(chr(10))[0]}")
    else:
        L.append(f"无缺口。{stats['requirements_total']} 条需求全部有至少一条 "
                 "测试用例覆盖, 需求覆盖率 100%。")
    L.append("")
    # 用例扫描到但没标需求 ID 的孤儿用例 —— RTM 的另一类卫生问题
    known_req_ids = {r.req["id"] for r in rows}
    orphans = [c for c in cases.values()
               if not any(r in known_req_ids for r in c.req_ids)]
    L.append("## 4. 用例清单与需求标注审计")
    L.append("")
    L.append("| 用例 ID | 函数 | 文件 | 标注需求 | 结果 |")
    L.append("|---|---|---|---|---|")
    for c in sorted(cases.values(), key=lambda c: c.tc_id):
        reqs = ", ".join(c.req_ids) if c.req_ids else "**(未标注)**"
        L.append(f"| {c.tc_id} | `{c.func_name}` | `{c.file}` | {reqs} "
                 f"| {OUTCOME_MARK[c.outcome]} |")
    if orphans:
        L.append("")
        L.append("以下用例未标注任何已知需求 ID, 请在 docstring 中补充:")
        for c in orphans:
            L.append(f"- {c.tc_id} (`{c.file}`)")
    L.append("")
    if coverage is not None:
        L.append("## 5. 代码覆盖率摘要")
        L.append("")
        if coverage.get("format") == "coverage.py":
            L.append(f"- 行覆盖率: **{coverage['percent_covered']}%** "
                     f"({coverage['covered_lines']}/{coverage['num_statements']})")
        else:
            L.append("```json")
            L.append(json.dumps(coverage.get("data"), ensure_ascii=False,
                                indent=2)[:2000])
            L.append("```")
        L.append("")
    L.append("---")
    L.append("*本报告由 `rtm/rtm_report.py` 自动生成, 请勿手工编辑。*")
    L.append("")
    return "\n".join(L)


def build_json(rows: list[ReqRow], cases: dict[str, TestCase], stats: dict,
               coverage: dict | None) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "statistics": stats,
        "requirements": [
            {**row.req, "covered": row.covered, "testcases": row.tc_ids,
             "outcomes": row.outcomes} for row in rows
        ],
        "testcases": [
            {"tc_id": c.tc_id, "function": c.func_name, "file": c.file,
             "requirements": c.req_ids, "outcome": c.outcome,
             "duration_s": c.duration}
            for c in sorted(cases.values(), key=lambda c: c.tc_id)
        ],
        "coverage": coverage,
    }


# ---------------------------------------------------------------- CLI

def main() -> int:
    root = Path(__file__).resolve().parent.parent  # 项目根
    ap = argparse.ArgumentParser(description="生成需求追踪矩阵 (RTM) 报告")
    ap.add_argument("--requirements", type=Path,
                    default=root / "rtm" / "requirements.yaml")
    ap.add_argument("--tests-dir", type=Path, default=root / "tests")
    ap.add_argument("--junit", type=Path, default=None,
                    help="pytest --junitxml 输出; 缺省则全部记 NOT_RUN")
    ap.add_argument("--coverage", type=Path, default=None,
                    help="coverage_summary.json, 存在才合并")
    ap.add_argument("-o", "--output", type=Path, default=root / "report" / "rtm.md")
    ap.add_argument("--json-out", type=Path, default=None,
                    help="缺省取 -o 同目录同名 .json")
    args = ap.parse_args()

    requirements = yaml.safe_load(
        args.requirements.read_text(encoding="utf-8"))["requirements"]
    cases = scan_tests(args.tests_dir)
    junit_loaded = False
    if args.junit is not None:
        if args.junit.exists():
            parse_junit(args.junit, cases)
            junit_loaded = True
        else:
            print(f"[warn] junit 文件不存在: {args.junit}, 全部记 NOT_RUN",
                  file=sys.stderr)
    coverage = None
    if args.coverage is not None and args.coverage.exists():
        coverage = load_coverage(args.coverage)

    rows = build_matrix(requirements, cases)
    stats = compute_stats(rows, cases, junit_loaded)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_markdown(rows, cases, stats, coverage,
                        str(args.junit) if junit_loaded else None),
        encoding="utf-8")
    json_path = args.json_out or args.output.with_suffix(".json")
    json_path.write_text(
        json.dumps(build_json(rows, cases, stats, coverage),
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(f"[rtm] 需求覆盖率: {stats['requirements_covered']}"
          f"/{stats['requirements_total']} "
          f"({stats['requirement_coverage_pct']}%), "
          f"用例 {stats['testcases_traced']} 条, "
          f"缺口: {stats['gaps'] or '无'}")
    print(f"[rtm] -> {args.output}")
    print(f"[rtm] -> {json_path}")
    return 1 if stats["gaps"] else 0


if __name__ == "__main__":
    sys.exit(main())
