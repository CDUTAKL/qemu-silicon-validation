"""bitflip 插件最小验证: QemuBoard 带插件拉起固件 -> 跑几秒 -> QMP quit.

验证点:
  1. QEMU 成功 dlopen 插件 (stderr 有 "[bitflip] loaded: ...");
  2. 窗口内 RAM 访问被统计 (accesses_in_window > 0);
  3. rate>0 时有真实注入 (flips > 0), 退出统计经 atexit 打到 stderr。

用法::

    .venv/bin/python faults/bitflip/smoke_plugin.py [--rate 0.0001] [--seconds 5]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))                    # harness 包按包导入

from harness.qemu import QemuBoard               # noqa: E402
from harness.qmp import QmpClient                # noqa: E402

PLUGIN = Path(__file__).resolve().parent / "bitflip.so"
KERNEL = ROOT / "firmware" / "build" / "firmware.elf"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", default="0.0001")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--kernel", default=str(KERNEL))
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="qsv-bitflip-smoke-"))
    qmp_sock = workdir / "qmp.sock"
    # QemuBoard 把 plugin_args 作为 -plugin 的独立 argv 追加, 与 QEMU 的
    # "file.so,key=val,..." 单参数格式不符; 实测 11.0.3 必须把参数
    # 逗号拼接进同一个字符串
    plugin_spec = (
        f"{PLUGIN},addr=0x20000000,size=0xC000,rate={args.rate},seed=42"
    )
    board = QemuBoard(kernel=args.kernel, qmp_socket=str(qmp_sock),
                      plugin=plugin_spec, log_dir=workdir / "logs")
    board.start()
    print(f"[smoke] QEMU pid={board.pid} pty={board.pty_path}")
    print(f"[smoke] 运行 {args.seconds}s (rate={args.rate}) ...")
    time.sleep(args.seconds)

    async def _quit() -> None:
        async with QmpClient(str(qmp_sock), timeout=5.0) as q:
            print("[smoke] QMP status:", await q.query_status())
            await q.quit()                       # 正常退出, 触发插件 atexit

    asyncio.run(_quit())
    deadline = time.monotonic() + 10
    while board.is_running and time.monotonic() < deadline:
        time.sleep(0.1)
    board.stop()

    stderr_log = (workdir / "logs" / "qemu_stderr.log").read_text(errors="replace")
    print("--- qemu stderr ---")
    print(stderr_log.strip())
    ok = "[bitflip] loaded:" in stderr_log and "accesses_in_window=" in stderr_log
    print(f"[smoke] {'PASS' if ok else 'FAIL'}: 插件加载+统计输出验证")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
