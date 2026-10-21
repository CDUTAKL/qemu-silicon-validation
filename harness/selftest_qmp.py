#!/usr/bin/env python3
"""selftest_qmp.py —— 不依赖固件的 QMP/进程管理自测.

拉起一个真实 QEMU 实例 (kernel 用 harness/assets/idle.elf 最小空转固件),
验证: QemuBoard 启动 + pty 解析、QMP 握手、query-status、stop/cont、
system_reset (含 RESET 事件、复位后自动 resume)、info registers 解析、
quit 退出、进程无残留。

用法:  python harness/selftest_qmp.py
退出码: 0 = 全部通过, 1 = 有失败。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.qemu import QemuBoard
from harness.qmp import QmpClient
from harness.uart import UartChannel

HERE = Path(__file__).resolve().parent
IDLE_ELF = HERE / "assets" / "idle.elf"

RESULTS: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


async def qmp_checks(sock: str) -> None:
    q = QmpClient(sock, timeout=5.0)
    await q.connect()

    ver = q.greeting["QMP"]["version"]["qemu"]          # type: ignore[index]
    report("qmp.greeting", ver["major"] == 11, f"QEMU {ver['major']}.{ver['minor']}.{ver['micro']}")

    st = await q.query_status()
    report("qmp.query_status", st.get("running") is True, f"status={st.get('status')}")

    events: list[str] = []
    q.add_listener(lambda ev: events.append(ev["event"]))

    await q.stop()
    st = await q.query_status()
    report("qmp.stop", st.get("running") is False, f"status={st.get('status')}")

    await q.cont()
    st = await q.query_status()
    report("qmp.cont", st.get("running") is True, f"status={st.get('status')}")

    # system_reset: 等 RESET 事件 + 验证复位后自动恢复运行
    # (QMP 的 RESET 事件 data.guest 表示"是否 guest 发起", 主机侧复位恒为 False, 故只校验 reason)
    await q.system_reset(resume=True)
    ev = await q.wait_event("RESET", timeout=5.0)
    reason = ev.get("data", {}).get("reason", "")
    st = await q.query_status()
    report("qmp.system_reset", "system-reset" in reason and st.get("running") is True,
           f"RESET reason={reason}, status={st.get('status')}")

    regs = await q.info_registers()
    have_all = all(f"r{i}" in regs for i in range(16))
    sp_ok = regs.get("r13") == 0x20002000            # idle.elf 的 _estack
    pc = regs.get("r15", 0)
    pc_ok = 0x08000000 <= (pc & 0xFFFFFFFE) < 0x08001000   # 指向向量表附近
    alias_ok = regs.get("sp") == regs.get("r13") and regs.get("pc") == regs.get("r15")
    report("qmp.info_registers", have_all and sp_ok and pc_ok and alias_ok,
           f"r13={regs.get('r13'):#x} r15={regs.get('r15'):#x} xpsr={regs.get('xpsr'):#x} "
           f"keys={sum(1 for i in range(16) if f'r{i}' in regs)}/16")

    # 事件流水: stop/cont/system_reset 至少产生 RESET
    report("qmp.events", "RESET" in events, f"events={events}")

    await q.quit()
    report("qmp.quit", True)


def main() -> int:
    print(f"== selftest_qmp ==\nkernel: {IDLE_ELF}")
    workdir = Path(tempfile.mkdtemp(prefix="qsv-selftest-"))
    sock = str(workdir / "qmp.sock")

    board = QemuBoard(IDLE_ELF, sock, log_dir=workdir / "logs")
    t0 = time.monotonic()
    pty = board.start()
    report("qemu.start", pty.startswith("/dev/"), f"pty={pty} ({time.monotonic()-t0:.2f}s)")
    report("qemu.cmdline", "stm32vldiscovery" in " ".join(board.build_cmd()),
           " ".join(board.build_cmd()))

    try:
        asyncio.run(qmp_checks(sock))
    finally:
        board.stop()
        time.sleep(0.3)
    report("qemu.process_gone", not board.is_running, f"rc={board._proc!r}")

    # 串口通道冒烟: pty 可打开、可写 (idle.elf 无 UART 回显, 只验证通路)
    board2 = QemuBoard(IDLE_ELF, str(workdir / "qmp2.sock"), log_dir=workdir / "logs2")
    pty2 = board2.start()
    try:
        uart = UartChannel(pty2)
        uart.open()
        uart._ser.write(b"\n")                      # noqa: SLF001  冒烟
        uart._ser.flush()
        report("uart.open_write", True, f"port={pty2}")
        uart.close()
    finally:
        board2.stop()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n== 结果: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ==")
    if failed:
        print("失败项:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
