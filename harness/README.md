# harness/ — QEMU 虚拟板卡硅后验证 · 测试控制层

被测对象: QEMU 仿真的 STM32 (`-M stm32vldiscovery`) 上运行的 FreeRTOS 固件。
本目录提供五层纯 Python 控制设施 (唯一第三方依赖: pyserial)，
不依赖固件即可自测 (见文末)。

```
qemu.py    QEMU 进程管理        QemuBoard: 拉起/监控/atexit 兜底清理
qmp.py     QMP 客户端 (自实现)  QmpClient: asyncio, 握手/命令/事件/寄存器
uart.py    串口帧解析层          FrameParser (状态机) + UartChannel (pyserial)
asserts.py 断言与统计            序号连续性 / 响应时间 / 上报周期 -> dict
fixture.py pytest fixture       board / board_session: 一键拉起整板
assets/    自测用最小 ARM ELF    idle.elf (Cortex-M3 空转, 源码见下)
```

## qemu.py — QEMU 进程管理

```python
from harness.qemu import QemuBoard

board = QemuBoard(kernel="firmware/build/firmware.elf",
                  qmp_socket="/tmp/qmp.sock",
                  snapshot=False,               # True -> -snapshot 不脏镜像
                  plugin="plugin/libfault.so",  # TCG 插件 (可省)
                  plugin_args=["arg=1"],        # 插件参数 (可省)
                  debug_flags="guest_errors")   # -d 标志 (可省)
pty = board.start()          # 阻塞至 pty 分配 + QMP socket 就绪
print(pty)                   # '/dev/ttys003' (macOS) 或 '/dev/pts/N' (Linux)
board.stop()                 # SIGTERM -> 宽限 -> SIGKILL; atexit 另有兜底
```

实测 (QEMU 11.0.3/homebrew): pty 分配信息 `char device redirected to ...`
打印到 **stdout** (部分构建/平台在 stderr, 实现同时轮询两个日志并兼容
`/dev/pts/N`、`/dev/ttysN` 两种形态)；固件跑飞 (Lockup) 时 QEMU 会
`qemu: fatal:` 后自行退出, `start()` 检测到提前退出即抛 `QemuStartError`
并附上日志尾部。

## qmp.py — QMP 客户端 (自实现, asyncio)

```python
import asyncio
from harness.qmp import QmpClient

async def main():
    async with QmpClient("/tmp/qmp.sock", timeout=5.0) as q:
        print(await q.query_status())            # {'status': 'running', ...}
        await q.stop(); await q.cont()
        await q.system_reset()                   # 原生 QMP; 旧版自动回退 HMP
        regs = await q.info_registers()          # {'r0'.. 'r15', 'pc', 'sp', 'xpsr', ...}
        print(hex(regs['pc']))
        ev = await q.wait_event("RESET", timeout=5.0)

asyncio.run(main())
```

- QMP 事件 (STOP/RESUME/RESET/SHUTDOWN) 与响应交错, 客户端自动分流;
  `add_listener(cb)` 订阅全部事件, `wait_event(name)` 等指定事件。
- `system_reset`: 本机 QEMU 11.0.3 **原生支持** `{"execute":"system_reset"}`,
  实现内仍保留 HMP 桥接回退 (`human-monitor-command` + `system_reset`),
  对上层统一封装; 复位后若处于暂停态自动 `cont`。
- `info_registers()` 经 HMP `info registers` 取文本并解析:
  `R00..R15/XPSR/FPSCR` 键转小写并归一化 (`R07` -> `r7`), 附 `pc`/`sp` 别名。

## uart.py — 串口帧解析层

帧格式 (冻结规格): `| AA 55 | seq u16 BE | type u8 | payload | crc16 BE |`
(CRC-16/CCITT-FALSE, init 0xFFFF, 覆盖帧头至 payload 末字节)

| type | 名称    | payload                                   |
|------|---------|-------------------------------------------|
| 0x01 | DATA    | 3 x float32 BE (v, i, t)                  |
| 0x02 | ALERT   | fault_id u8, level u8, value u16 BE       |
| 0x03 | HEARTBEAT | 任务位图 u8, 堆栈高水位 u16 BE            |
| 0x04 | REQUEST | — (测试端发出)                            |
| 0x05 | ACK     | —                                         |

```python
from harness.uart import UartChannel, FrameParser, TYPE_ALERT

uart = UartChannel(board.pty_path)     # pyserial 打开 pty
uart.open()
status, echo = uart.send_command("inject 3 2 1500")   # 文本命令 -> OK/ERR
frame = uart.wait_frame(type=TYPE_ALERT, timeout=5)   # 等一帧告警
fault_id, level, value = frame.values
frames = uart.collect_frames(duration=2.0)            # 限时收帧
uart.close()
```

文本回显与二进制帧混在同一字节流: `FrameParser` 按字节分流 —— `AA 55`
进定长收包状态机, 可打印 ASCII 收进文本行缓冲, 其余计垃圾字节;
坏 CRC / 未知 type 计坏帧, 序号缺口计丢帧、重号计重复。
`FrameParser` 可独立使用: `frames = parser.feed(chunk)`。

## asserts.py — 断言与统计 (全部返回 dict)

```python
from harness import asserts

sc = asserts.seq_continuity(frames)                 # 丢帧/重复/乱序
assert sc["is_contiguous"]

samples = asserts.alert_response_times(cmd_sent_at, frames, fault_id=3)
rt = asserts.response_time_stats(samples)           # n/min/max/mean/p50/p95/p99 (ms)

pd = asserts.period_stats(f.received_at for f in data_frames)  # 上报周期 (ms)
assert abs(pd["mean"] - 100.0) < 5.0 and pd["std"] < 2.0
```

## fixture.py — pytest fixture

```python
# tests/ 里:  from harness.fixture import board  (function 级, 默认)
#             from harness.fixture import board_session  (session 级)

def test_alert(board):
    board.reset()                                   # system_reset + 等启动横幅
    status, _ = board.uart.send_command("inject 3 2 1500")
    assert status == "OK"
    frame = board.uart.wait_frame(type=0x02, timeout=5)
    assert frame.values[0] == 3                     # fault_id
```

环境变量: `QSV_KERNEL` (固件 ELF 路径), `QSV_BOOT_BANNER` (启动横幅正则,
缺省等串口静默 0.3s), `QSV_QEMU_BIN` (QEMU 路径)。

## 自测 (不依赖固件)

```bash
.venv/bin/python harness/selftest_qmp.py       # 真实 QEMU: 握手/stop/cont/reset/寄存器/quit
.venv/bin/python harness/selftest_parser.py    # 合成字节流: 帧解析/混流/统计/FakeSerial
```

`assets/idle.elf` 为自测用最小 Cortex-M3 空转固件 (向量表 + WFI 循环,
源码 `harness/assets/idle.S`, 重编译:
`arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib -nostartfiles -T idle.ld`)。
