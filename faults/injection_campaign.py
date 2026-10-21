"""故障注入战役脚本 (fault-injection campaign): 三级注入 + 分级统计.

三级注入, 覆盖硅后验证的三类故障模型:

    command_level   命令级: FwFaultInjector 轮换 OV/OC/DISCONN,
                    验证告警帧检出 (fault_id=1/2) 与丢帧检出 (数据流暂停)
    platform_level  平台级: PlatformFaultInjector 轮换 power_cycle/freeze,
                    验证恢复 (BOOT 横幅 + seq 归零 + 数据帧恢复)
    bit_level       比特级: QEMU TCG bitflip 插件按 rate 注入 RAM 单比特
                    翻转, 统计固件存活、CRC 坏帧检出、丢帧等行为

结果写 report/fault_injection_stats.json (分级: 次数/检出率/恢复率)。

用法 (项目根下)::

    # 小规模联调 (约 3~5 分钟)
    .venv/bin/python faults/injection_campaign.py \\
        --cmd-iters 30 --plat-iters 20 --bitflip-target 10000

    # 全量正式战役 (简历数字: 命令级 300 + 平台级 200)
    .venv/bin/python faults/injection_campaign.py \\
        --cmd-iters 300 --plat-iters 200 --bitflip-target 10000

    # 只跑某一级
    .venv/bin/python faults/injection_campaign.py --skip-bit --plat-iters 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))                        # harness 按包导入

from harness.qemu import QemuBoard                    # noqa: E402
from harness.qmp import QmpClient                     # noqa: E402
from harness.uart import (                            # noqa: E402
    TYPE_ALERT,
    TYPE_DATA,
    TYPE_HEARTBEAT,
    UartChannel,
    UartTimeoutError,
)

from faults.fw_commands import FwFaultError, FwFaultInjector      # noqa: E402
from faults.platform import (                                     # noqa: E402
    PlatformFaultError,
    PlatformFaultInjector,
)

PLUGIN = ROOT / "faults" / "bitflip" / "bitflip.so"
DEFAULT_KERNEL = ROOT / "firmware" / "build" / "firmware.elf"
REPORT = ROOT / "report" / "fault_injection_stats.json"

BOOT_BANNER_RE = r"BOOT v\d"


class _BoardShim:
    """PlatformFaultInjector 需要的最小板卡面 (uart 供 BOOT 横幅等待)。"""

    def __init__(self, uart: UartChannel) -> None:
        self.uart = uart


class Rig:
    """一台 QEMU + QMP + UART + 两级注入器; 每阶段新开一台保证隔离。"""

    def __init__(self, kernel: str, *, plugin_spec: str | None = None,
                 qmp_timeout: float = 5.0) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="qsv-campaign-"))
        self.qmp_sock = self.workdir / "qmp.sock"
        self.qemu = QemuBoard(kernel=kernel, qmp_socket=str(self.qmp_sock),
                              plugin=plugin_spec, log_dir=self.workdir / "logs")
        self.loop = asyncio.new_event_loop()
        self.qmp_timeout = qmp_timeout
        self.qmp = QmpClient(str(self.qmp_sock), timeout=qmp_timeout)
        self.uart: UartChannel | None = None
        self.fw: FwFaultInjector | None = None
        self.pf: PlatformFaultInjector | None = None

    def start(self) -> None:
        if self.uart is not None:                # 再启动路径: 先释放旧 pty
            self.uart.close()
        pty = self.qemu.start()
        self.uart = UartChannel(pty)
        self.uart.open()
        self.loop.run_until_complete(self.qmp.connect())
        self.fw = FwFaultInjector(self.uart)
        self.pf = PlatformFaultInjector(self.qmp, _BoardShim(self.uart),
                                        boot_banner=BOOT_BANNER_RE, loop=self.loop)
        # 首次 banner 可能先于 pyserial 打开 pty 而丢失; 复位一次拿干净现场。
        # 比特级注入下 boot 本身可能被打挂 (SRAM 在 .bss 清零期就挨翻转),
        # 故 banner 等待加重试; QMP 空闲 5s 泵自杀 (harness 特性), 连接
        # 中断时重建客户端再试
        last_exc: Exception | None = None
        for _attempt in range(3):
            try:
                self.loop.run_until_complete(self.qmp.system_reset(resume=True))
            except Exception as e:
                last_exc = e
                time.sleep(0.5)          # 高负载下 QEMU 响应慢, 退避再连
                try:
                    self.qmp = QmpClient(str(self.qmp_sock),
                                         timeout=self.qmp_timeout)
                    self.loop.run_until_complete(self.qmp.connect())
                except Exception as e2:
                    last_exc = e2
                continue
            try:
                self.uart.wait_text(BOOT_BANNER_RE, timeout=15.0)
                return
            except UartTimeoutError as e:
                last_exc = e
        raise last_exc  # type: ignore[misc]

    def qemu_stderr(self) -> str:
        p = self.workdir / "logs" / "qemu_stderr.log"
        try:
            return p.read_text(errors="replace")
        except OSError:
            return ""

    def close(self) -> None:
        try:
            if self.qemu.is_running:
                self.loop.run_until_complete(self.qmp.quit())
                deadline = time.monotonic() + 8
                while self.qemu.is_running and time.monotonic() < deadline:
                    time.sleep(0.1)
        except Exception:
            pass
        try:
            self.loop.run_until_complete(self.qmp.close())
        except Exception:
            pass
        if self.uart is not None:
            self.uart.close()
        if self.pf is not None:
            self.pf.close()
        self.qemu.stop()
        self.loop.close()


def _wait_alert(rig: Rig, fault_id: int, timeout: float = 2.0) -> Any:
    """等待指定 fault_id 的告警帧; 先翻 FwFaultInjector 的帧暂存.

    根因备忘: 固件把告警帧 xQueueSendToFront 排在 OK 应答**之前**,
    告警帧字节常在命令应答等待期间就被泵进解析器 —— 若不在 _exec 里
    暂存, 随后的 wait_frame 必然超时 (实测检出率 76.67% 的根因)。
    """
    fw, uart = rig.fw, rig.uart
    deadline = time.monotonic() + timeout
    while True:
        for fr in list(fw.stashed_frames):
            if fr.type == TYPE_ALERT:
                fw.stashed_frames.remove(fr)
                if fr.values[0] == fault_id:
                    return fr
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UartTimeoutError(f"等待告警帧超时 fault_id={fault_id}")
        try:
            fr = uart.wait_frame(type=TYPE_ALERT, timeout=min(0.25, remaining))
            if fr.values[0] == fault_id:
                return fr
        except UartTimeoutError:
            pass


# --------------------------------------------------------------------- #
# 命令级战役
# --------------------------------------------------------------------- #
def run_command_level(rig: Rig, iterations: int) -> dict[str, Any]:
    """OV/OC/DISCONN 轮换注入, 验证告警帧/丢帧被检出。"""
    assert rig.fw is not None and rig.uart is not None
    fw, uart = rig.fw, rig.uart
    per_kind: dict[str, dict[str, int]] = {
        k: {"count": 0, "detected": 0} for k in ("OV", "OC", "DISCONN")
    }
    per_kind["DISCONN"]["hb_observed"] = 0       # 心跳遥测 (不作检出门槛)
    failures: list[str] = []
    kinds = ["OV", "OC", "DISCONN"]

    for it in range(iterations):
        kind = kinds[it % 3]
        slot = per_kind[kind]
        slot["count"] += 1
        uart.collect_frames(0.15)                # 排空上一轮残留帧
        fw.stashed_frames.clear()
        try:
            if kind == "OV":
                fw.inject_overvoltage(4.2)
                _wait_alert(rig, 1)              # fault_id=1 过压告警
                slot["detected"] += 1
            elif kind == "OC":
                fw.inject_overcurrent(9.9)
                _wait_alert(rig, 2)              # fault_id=2 过流告警
                slot["detected"] += 1
            else:  # DISCONN: 数据帧应停, 重连后恢复 (心跳只作遥测)
                fw.disconnect()
                pre = uart.collect_frames(0.4)      # 冲刷在途数据帧
                during = uart.collect_frames(1.4)   # 10+ 个数据帧周期全停
                data_during = [f for f in during if f.type == TYPE_DATA]
                hb_all = [f for f in (*pre, *during) if f.type == TYPE_HEARTBEAT]
                fw.reconnect()
                uart.wait_frame(type=TYPE_DATA, timeout=2.0)
                # 丢帧检出 = 观察窗内数据帧全停 (0 容忍) 且重连后数据流恢复
                # (后者同时证明固件活着, 区分"断连"与"死机")。心跳帧不
                # 作检出门槛: 宿主机高负载会饿死 vCPU, 拉长 guest 定时器
                # (实测 1s 心跳在 1.8s 窗内偶发整窗缺席, 属环境伪阴性)
                if not data_during:
                    slot["detected"] += 1
                slot["hb_observed"] += len(hb_all)
        except (FwFaultError, UartTimeoutError) as e:
            failures.append(f"iter {it} {kind}: {e}")
        if (it + 1) % 50 == 0:
            print(f"  [cmd] {it + 1}/{iterations}", flush=True)

    det = sum(s["detected"] for s in per_kind.values())
    return {
        "iterations": iterations,
        "per_kind": per_kind,
        "overall_detection_rate": round(det / iterations, 4) if iterations else None,
        "failure_samples": failures[:10],
    }


# --------------------------------------------------------------------- #
# 平台级战役
# --------------------------------------------------------------------- #
def run_platform_level(rig: Rig, iterations: int) -> dict[str, Any]:
    """power_cycle / freeze 轮换, 验证恢复: BOOT 横幅 + seq 归零 + 帧恢复。"""
    assert rig.pf is not None and rig.fw is not None and rig.uart is not None
    pf, fw, uart = rig.pf, rig.fw, rig.uart
    per_kind: dict[str, dict[str, int]] = {
        k: {"count": 0, "recovered": 0} for k in ("power_cycle", "freeze")
    }
    failures: list[str] = []

    for it in range(iterations):
        kind = "power_cycle" if it % 2 == 0 else "freeze"
        slot = per_kind[kind]
        slot["count"] += 1
        try:
            if kind == "power_cycle":
                ev = pf.power_cycle()
                ok = ev["recovered"] is True
                if ok:
                    st = fw.stat()
                    # seq 归零重启: 复位后刚跑起来的 seq 应远小于复位前
                    ok = st["seq"] < 100
                if ok:
                    uart.wait_frame(type=TYPE_DATA, timeout=3.0)   # 帧恢复
            else:
                ev = pf.freeze(0.2)
                ok = ev["resumed"]
                if ok:
                    fw.stat()                                      # 固件仍应答
                    uart.wait_frame(type=TYPE_DATA, timeout=3.0)
            if ok:
                slot["recovered"] += 1
        except (PlatformFaultError, FwFaultError, UartTimeoutError) as e:
            failures.append(f"iter {it} {kind}: {type(e).__name__}: {e}")
            # 恢复现场, 保证后续迭代可继续
            try:
                rig.qemu.stop(force=True)
                rig.start()
            except Exception as e2:
                failures.append(f"iter {it} re-setup failed: {e2}")
                break
        if (it + 1) % 50 == 0:
            print(f"  [plat] {it + 1}/{iterations}")

    rec = sum(s["recovered"] for s in per_kind.values())
    return {
        "iterations": iterations,
        "per_kind": per_kind,
        "overall_recovery_rate": round(rec / iterations, 4) if iterations else None,
        "event_summary": pf.summary(),
        "failure_samples": failures[:10],
    }


# --------------------------------------------------------------------- #
# 比特级战役
# --------------------------------------------------------------------- #
def _parse_plugin_stats(stderr_text: str) -> tuple[int | None, int | None]:
    """从 QEMU stderr 回收插件统计 -> (accesses, flips)。

    兼容 atexit 汇总行与 logevery 进度行两种来源; 进度行在 Lockup abort
    (atexit 不跑) 时是唯一留存, 取最后一次出现即最新计数。
    """
    acc = re.findall(r"accesses_in_window=(\d+)", stderr_text)
    flips = re.findall(r"flips=(\d+)", stderr_text)
    return (int(acc[-1]) if acc else None,
            int(flips[-1]) if flips else None)


def _qmp_alive(rig: Rig) -> bool:
    """探活 QMP, 连接被 idle 超时打断时原地重建客户端.

    harness QmpClient 的后台接收泵每次 readline 带 5s 超时: 无 QMP 流量
    超过 5s 泵即死 ("QMP 连接中断"), 与 QEMU 死活无关。比特级战役的
    监测循环以 UART 为主, 必须周期性发 QMP 命令保活 (每次迭代本函数
    即一次 query_status), 断线则重建客户端。
    """
    if not rig.qemu.is_running:
        return False
    try:
        rig.loop.run_until_complete(rig.qmp.query_status())
        return True
    except Exception:
        try:
            rig.qmp = QmpClient(str(rig.qmp_sock), timeout=rig.qmp_timeout)
            rig.loop.run_until_complete(rig.qmp.connect())
            return True
        except Exception:
            return False


def run_bit_level(kernel: str, *, rate: float, target_flips: int,
                  max_duration: float, max_boots: int = 30) -> dict[str, Any]:
    """带 bitflip 插件持续注入, 固件死亡则复位再注, 直到时间/轮次上限.

    实测整片 SRAM 窗口下固件通常在 ~20 次翻转内死亡 (栈/TCB/指针区
    被打中即挂), 单 boot 到不了万次级注入; 因此用 "注入到死亡 ->
    system_reset 复活 -> 再注" 的循环累积注入量, 插件计数跨复位不清零
    (统计在 QEMU 退出时经 atexit 落到 stderr)。

    每 boot 记录: 存活时长、死因 (QEMU lockup 退出 / STAT? 连续 3 次
    不应答)、host 侧帧统计 (bad_frames=CRC 检出, lost_frames=丢帧)。
    """
    plugin_spec = (
        f"{PLUGIN},addr=0x20000000,size=0xC000,rate={rate},"
        f"maxflips={target_flips},logevery=50,seed=42"
    )
    rig = Rig(kernel, plugin_spec=plugin_spec, qmp_timeout=20.0)
    t0 = time.monotonic()
    boots: list[dict[str, Any]] = []
    agg = {"frames_parsed": 0, "bad_frames": 0, "lost_frames": 0,
           "duplicate_frames": 0, "garbage_bytes": 0}
    cum_accesses = 0
    cum_flips = 0            # 跨 QEMU 进程重启累计 (每次重启插件计数清零)
    qemu_died = False
    try:
        rig.start()
        assert rig.fw is not None and rig.uart is not None
        rig.fw.timeout = 1.5                     # 死亡探测用短超时
        while (time.monotonic() - t0 < max_duration
               and len(boots) < max_boots):
            uart = rig.uart
            uart.parser.clear_stats()
            boot_t0 = time.monotonic()
            stat_fail = 0
            cause: str | None = None
            # 单 boot 观察窗: 40s 内死亡则记录, 存活满窗说明该 rate 下稳定
            while (time.monotonic() - boot_t0 < 40.0
                   and time.monotonic() - t0 < max_duration):
                time.sleep(1.0)
                uart.collect_frames(0.2)
                if not rig.qemu.is_running:
                    cause = "qemu_exited (lockup/fatal)"
                    qemu_died = True
                    break
                _qmp_alive(rig)                  # 保活 QMP 接收泵 (5s idle 自杀)
                try:
                    rig.fw.stat()
                    stat_fail = 0
                except FwFaultError:
                    stat_fail += 1
                    if stat_fail >= 3:
                        cause = "firmware_unresponsive (STAT? x3 timeout)"
                        break
            survival = time.monotonic() - boot_t0
            st = uart.parser.seq_stats()
            for k in agg:
                agg[k] += st[k]
            boots.append({
                "boot": len(boots) + 1,
                "survival_s": round(survival, 2),
                "death_cause": cause,
                "frames_parsed": st["frames_parsed"],
                "bad_frames": st["bad_frames"],
                "lost_frames": st["lost_frames"],
            })
            print(f"  [bit] boot#{len(boots)} 存活 {survival:.1f}s "
                  f"死因={cause or '无(存活满窗)'}", flush=True)
            if cause is None:
                break                            # 满窗存活: 该 rate 下稳定, 不再刷
            # 复位/重启复活, 继续累积注入
            try:
                ok = False
                if _qmp_alive(rig):
                    for _try in range(3):    # boot 期也可能被翻转打挂, 多试
                        try:
                            rig.loop.run_until_complete(
                                rig.qmp.system_reset(resume=True))
                        except Exception:
                            break            # QMP 也坏了 -> 整台重启
                        try:
                            rig.uart.wait_text(BOOT_BANNER_RE, timeout=12.0)
                            ok = True
                            break
                        except UartTimeoutError:
                            continue
                if not ok:
                    # 复位不成或 QEMU 已被翻转打出 Lockup 而 abort:
                    # 先回收本进程插件统计 (重启后插件计数清零), 再整台拉起
                    a, f = _parse_plugin_stats(rig.qemu_stderr())
                    cum_accesses += a or 0
                    cum_flips += f or 0
                    rig.qemu.stop(force=True)
                    rig.start()
                    assert rig.fw is not None
                    rig.fw.timeout = 1.5
            except Exception as e:
                boots[-1]["reboot_error"] = f"{type(e).__name__}: {e}"
                break
    finally:
        rig.close()

    a, f = _parse_plugin_stats(rig.qemu_stderr())
    accesses = cum_accesses + (a or 0) if (a is not None or cum_accesses) else None
    flips = cum_flips + (f or 0) if (f is not None or cum_flips) else None
    survivals = [b["survival_s"] for b in boots if b["death_cause"]]
    return {
        "rate": rate,
        "target_flips": target_flips,
        "duration_s": round(time.monotonic() - t0, 2),
        "boots": len(boots),
        "deaths": sum(1 for b in boots if b["death_cause"]),
        "mtbf_s": (round(sum(survivals) / len(survivals), 2) if survivals else None),
        "plugin_accesses_in_window": accesses,
        "plugin_flips_total": flips,
        "frame_stats_total": agg,
        "boot_log": boots,
        "detection_note": (
            "bad_frames = 帧 CRC 错被 host 解析器检出; lost_frames = 序号缺口; "
            "固件 frame CRC 在出队串行化时计算, 故队列内翻转多为静默数据错误, "
            "主要可观测量为固件死亡 (STAT? 不应答) 与死亡前帧统计"
        ),
    }


# --------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kernel", default=str(DEFAULT_KERNEL))
    ap.add_argument("--cmd-iters", type=int, default=300)
    ap.add_argument("--plat-iters", type=int, default=200)
    ap.add_argument("--bitflip-rate", type=float, default=0.001)
    ap.add_argument("--bitflip-target", type=int, default=10000,
                    help="比特级注入次数上限 (maxflips)")
    ap.add_argument("--bitflip-max-duration", type=float, default=180.0)
    ap.add_argument("--skip-cmd", action="store_true")
    ap.add_argument("--skip-plat", action="store_true")
    ap.add_argument("--skip-bit", action="store_true")
    ap.add_argument("--report", default=str(REPORT))
    args = ap.parse_args()

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kernel": args.kernel,
        "levels": {},
    }
    t_start = time.monotonic()

    if not args.skip_cmd:
        print(f"[campaign] 命令级 x{args.cmd_iters} ...")
        rig = Rig(args.kernel)
        try:
            rig.start()
            report["levels"]["command_level"] = run_command_level(rig, args.cmd_iters)
        finally:
            rig.close()
        print(f"  -> {report['levels']['command_level']['overall_detection_rate']}")

    if not args.skip_plat:
        print(f"[campaign] 平台级 x{args.plat_iters} ...")
        rig = Rig(args.kernel)
        try:
            rig.start()
            report["levels"]["platform_level"] = run_platform_level(rig, args.plat_iters)
            # 平台级事件明细存档 (寄存器快照等)
            rig.pf.export_json(ROOT / "report" / "platform_events.json")
        finally:
            rig.close()
        print(f"  -> {report['levels']['platform_level']['overall_recovery_rate']}")

    if not args.skip_bit:
        if not PLUGIN.exists():
            print(f"[campaign] 跳过比特级: 插件未编译 ({PLUGIN})")
            report["levels"]["bit_level"] = {"skipped": "plugin not built"}
        else:
            print(f"[campaign] 比特级 rate={args.bitflip_rate} "
                  f"target={args.bitflip_target} ...")
            report["levels"]["bit_level"] = run_bit_level(
                args.kernel, rate=args.bitflip_rate,
                target_flips=args.bitflip_target,
                max_duration=args.bitflip_max_duration)
            bl = report["levels"]["bit_level"]
            print(f"  -> flips={bl['plugin_flips_total']} boots={bl['boots']} "
                  f"deaths={bl['deaths']}", flush=True)

    report["total_duration_s"] = round(time.monotonic() - t_start, 2)
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[campaign] 报告已写入 {rp} (总耗时 {report['total_duration_s']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
