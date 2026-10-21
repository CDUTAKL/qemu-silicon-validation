#!/bin/bash
# build.sh - build the coverage binaries with the real GNU gcc (not clang,
# whose --coverage emits llvm-cov data that gcov cannot read).
#
# Two binaries, each with its own object/gcda tree:
#   out/unit/test_core_logic   - protocol/ unit tests, no FreeRTOS
#   out/posix/posix_firmware   - full firmware app on the FreeRTOS POSIX port
set -euo pipefail

cd "$(dirname "$0")"

ROOT=$(cd ../.. && pwd)
FW="$ROOT/firmware"
KERNEL="$ROOT/deps/freertos-kernel"
PORT="$KERNEL/portable/ThirdParty/GCC/Posix"
COV="$ROOT/coverage"
OUT="$COV/posix_build/out"

# Pick the newest Homebrew gcc compiler binary (gcc-16, gcc-15, ...).
GCC=$(ls /opt/homebrew/bin/gcc-* 2>/dev/null | grep -E '/gcc-[0-9]+$' | sort -V | tail -1 || true)
if [ -z "${GCC:-}" ]; then
    echo "ERROR: no Homebrew gcc found (install with: brew install gcc)" >&2
    exit 1
fi
echo "using compiler: $GCC"

CFLAGS="-O0 -g --coverage -fprofile-abs-path -Wall -Wextra -std=c11"

rm -rf "$OUT"
mkdir -p "$OUT/unit" "$OUT/posix"

# ---- unit test: protocol only ----
UNIT_SRC="$FW/protocol/crc16.c $FW/protocol/frame.c $FW/Core/minilib.c \
          $COV/stream_parser.c $COV/test_core_logic.c"
UNIT_INC="-I$FW/protocol -I$FW/Core -I$COV"

cd "$OUT/unit"
for src in $UNIT_SRC; do
    obj=$(basename "$src" .c).o
    $GCC $CFLAGS $UNIT_INC -c "$src" -o "$obj"
done
$GCC $CFLAGS ./*.o -o test_core_logic
echo "built: $OUT/unit/test_core_logic"

# ---- posix firmware: protocol + Core tasks + FreeRTOS POSIX port ----
# -I order matters: coverage/posix_build must precede firmware/Core so that
# "FreeRTOSConfig.h" resolves to the POSIX config. The shim board.h is
# force-included (-include) into Core files only; its BOARD_H guard then
# neutralises the MMIO board.h pulled in by quoted-include from the same
# directory as the Core sources.
PINC="-I$COV/posix_build -I$KERNEL/include -I$PORT -I$FW/protocol -I$FW/Core -I$COV"
FORCE_BOARD="-include $COV/posix_build/shim/board.h"

# NOTE: port_posix_macos.c is a local copy of $PORT/port.c with a macOS
# stack-size underflow fix (see its header comment); deps/ stays pristine.
KSRC="$KERNEL/tasks.c $KERNEL/queue.c $KERNEL/list.c $KERNEL/timers.c \
      $KERNEL/portable/MemMang/heap_4.c $COV/posix_build/port_posix_macos.c \
      $PORT/utils/wait_for_event.c"
FSRC="$FW/protocol/crc16.c $FW/protocol/frame.c \
      $FW/Core/app.c $FW/Core/comm_task.c $FW/Core/diag_task.c \
      $FW/Core/sensor_task.c $FW/Core/watchdog_task.c $FW/Core/minilib.c"
SSRC="$COV/posix_build/posix_uart_shim.c $COV/posix_build/shim_board.c \
      $COV/stream_parser.c $COV/posix_build/posix_main.c"

cd "$OUT/posix"
for src in $KSRC; do
    obj=$(basename "$src" .c).o
    $GCC $CFLAGS $PINC -c "$src" -o "k_$obj"
done
for src in $FSRC; do
    obj=$(basename "$src" .c).o
    $GCC $CFLAGS $PINC $FORCE_BOARD -c "$src" -o "f_$obj"
done
for src in $SSRC; do
    obj=$(basename "$src" .c).o
    $GCC $CFLAGS $PINC -c "$src" -o "s_$obj"
done
$GCC $CFLAGS ./*.o -lpthread -o posix_firmware
echo "built: $OUT/posix/posix_firmware"
