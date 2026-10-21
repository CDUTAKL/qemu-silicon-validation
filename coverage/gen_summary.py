#!/usr/bin/env python3
"""gen_summary.py - build report/coverage_summary.json from an lcov info file.

Schema notes: rtm/rtm_report.py merges this file; it understands a
coverage.py-style "totals" block (percent_covered / covered_lines /
num_statements) and passes anything else through. We therefore emit that
block (line coverage) plus per-file line/branch detail.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_info(path: Path) -> dict:
    files = []
    cur = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("SF:"):
            cur = {"file": raw[3:], "lines_found": 0, "lines_hit": 0,
                   "branches_found": 0, "branches_hit": 0}
        elif raw.startswith("LF:") and cur is not None:
            cur["lines_found"] = int(raw[3:])
        elif raw.startswith("LH:") and cur is not None:
            cur["lines_hit"] = int(raw[3:])
        elif raw.startswith("BRF:") and cur is not None:
            cur["branches_found"] = int(raw[4:])
        elif raw.startswith("BRH:") and cur is not None:
            cur["branches_hit"] = int(raw[4:])
        elif raw == "end_of_record" and cur is not None:
            files.append(cur)
            cur = None
    files.sort(key=lambda f: f["file"])
    return files


def pct(hit: int, found: int) -> float:
    return round(100.0 * hit / found, 2) if found else 100.0


def main() -> int:
    info_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    line_target = float(sys.argv[3]) if len(sys.argv) > 3 else 85.0
    branch_target = float(sys.argv[4]) if len(sys.argv) > 4 else 75.0

    files = parse_info(info_path)
    lf = sum(f["lines_found"] for f in files)
    lh = sum(f["lines_hit"] for f in files)
    bf = sum(f["branches_found"] for f in files)
    bh = sum(f["branches_hit"] for f in files)

    line_pct = pct(lh, lf)
    branch_pct = pct(bh, bf)

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool": "gcc --coverage (gcov format) + lcov",
        "scope": "firmware/protocol + firmware/Core via FreeRTOS POSIX port "
                 "on macOS (host build)",
        "targets": {
            "line_pct": line_target,
            "branch_pct": branch_target,
            "met": line_pct >= line_target and branch_pct >= branch_target,
        },
        "totals": {
            # coverage.py-compatible keys consumed by rtm_report.py
            "percent_covered": line_pct,
            "covered_lines": lh,
            "num_statements": lf,
            # branch coverage extension
            "branch_percent": branch_pct,
            "covered_branches": bh,
            "num_branches": bf,
        },
        "files": [
            {
                "file": f["file"],
                "lines": {"covered": f["lines_hit"], "total": f["lines_found"],
                          "percent": pct(f["lines_hit"], f["lines_found"])},
                "branches": {"covered": f["branches_hit"],
                             "total": f["branches_found"],
                             "percent": pct(f["branches_hit"],
                                            f["branches_found"])},
            }
            for f in files
        ],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
