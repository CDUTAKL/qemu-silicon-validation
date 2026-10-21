"""平台级故障注入封装: 经 QMP 对整板做冻结/掉电复位/寄存器存档.

与命令级 (faults/fw_commands.py) 的分工: 命令级走固件内建故障路径,
平台级绕过固件, 直接在仿真器层面制造异常工况:

    freeze(seconds)      QMP stop -> sleep -> cont        模拟时钟/调度冻结
    power_cycle()        QMP system_reset -> 等 BOOT 横幅  模拟掉电上电循环
    register_snapshot()  stop -> info registers -> cont    故障现场寄存器存档

每次注入追加一条事件 dict 到内存列表, export_json() 可整体导出
(供 injection_campaign.py 汇总进 report/fault_injection_stats.json)。

用法 (配合 harness.fixture.Board)::

    pf = PlatformFaultInjector(board.qmp, board)
    pf.freeze(0.5)
    ok = pf.power_cycle()                    # True = 看到 BOOT 横幅
    snap = pf.register_snapshot()            # -> {"r0": ..., "pc": ..., ...}
    pf.export_json("report/platform_events.json")

事件循环说明: QmpClient 是 asyncio 实现, 其后台接收任务绑定在建立连接时
的 loop 上; 因此当 board (harness.fixture.Board) 可用时一律复用
``board._run()`` 提交协程, 避免跨 loop 唤醒 Future 的隐患。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BOOT_BANNER = r"BOOT v\d"           # 固件横幅: "BOOT v1.0.0 tasks=4 q:16"


class PlatformFaultError(RuntimeError):
    """平台级注入执行失败 (QMP 错误 / 恢复验证超时)。"""


class PlatformFaultInjector:
    """平台级故障注入器.

    Args:
        qmp: ``harness.qmp.QmpClient`` (可尚未连接, 由 board 或调用方连接)。
        board: ``harness.fixture.Board`` 或任何带 ``uart`` 属性的聚合句柄;
            用于 power_cycle 的 BOOT 横幅等待。可为 None (此时 power_cycle
            只做复位, 不做恢复验证, 事件里 recovered=None)。
        boot_banner: 启动横幅正则 (缺省匹配 ``BOOT v<数字>``)。
        boot_timeout: 复位后等横幅的超时 (秒)。
        loop: 显式事件循环; 缺省时优先复用 board._run, 再退回自建 loop。
    """

    def __init__(
        self,
        qmp: Any,
        board: Any = None,
        *,
        boot_banner: str = DEFAULT_BOOT_BANNER,
        boot_timeout: float = 15.0,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.qmp = qmp
        self.board = board
        self.boot_banner = boot_banner
        self.boot_timeout = boot_timeout
        self.events: list[dict[str, Any]] = []
        self._own_loop = loop is None and not (board is not None and hasattr(board, "_run"))
        self._loop = loop if loop is not None else (
            asyncio.new_event_loop() if self._own_loop else None
        )

    # ------------------------------------------------------------------ #
    def _run(self, coro: Any) -> Any:
        """在正确的 loop 上同步执行协程 (优先 board 的专属 loop)。"""
        if self.board is not None and hasattr(self.board, "_run"):
            return self.board._run(coro)             # noqa: SLF001
        assert self._loop is not None
        return self._loop.run_until_complete(coro)

    def _record(self, event: dict[str, Any]) -> dict[str, Any]:
        event.setdefault("at", datetime.now(timezone.utc).isoformat())
        self.events.append(event)
        return event

    # ------------------------------------------------------------------ #
    # 注入原语
    # ------------------------------------------------------------------ #
    def freeze(self, seconds: float) -> dict[str, Any]:
        """冻结注入: 暂停全部 vCPU ``seconds`` 秒后恢复.

        模拟硬件层面的时钟/供电毛刺导致的执行停顿。返回事件记录。
        """
        if seconds <= 0:
            raise ValueError("freeze 时长必须为正")
        t0 = time.monotonic()
        self._run(self.qmp.stop())
        time.sleep(seconds)
        self._run(self.qmp.cont())
        status = self._run(self.qmp.query_status())
        running = bool(status.get("running", False))
        return self._record({
            "type": "freeze",
            "seconds": float(seconds),
            "elapsed": round(time.monotonic() - t0, 6),
            "resumed": running,
        })

    def power_cycle(self, *, verify: bool = True) -> dict[str, Any]:
        """掉电上电循环: system_reset 后等待固件 BOOT 横幅.

        Args:
            verify: True 且有 uart 时等待 BOOT 横幅验证恢复。

        Returns:
            事件记录; ``recovered`` 为 True/False, 无法验证时为 None。
        """
        t0 = time.monotonic()
        self._run(self.qmp.system_reset(resume=True))
        recovered: bool | None = None
        banner: str | None = None
        uart = getattr(self.board, "uart", None)
        if verify and uart is not None:
            try:
                banner = uart.wait_text(self.boot_banner, timeout=self.boot_timeout)
                recovered = True
            except TimeoutError:
                recovered = False
        return self._record({
            "type": "power_cycle",
            "elapsed": round(time.monotonic() - t0, 6),
            "recovered": recovered,
            "banner": banner,
        })

    def register_snapshot(self, label: str | None = None) -> dict[str, Any]:
        """寄存器现场存档: stop -> info registers -> cont.

        快照 (去掉冗长 raw 文本) 直接并入事件列表, 同时返回寄存器 dict
        (含 r0..r15/pc/sp/xpsr 等, 见 harness.qmp.QmpClient.info_registers)。
        """
        t0 = time.monotonic()
        self._run(self.qmp.stop())
        try:
            regs = self._run(self.qmp.info_registers())
        finally:
            self._run(self.qmp.cont())
        compact = {k: v for k, v in regs.items() if k != "raw"}
        self._record({
            "type": "register_snapshot",
            "label": label,
            "elapsed": round(time.monotonic() - t0, 6),
            "registers": compact,
        })
        return compact

    # ------------------------------------------------------------------ #
    # 汇总导出
    # ------------------------------------------------------------------ #
    def summary(self) -> dict[str, Any]:
        """按事件类型统计次数与成功率 (freeze.resumed / power_cycle.recovered)。"""
        out: dict[str, Any] = {}
        for ev in self.events:
            t = ev["type"]
            slot = out.setdefault(t, {"count": 0, "ok": 0, "fail": 0, "unknown": 0})
            slot["count"] += 1
            verdict = ev.get("resumed", ev.get("recovered"))
            if verdict is True:
                slot["ok"] += 1
            elif verdict is False:
                slot["fail"] += 1
            else:
                slot["unknown"] += 1
        return out

    def export_json(self, path: str | Path) -> Path:
        """把全部注入事件写到 JSON 文件 (供战役脚本合并)。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "event_count": len(self.events),
            "summary": self.summary(),
            "events": self.events,
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return p

    def close(self) -> None:
        if self._own_loop and self._loop is not None:
            self._loop.close()
            self._loop = None
