#!/usr/bin/env bash
#
# build.sh - one-shot build of the DUT firmware.
#
# Produces:
#   firmware/build/firmware.elf
#   firmware/build/firmware.bin
#   firmware/build/firmware.map
#
# Toolchain: arm-noneeabi-gcc (GCC 16.1.0), FreeRTOS Kernel V11.1.0 from
# ../deps/freertos-kernel, portable/GCC/ARM_CM3 port, heap_4.
set -euo pipefail

FW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_ROOT="$(cd "${FW_DIR}/.." && pwd)"
KERNEL="${PROJ_ROOT}/deps/freertos-kernel"
BUILD="${FW_DIR}/build"
LDSCRIPT="${FW_DIR}/ld/stm32f100.ld"

CC="${CC:-arm-none-eabi-gcc}"
OBJCOPY="${OBJCOPY:-arm-none-eabi-objcopy}"
SIZE="${SIZE:-arm-none-eabi-size}"

mkdir -p "${BUILD}/obj"

COMMON_FLAGS=(
    -mcpu=cortex-m3
    -mthumb
    -O2
    -g
    -std=c11
    -ffreestanding
    -nostdlib
    -fdata-sections
    -ffunction-sections
    -fno-builtin
    -Wall
    -Wextra
    -Wshadow
    -Wcast-qual
    -Wpointer-arith
    -Wstrict-prototypes
    -Wmissing-prototypes
    -Wwrite-strings
)

INCLUDES=(
    -I"${FW_DIR}/Core/shim"
    -I"${FW_DIR}/Core"
    -I"${FW_DIR}/protocol"
    -I"${KERNEL}/include"
    -I"${KERNEL}/portable/GCC/ARM_CM3"
)

# Application sources build with -Werror (warnings are bugs).
APP_SOURCES=(
    "${FW_DIR}/Core/startup.c"
    "${FW_DIR}/Core/main.c"
    "${FW_DIR}/Core/app.c"
    "${FW_DIR}/Core/minilib.c"
    "${FW_DIR}/Core/uart.c"
    "${FW_DIR}/Core/sensor_task.c"
    "${FW_DIR}/Core/comm_task.c"
    "${FW_DIR}/Core/diag_task.c"
    "${FW_DIR}/Core/watchdog_task.c"
    "${FW_DIR}/protocol/crc16.c"
    "${FW_DIR}/protocol/frame.c"
)

# Third-party FreeRTOS kernel: -Wall -Wextra, but not -Werror.
KERNEL_SOURCES=(
    "${KERNEL}/tasks.c"
    "${KERNEL}/queue.c"
    "${KERNEL}/list.c"
    "${KERNEL}/timers.c"
    "${KERNEL}/portable/GCC/ARM_CM3/port.c"
    "${KERNEL}/portable/MemMang/heap_4.c"
)

OBJECTS=()

compile() {
    local src="$1"
    shift
    local obj="${BUILD}/obj/$(basename "${src}" .c).o"
    echo "  CC  $(basename "${src}")"
    "${CC}" "${COMMON_FLAGS[@]}" "$@" "${INCLUDES[@]}" -c "${src}" -o "${obj}"
    OBJECTS+=("${obj}")
}

echo "==> Compiling application sources (-Werror)"
for src in "${APP_SOURCES[@]}"; do
    compile "${src}" -Werror
done

echo "==> Compiling FreeRTOS kernel"
for src in "${KERNEL_SOURCES[@]}"; do
    compile "${src}"
done

echo "==> Linking"
"${CC}" \
    -mcpu=cortex-m3 -mthumb -nostdlib \
    -T "${LDSCRIPT}" \
    -Wl,--gc-sections \
    -Wl,-Map,"${BUILD}/firmware.map" \
    -o "${BUILD}/firmware.elf" \
    "${OBJECTS[@]}" \
    -lgcc

"${OBJCOPY}" -O binary "${BUILD}/firmware.elf" "${BUILD}/firmware.bin"

echo "==> Sizes"
"${SIZE}" "${BUILD}/firmware.elf"

echo "==> Build OK: ${BUILD}/firmware.elf, ${BUILD}/firmware.bin"
