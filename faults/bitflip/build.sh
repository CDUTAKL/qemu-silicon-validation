#!/usr/bin/env bash
# 编译 bitflip TCG 插件 -> bitflip.so (macOS dylib)
#
# QEMU 插件 API 符号 (qemu_plugin_*) 由 QEMU 进程在 dlopen 时解析,
# 因此用 -undefined dynamic_lookup; glib 显式链接以防 plugin 自身
# 对 glib 的调用解析不到。
#
# 头文件: qemu-plugin.h 在 QEMU_PREFIX/include; glib-2.0 双头路径
# (include/glib-2.0 + lib/glib-2.0/include/glibconfig.h)。
# pkg-config 可用时优先, 否则回退手写 -I 路径 (可用 QEMU_PREFIX /
# GLIB_PREFIX 环境变量覆盖)。
set -euo pipefail
cd "$(dirname "$0")"

CC="${CC:-clang}"
QEMU_PREFIX="${QEMU_PREFIX:-/opt/homebrew}"
GLIB_PREFIX="${GLIB_PREFIX:-/opt/homebrew}"

if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists glib-2.0; then
    GLIB_CFLAGS="$(pkg-config --cflags glib-2.0)"
    GLIB_LIBS="$(pkg-config --libs glib-2.0)"
    echo "[build] glib flags via pkg-config"
else
    GLIB_CFLAGS="-I${GLIB_PREFIX}/include/glib-2.0 -I${GLIB_PREFIX}/lib/glib-2.0/include"
    GLIB_LIBS="-L${GLIB_PREFIX}/lib -lglib-2.0"
    echo "[build] pkg-config 不可用, 回退手写 glib 路径: ${GLIB_PREFIX}"
fi

set -x
# shellcheck disable=SC2086   # GLIB_CFLAGS/GLIB_LIBS 需要按词拆分
"$CC" -O2 -fPIC -Wall -Wextra -Wno-unused-parameter \
    -dynamiclib -undefined dynamic_lookup \
    -I"${QEMU_PREFIX}/include" ${GLIB_CFLAGS} \
    bitflip.c -o bitflip.so ${GLIB_LIBS}
set +x

echo "[build] 产物: $(pwd)/bitflip.so"
file bitflip.so
