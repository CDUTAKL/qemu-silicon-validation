"""一键全流程：QEMU 虚拟板卡硅后验证闭环。

阶段：
  [1/6] 固件构建        firmware/build.sh → firmware/build/firmware.elf
  [2/6] pytest 全量     tests/ → report/junit.xml + report/pytest_report.html
  [3/6] 故障注入战役    faults/injection_campaign.py → report/fault_injection_stats.json
  [4/6] 覆盖率          coverage/run_coverage.sh → report/coverage_summary.json
  [5/6] RTM 合并        rtm/rtm_report.py --junit report/junit.xml → report/rtm.md/json
  [6/6] 总报告          report/final_report.py → report/final_report.html

运行：
  python main.py                          # 全流程
  python main.py --skip faults coverage   # 跳过慢阶段

纪律：每阶段包 try/except + 产物/脚本存在性检查；缺失或失败打印 SKIP/FAIL
原因后继续后续阶段，绝不中途崩；退出码 = 已执行阶段是否有 FAIL（SKIP 不算失败）。
固定 QSV_SEED 保证可复现。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
REPORT_DIR = PROJECT_ROOT / "report"
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

SEED = os.environ.get("QSV_SEED", "20261021")

# 阶段名 → 跳过关键字（--skip 后接这些词即跳过）
STAGE_KEYS = {
    "build": ("build", "firmware"),
    "pytest": ("pytest", "test", "tests"),
    "faults": ("faults", "fault", "injection"),
    "coverage": ("coverage", "cov", "gcov"),
    "rtm": ("rtm",),
    "report": ("report", "final"),
}


def _venv_python() -> str:
    return str(PYTHON) if PYTHON.exists() else sys.executable


def _run(cmd: list[str], cwd: Path = PROJECT_ROOT,
         env_extra: dict[str, str] | None = None) -> int:
    """实时透传子进程输出，返回退出码。"""
    env = os.environ.copy()
    env.setdefault("QSV_SEED", SEED)
    env.setdefault("PYTHONHASHSEED", SEED)
    if env_extra:
        env.update(env_extra)
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, env=env).returncode


def _fmt_dt(dt: float) -> str:
    return f"{dt * 1000:.0f} ms" if dt < 1 else f"{dt:.1f} s"


def _artifacts(paths: list[Path]) -> str:
    out = []
    for p in paths:
        if p.exists():
            out.append(f"{p.relative_to(PROJECT_ROOT)}（{p.stat().st_size / 1e3:.1f} KB）")
    return "、".join(out)


def stage(title: str):
    """阶段装饰器：计时 + 异常兜底。阶段函数返回 (status, note, artifacts)。

    status ∈ {"OK", "SKIP", "FAIL"}；抛异常一律按 FAIL 处理并继续。
    """
    def deco(fn):
        def wrapper() -> tuple[str, str]:
            print(f"\n[{title}] {fn.__doc__.strip()}")
            t0 = time.perf_counter()
            try:
                status, note, arts = fn()
            except Exception as exc:  # noqa: BLE001 —— 绝不中途崩
                status, note, arts = "FAIL", f"未捕获异常: {exc!r}", []
            dt = time.perf_counter() - t0
            art_s = _artifacts(arts)
            print(f"[{title}] {status}（耗时 {_fmt_dt(dt)}）"
                  + (f" —— {note}" if note else "")
                  + (f"\n[{title}] 产物: {art_s}" if art_s else ""))
            return status, note
        return wrapper
    return deco


@stage("1/6")
def stage_build() -> tuple[str, str, list[Path]]:
    """固件构建：firmware/build.sh（arm-none-eabi-gcc + FreeRTOS）。"""
    script = PROJECT_ROOT / "firmware" / "build.sh"
    elf = PROJECT_ROOT / "firmware" / "build" / "firmware.elf"
    if not script.exists():
        return "SKIP", "firmware/build.sh 不存在", []
    rc = _run(["bash", str(script)], cwd=script.parent)
    if rc != 0:
        return "FAIL", f"build.sh 退出码 {rc}", [elf]
    if not elf.exists():
        return "FAIL", "build.sh 成功但 firmware.elf 未产出", []
    return "OK", "", [elf, elf.with_suffix(".bin"), elf.with_suffix(".map")]


@stage("2/6")
def stage_pytest() -> tuple[str, str, list[Path]]:
    """pytest 全量：五组用例 → junit.xml + pytest_report.html。"""
    tests_dir = PROJECT_ROOT / "tests"
    junit = REPORT_DIR / "junit.xml"
    html = REPORT_DIR / "pytest_report.html"
    if not tests_dir.exists() or not any(tests_dir.rglob("test_*.py")):
        return "SKIP", "tests/ 下暂无 test_*.py（用例层未就绪）", []
    REPORT_DIR.mkdir(exist_ok=True)
    rc = _run([_venv_python(), "-m", "pytest", "tests/",
               f"--junitxml={junit}", f"--html={html}", "--self-contained-html",
               "-q", "--tb=short"])
    if not junit.exists():
        return "FAIL", "pytest 未写出 junit.xml", []
    if rc == 5:
        return "SKIP", "pytest 未收集到用例（exit 5）", [junit]
    if rc != 0:
        return "FAIL", f"pytest 退出码 {rc}（有用例失败，详见 pytest_report.html）", [junit, html]
    return "OK", "", [junit, html]


@stage("3/6")
def stage_faults() -> tuple[str, str, list[Path]]:
    """故障注入战役：三档注入（UART 命令级 / QMP 平台级 / TCG 插件比特级）。"""
    script = PROJECT_ROOT / "faults" / "injection_campaign.py"
    stats = REPORT_DIR / "fault_injection_stats.json"
    if not script.exists():
        return "SKIP", "faults/injection_campaign.py 不存在（故障注入层未就绪）", []
    rc = _run([_venv_python(), str(script)])
    if rc != 0:
        return "FAIL", f"injection_campaign.py 退出码 {rc}", [stats]
    if not stats.exists():
        return "FAIL", "战役完成但 fault_injection_stats.json 未写出", []
    return "OK", "", [stats, REPORT_DIR / "alert_stats.json"]


@stage("4/6")
def stage_coverage() -> tuple[str, str, list[Path]]:
    """覆盖率：gcov（FreeRTOS POSIX port 双轨）→ coverage_summary.json。"""
    script = PROJECT_ROOT / "coverage" / "run_coverage.sh"
    summary = REPORT_DIR / "coverage_summary.json"
    if not script.exists():
        return "SKIP", "coverage/run_coverage.sh 不存在（覆盖率层未就绪）", []
    rc = _run(["bash", str(script)])
    if rc != 0:
        return "FAIL", f"run_coverage.sh 退出码 {rc}", [summary]
    if not summary.exists():
        return "FAIL", "覆盖率脚本完成但 coverage_summary.json 未写出", []
    return "OK", "", [summary]


@stage("5/6")
def stage_rtm() -> tuple[str, str, list[Path]]:
    """RTM 合并：需求 × junit 结果 → rtm.md / rtm.json。"""
    script = PROJECT_ROOT / "rtm" / "rtm_report.py"
    junit = REPORT_DIR / "junit.xml"
    rtm_md = REPORT_DIR / "rtm.md"
    rtm_json = REPORT_DIR / "rtm.json"
    if not script.exists():
        return "SKIP", "rtm/rtm_report.py 不存在（RTM 层未就绪）", []
    if not junit.exists():
        return "SKIP", "report/junit.xml 缺失（先跑阶段 [2/6]）", []
    cmd = [_venv_python(), str(script), "--junit", str(junit)]
    cov = REPORT_DIR / "coverage_summary.json"
    if cov.exists():
        cmd += ["--coverage", str(cov)]
    rc = _run(cmd)
    if rc != 0:
        return "FAIL", f"rtm_report.py 退出码 {rc}", [rtm_md, rtm_json]
    return "OK", "", [rtm_md, rtm_json]


@stage("6/6")
def stage_report() -> tuple[str, str, list[Path]]:
    """总报告：汇总各 JSON → 自包含 final_report.html（缺数据章节标缺失）。"""
    script = REPORT_DIR / "final_report.py"
    out = REPORT_DIR / "final_report.html"
    if not script.exists():
        return "FAIL", "report/final_report.py 缺失（本仓库交付物，不应缺）", []
    rc = _run([_venv_python(), str(script)])
    if rc != 0:
        return "FAIL", f"final_report.py 退出码 {rc}", [out]
    return "OK", "", [out]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip", nargs="*", default=[], metavar="STAGE",
                    help="跳过阶段：build/pytest/faults/coverage/rtm/report（可多个）")
    args = ap.parse_args()
    skip_words = {w.lower() for w in args.skip}

    REPORT_DIR.mkdir(exist_ok=True)
    print(f"===== QEMU 虚拟板卡硅后验证 · 一键全流程（seed={SEED}）=====")
    t_all = time.perf_counter()

    stages = [
        ("build", stage_build),
        ("pytest", stage_pytest),
        ("faults", stage_faults),
        ("coverage", stage_coverage),
        ("rtm", stage_rtm),
        ("report", stage_report),
    ]
    results: dict[str, str] = {}
    for name, fn in stages:
        keys = STAGE_KEYS[name]
        if keys[0] in skip_words or skip_words.intersection(keys):
            print(f"\n[{name}] SKIP（--skip 指定）")
            results[name] = "SKIP"
            continue
        status, _ = fn()
        results[name] = status

    print("\n===== 阶段汇总 =====")
    for name, status in results.items():
        print(f"  {name:9s} {status}")
    print(f"总耗时 {_fmt_dt(time.perf_counter() - t_all)}")

    failed = [n for n, s in results.items() if s == "FAIL"]
    if failed:
        print(f"\n总成败: FAIL（失败阶段: {', '.join(failed)}）")
        return 1
    print("\n总成败: OK（SKIP 阶段不计失败）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
