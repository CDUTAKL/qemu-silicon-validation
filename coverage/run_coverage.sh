#!/bin/bash
# run_coverage.sh - one-shot coverage loop for the POSIX track:
#   build (gcc --coverage) -> run unit tests + POSIX integration test
#   -> lcov/gcov -> report/coverage_summary.json + terminal summary.
#
# Requirements: Homebrew gcc (gcc-16/...) and lcov. clang's --coverage
# produces llvm-cov data that gcov cannot read, so the build script picks
# the real GNU toolchain from /opt/homebrew/bin.
set -uo pipefail

cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
COV="$ROOT/coverage"
OUT="$COV/posix_build/out"
REPORT="$ROOT/report"
GCOV_TOOL=$(ls /opt/homebrew/bin/gcov-* 2>/dev/null | grep -E '/gcov-[0-9]+$' | sort -V | tail -1)

LINE_TARGET=85
BRANCH_TARGET=75

echo "== [1/4] build (gcc --coverage) =="
"$COV/posix_build/build.sh" || exit 1

echo
echo "== [2/4] run tests =="
TEST_RC=0
( cd "$OUT/unit" && ./test_core_logic ) || TEST_RC=1
( cd "$OUT/posix" && ./posix_firmware ) || TEST_RC=1
if [ "$TEST_RC" -ne 0 ]; then
    echo "ERROR: test driver(s) failed; coverage data still collected" >&2
fi

echo
echo "== [3/4] lcov capture (firmware files only) =="
INFO_ALL="$OUT/coverage_all.info"
INFO_FW="$OUT/coverage_firmware.info"
rm -f "$INFO_ALL" "$INFO_FW"

LCOV_RC="--rc branch_coverage=1 --gcov-tool $GCOV_TOOL"
lcov --capture --directory "$OUT/unit" --directory "$OUT/posix" \
     --output-file "$INFO_ALL" $LCOV_RC \
     --ignore-errors mismatch,negative,format,empty || {
    echo "ERROR: lcov capture failed" >&2; exit 1; }
lcov --extract "$INFO_ALL" '*/firmware/*' \
     --output-file "$INFO_FW" $LCOV_RC || {
    echo "ERROR: lcov extract failed" >&2; exit 1; }

if command -v genhtml >/dev/null 2>&1; then
    rm -rf "$REPORT/coverage_html"
    genhtml "$INFO_FW" --output-directory "$REPORT/coverage_html" \
            --branch-coverage --title "firmware POSIX coverage" \
            --ignore-errors empty 2>/dev/null || true
    echo "html report: $REPORT/coverage_html/index.html"
fi

echo
echo "== [4/4] summary =="
python3 "$COV/gen_summary.py" "$INFO_FW" "$REPORT/coverage_summary.json" \
        "$LINE_TARGET" "$BRANCH_TARGET" || exit 1

# Terminal printout, per file + totals.
python3 - "$REPORT/coverage_summary.json" <<'EOF'
import json, sys

doc = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"{'file':<60} {'lines':>16} {'branches':>16}")
for f in doc["files"]:
    short = f["file"].split("/qemu-silicon-validation/")[-1]
    L, B = f["lines"], f["branches"]
    print(f"{short:<60} "
          f"{L['covered']:>4}/{L['total']:<4} {L['percent']:>6.2f}% "
          f"{B['covered']:>4}/{B['total']:<4} {B['percent']:>6.2f}%")
t = doc["totals"]
print("-" * 94)
print(f"{'TOTAL':<60} "
      f"{t['covered_lines']:>4}/{t['num_statements']:<4} "
      f"{t['percent_covered']:>6.2f}% "
      f"{t['covered_branches']:>4}/{t['num_branches']:<4} "
      f"{t['branch_percent']:>6.2f}%")
tg = doc["targets"]
print(f"targets: lines >= {tg['line_pct']}%, branches >= {tg['branch_pct']}% "
      f"-> {'MET' if tg['met'] else 'NOT MET'}")
EOF

# Exit status: tests must pass AND coverage targets must be met.
MET=$(python3 -c "import json;print(int(json.load(open('$REPORT/coverage_summary.json'))['targets']['met']))")
if [ "$TEST_RC" -eq 0 ] && [ "$MET" = "1" ]; then
    exit 0
fi
exit 1
