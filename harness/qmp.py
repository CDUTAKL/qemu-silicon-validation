"""自实现 QMP (QEMU Monitor Protocol) 客户端 —— 无第三方依赖.

设计要点 (基于 QEMU 11.0.3 实测):
  * 连接 Unix socket 后先读 greeting ``{"QMP": ...}``,
    再发送 ``{"execute": "qmp_capabilities"}`` 完成握手。
  * QMP 会把**异步事件** (STOP / RESET / SHUTDOWN ...) 与命令响应
    交错发送, 因此读取响应时必须跳过带 ``"event"`` 键的消息,
    直到遇到 ``"return"`` 或 ``"error"``。
  * ``system_reset`` 在 QMP 有原生命令 (本版本已实测支持),
    实现为: 优先原生, 失败时回退经 ``human-monitor-command`` 桥接 HMP
    ``system_reset``, 对上统一封装。
  * ``info registers`` 无原生 QMP 等价物, 统一走 HMP 桥接,
    返回解析后的寄存器 dict。

用法::

    async with QmpClient("/tmp/qmp.sock") as q:
        print(await q.query_status())
        await q.stop(); await q.cont()
        regs = await q.info_registers()
        await q.system_reset()
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

DEFAULT_TIMEOUT = 5.0


class QMPError(RuntimeError):
    """QMP 协议错误基类。"""


class QMPTimeoutError(QMPError):
    """命令/事件等待超时。"""


class QMPCommandError(QMPError):
    """QMP 返回 ``{"error": ...}``。"""

    def __init__(self, error: dict[str, Any]):
        self.error_class = error.get("class", "?")
        self.description = error.get("desc", "")
        super().__init__(f"QMP error [{self.error_class}]: {self.description}")


EventListener = Callable[[dict[str, Any]], None]

# HMP `info registers` (ARM) 输出形如:
#   CPU#0
#   R00=00000000 R01=00000000 ...
#   R12=00000000 R13=20002000 R14=ffffffff R15=08000040
#   XPSR=41000000 -Z-- T priv-thread
#   FPSCR: 00000000
_REG_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,5})\s*[=:]\s*([0-9a-fA-F]+)\b")


class QmpClient:
    """异步 QMP 客户端.

    异步上下文管理器::

        async with QmpClient(sock_path, timeout=5.0) as q:
            ...

    事件监听: ``add_listener(cb)`` 注册回调 (cb(event_dict)),
    或 ``await wait_event(name, timeout)`` 等待特定事件。
    """

    def __init__(self, socket_path: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listeners: list[EventListener] = []
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._rx_task: asyncio.Task[None] | None = None
        self._tx_lock = asyncio.Lock()
        self._next_id = 0
        # id -> 等待响应的 Future; _rx_pump 收到匹配响应后 set_result
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.greeting: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # 连接 / 握手
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """连接 socket 并完成 QMP 握手 (greeting -> qmp_capabilities)。"""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.socket_path), timeout=self.timeout
        )
        greeting = await self._read_message()
        if "QMP" not in greeting:
            raise QMPError(f"非 QMP greeting: {greeting!r}")
        self.greeting = greeting
        # 启动后台事件泵: 持续读消息, 事件分发 + 响应入 pending 表
        self._rx_task = asyncio.create_task(self._rx_pump())
        await self.execute("qmp_capabilities")

    async def close(self) -> None:
        """关闭连接 (不关 QEMU; 关 QEMU 用 quit())。"""
        if self._rx_task is not None:
            self._rx_task.cancel()
            self._rx_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._writer = self._reader = None

    async def __aenter__(self) -> "QmpClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # 底层收发
    # ------------------------------------------------------------------ #
    def _readline(self) -> Awaitable[bytes]:
        assert self._reader is not None, "未连接"
        return self._reader.readline()

    async def _read_message(self) -> dict[str, Any]:
        line = await asyncio.wait_for(self._readline(), timeout=self.timeout)
        if not line:
            raise QMPError("QMP 连接被对端关闭 (EOF)")
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise QMPError(f"QMP 消息解析失败: {line!r}: {e}") from e

    async def _rx_pump(self) -> None:
        """后台读循环: 事件 -> 监听者+队列; 响应 -> 按 id 唤醒等待者."""
        pending = self._pending
        try:
            while True:
                msg = await self._read_message()
                if "event" in msg:
                    for cb in list(self._listeners):
                        try:
                            cb(msg)
                        except Exception:        # 监听者异常不影响协议
                            pass
                    self._event_queue.put_nowait(msg)
                elif "return" in msg or "error" in msg:
                    ident = msg.get("id")
                    fut = pending.pop(ident, None) if ident is not None else None
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                    # 无匹配 waiter 的响应 (如 oob) 直接丢弃
        except asyncio.CancelledError:
            raise
        except Exception as e:                 # EOF / 超时 / 解析错: 唤醒全部
            for fut in pending.values():
                if not fut.done():
                    fut.set_exception(QMPError(f"QMP 连接中断: {e}"))
            raise

    async def execute(
        self, command: str, arguments: dict[str, Any] | None = None,
        *, timeout: float | None = None,
    ) -> dict[str, Any]:
        """执行 QMP 命令, 返回 ``{"return": ...}`` 中的 return 值.

        Raises:
            QMPCommandError: QMP 返回错误。
            QMPTimeoutError: 超时 (默认 self.timeout)。
        """
        if self._writer is None:
            raise QMPError("未连接 QMP")
        ident = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"execute": command, "id": ident}
        if arguments:
            msg["arguments"] = arguments
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[ident] = fut          # _rx_pump 运行后 _pending 存在
        payload = (json.dumps(msg) + "\r\n").encode("utf-8")
        async with self._tx_lock:           # 串行化写, 避免交错
            self._writer.write(payload)
            await self._writer.drain()
        try:
            reply = await asyncio.wait_for(fut, timeout or self.timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(ident, None)
            raise QMPTimeoutError(
                f"QMP 命令 {command!r} 超时 ({timeout or self.timeout}s)"
            ) from e
        if "error" in reply:
            raise QMPCommandError(reply["error"])
        return reply["return"]

    # ------------------------------------------------------------------ #
    # 便捷方法
    # ------------------------------------------------------------------ #
    async def query_status(self) -> dict[str, Any]:
        """``query-status`` -> ``{"status": ..., "running": bool}``."""
        return await self.execute("query-status")

    async def stop(self) -> None:
        """暂停 guest (vcpu)。会触发 STOP 事件。"""
        await self.execute("stop")

    async def cont(self) -> None:
        """恢复 guest 运行。会触发 RESUME 事件。"""
        await self.execute("cont")

    async def quit(self) -> None:
        """退出 QEMU 进程 (触发 SHUTDOWN 事件, 随后连接 EOF)。"""
        await self.execute("quit", timeout=self.timeout)

    async def system_reset(self, *, resume: bool = True) -> None:
        """复位 guest。

        实测 QEMU 11.0.3 原生支持 QMP ``system_reset``; 为兼容旧版本,
        原生调用失败时回退 HMP 桥接 ``human-monitor-command`` + ``system_reset``。

        Args:
            resume: 复位后若 VM 处于暂停状态 (常见于 stop 后复位,
                状态为 ``prelaunch``/``paused``), 自动 ``cont`` 恢复运行。
        """
        try:
            await self.execute("system_reset")
        except QMPCommandError as e:
            if "not found" in e.description.lower() or "command" in e.error_class.lower():
                await self.execute("human-monitor-command",
                                   {"command-line": "system_reset"})
            else:
                raise
        if resume:
            st = await self.query_status()
            if not st.get("running", False):
                await self.cont()

    async def execute_hmp(self, command_line: str) -> str:
        """经 ``human-monitor-command`` 桥接执行任意 HMP 命令, 返回文本输出。"""
        return await self.execute(
            "human-monitor-command", {"command-line": command_line}
        )

    async def info_registers(self) -> dict[str, Any]:
        """``info registers`` -> 解析后的寄存器 dict。

        解析 HMP 文本输出, 提取 ``RN=XXXXXXXX`` / ``XPSR=`` / ``FPSCR:`` /
        (若存在) ``MSP=`` / ``PSP=`` 等字段, 统一转小写键并解析为 int。

        Returns:
            dict, 含 ``r0..r15`` (int)、``xpsr``、``fpscr`` 以及别名
            ``pc`` (= r15)、``sp`` (= r13); ``raw`` 保留原始文本;
            存在的 ``msp`` / ``psp`` 也一并收录。
        """
        text = await self.execute_hmp("info registers")
        regs: dict[str, Any] = {"raw": text}
        for m in _REG_RE.finditer(text):
            key, hexval = m.group(1).lower(), m.group(2)
            # QEMU 把 r0-r9 打印为零填充的 R00..R09, 规范化为 r0..r9
            z = re.fullmatch(r"r0*(\d+)", key)
            if z:
                key = f"r{int(z.group(1))}"
            try:
                regs[key] = int(hexval, 16)
            except ValueError:
                continue
        # 常用别名: R13=sp, R15=pc; 若文本里直接给了 MSP/PSP 则已在上一步收录
        if "r13" in regs:
            regs.setdefault("sp", regs["r13"])
        if "r15" in regs:
            regs.setdefault("pc", regs["r15"])
        return regs

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def add_listener(self, cb: EventListener) -> None:
        """注册事件回调, 每个 QMP 事件都会调用 ``cb(event_dict)``。"""
        self._listeners.append(cb)

    def remove_listener(self, cb: EventListener) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    async def wait_event(
        self, name: str | None = None, *, timeout: float | None = None,
    ) -> dict[str, Any]:
        """等待一个 (可选按名过滤的) QMP 事件, 返回事件 dict。

        Raises:
            QMPTimeoutError: 超时。
        """
        deadline = timeout if timeout is not None else self.timeout
        # 先扫一遍积压事件 (无截止时间消耗)
        while True:
            try:
                ev = self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if name is None or ev.get("event") == name:
                return ev
        try:
            return await asyncio.wait_for(self._wait_event_inner(name), deadline)
        except asyncio.TimeoutError as e:
            raise QMPTimeoutError(
                f"等待事件 {name or '*'} 超时 ({deadline}s)"
            ) from e

    async def _wait_event_inner(self, name: str | None) -> dict[str, Any]:
        while True:
            ev = await self._event_queue.get()
            if name is None or ev.get("event") == name:
                return ev
