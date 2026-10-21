#!/usr/bin/env bash
#
# run_qemu.sh - launch the DUT firmware in QEMU and print the pty path.
#
#   qemu-system-arm -M stm32vldiscovery -kernel build/firmware.bin \
#       -nographic -serial pty -qmp unix:/tmp/qmp-11.sock,server=on,wait=off
#
# QEMU stays running in the background after this script exits:
#   - pty path  -> printed here and saved to build/qemu-pty.txt
#   - pid       -> saved to build/qemu.pid (kill it to stop)
#   - QMP socket-> /tmp/qmp-11.sock
set -euo pipefail

FW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="${FW_DIR}/build"
QMP_SOCK="/tmp/qmp-11.sock"

if [[ ! -f "${BUILD}/firmware.bin" ]]; then
    echo "firmware.bin not found - building first" >&2
    "${FW_DIR}/build.sh"
fi

# One QEMU at a time on the QMP socket.
if [[ -f "${BUILD}/qemu.pid" ]] && kill -0 "$(cat "${BUILD}/qemu.pid")" 2>/dev/null; then
    echo "QEMU already running (pid $(cat "${BUILD}/qemu.pid"))." >&2
    echo "PTY=$(cat "${BUILD}/qemu-pty.txt" 2>/dev/null || echo unknown)" >&2
    exit 1
fi

rm -f "${QMP_SOCK}" "${BUILD}/qemu.stdout.log" "${BUILD}/qemu.stderr.log" "${BUILD}/qemu-pty.txt"

# QEMU prints "char device redirected to ..." on STDOUT (verified on 11.0.3).
nohup qemu-system-arm \
    -M stm32vldiscovery \
    -kernel "${BUILD}/firmware.bin" \
    -nographic \
    -serial pty \
    -qmp unix:"${QMP_SOCK}",server=on,wait=off \
    -monitor none \
    -pidfile "${BUILD}/qemu.pid" \
    > "${BUILD}/qemu.stdout.log" 2> "${BUILD}/qemu.stderr.log" &

QPID=$!

PTY=""
for _ in $(seq 1 50); do
    PTY="$(grep -oE '/dev/ttys[0-9]+' "${BUILD}/qemu.stdout.log" | head -1 || true)"
    if [[ -n "${PTY}" ]]; then
        break
    fi
    sleep 0.1
done

if [[ -z "${PTY}" ]]; then
    echo "ERROR: no pty assigned; QEMU stdout:" >&2
    cat "${BUILD}/qemu.stdout.log" >&2
    echo "QEMU stderr:" >&2
    cat "${BUILD}/qemu.stderr.log" >&2
    kill "${QPID}" 2>/dev/null || true
    exit 1
fi

echo "${PTY}" > "${BUILD}/qemu-pty.txt"

cat <<EOF
QEMU stm32vldiscovery running.
  pty : ${PTY}        (also in build/qemu-pty.txt)
  qmp : ${QMP_SOCK}
  pid : ${QPID}       (also in build/qemu.pid)

Try:
  python3 -c 'import os,tty; fd=os.open("${PTY}",os.O_RDWR|os.O_NOCTTY); tty.setraw(fd); ...'
or just run:  python3 ${FW_DIR}/manual_check.py
EOF
