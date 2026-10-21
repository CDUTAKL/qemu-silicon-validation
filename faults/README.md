# faults/ — 故障注入层 (硅后验证三级注入)

```
fw_commands.py        命令级注入封装   FwFaultInjector: 固件文本命令 -> 内建故障路径
platform.py           平台级注入封装   PlatformFaultInjector: QMP 冻结/掉电复位/寄存器存档
bitflip/              比特级注入       QEMU TCG 插件, RAM 单比特翻转 (SEU), 见 bitflip/README.md
injection_campaign.py 注入战役脚本     三级战役 + 分级统计 -> report/fault_injection_stats.json
```

三级故障模型对应硅后验证的三类真实工况:

| 级别   | 机制                                | 对应物理场景                     |
|--------|-------------------------------------|----------------------------------|
| 命令级 | 固件内建 FAULT 命令 (OV/OC/DISCONN/HANG) | 传感器过压/过流/断连、任务死锁 |
| 平台级 | QMP stop/cont、system_reset         | 时钟毛刺/调度冻结、掉电上电循环  |
| 比特级 | TCG 插件翻转 RAM 单比特             | SEU 单粒子翻转 (航天/车规)       |

## 快速上手

```python
import sys; sys.path.insert(0, ".")      # 项目根
from harness.uart import UartChannel
from faults.fw_commands import FwFaultInjector

fi = FwFaultInjector(uart)               # uart: 已打开的 UartChannel
fi.inject_overvoltage(4.2)               # -> "OK FAULT OV" + 告警帧 fault_id=1
fi.inject_overcurrent(9.9)               # -> "OK FAULT OC" + 告警帧 fault_id=2
fi.disconnect(); fi.reconnect()          # 数据帧暂停/恢复 (心跳不受影响)
fi.hang("sensor"); fi.resume("sensor")   # vTaskSuspend/vTaskResume
fi.stat()                                # {"tasks":4,"qdepth":0,"run":N,"hb":N,"seq":N}
```

注意: 固件应答是 `OK FAULT OV` 这类带后缀文本行而非裸 `OK`,
`UartChannel.send_command` (只认裸 OK/ERR) 不适用, FwFaultInjector
内部直接写串口 + `wait_text` 匹配应答行。

```python
from faults.platform import PlatformFaultInjector

pf = PlatformFaultInjector(qmp, board)   # board 提供 .uart 用于 BOOT 横幅等待
pf.freeze(0.5)                           # QMP stop -> sleep -> cont
pf.power_cycle()                         # system_reset + 等 BOOT 横幅
pf.register_snapshot("before-ov")        # 寄存器现场存档 (r0..r15/pc/sp/xpsr)
pf.export_json("report/platform_events.json")
```

## 注入战役

小规模联调 (约 3~5 分钟):

```bash
.venv/bin/python faults/injection_campaign.py \
    --cmd-iters 30 --plat-iters 20 \
    --bitflip-rate 0.001 --bitflip-target 10000
```

全量正式战役 (简历数字来源: 命令级 300 次 + 平台级 200 次 +
比特级 10,000 注入量级):

```bash
.venv/bin/python faults/injection_campaign.py \
    --cmd-iters 300 --plat-iters 200 \
    --bitflip-rate 0.001 --bitflip-target 10000 --bitflip-max-duration 600
```

可选: `--skip-cmd/--skip-plat/--skip-bit` 只跑部分级别;
`--report` 改输出路径 (缺省 `report/fault_injection_stats.json`)。

## 报告格式

`report/fault_injection_stats.json`:

- `levels.command_level`: per-kind (OV/OC/DISCONN) 次数与检出数 +
  `overall_detection_rate` (OV/OC 看告警帧 fault_id; DISCONN 看丢帧:
  观察窗内数据帧全停 + 重连后恢复; 心跳只作遥测不作门槛 —— 宿主机
  高负载会饿死 vCPU 拉长 guest 定时器, 心跳整窗缺席是环境伪阴性)
- `levels.platform_level`: power_cycle/freeze 各自次数与恢复数 +
  `overall_recovery_rate` (恢复 = BOOT 横幅 + seq 归零 + 数据帧恢复)
- `levels.bit_level`: 插件统计 (accesses_in_window/flips, 从 QEMU
  stderr 回收, 跨重启累计)、boots/deaths/MTBF、host 侧帧统计
  (bad_frames=CRC 检出, lost_frames=序号缺口)
- 平台级事件明细 (含每次注入时间戳) 另存 `report/platform_events.json`

## 全量战役实测数字 (2026-10-21, QEMU 11.0.3, 与 pytest 全量并行)

| 级别   | 规模                          | 结果                                         |
|--------|-------------------------------|----------------------------------------------|
| 命令级 | 300 次 (OV/OC/DISCONN 各 100) | 检出率 **100%** (300/300)                    |
| 平台级 | 200 次 (复位/冻结 各 100)     | 恢复率 **100%** (200/200)                    |
| 比特级 | 30 boot 循环, rate=0.001      | 累计 **321 次翻转** / 334,835 次 RAM 访问, 30/30 固件死亡, MTBF **8.25s** |

比特级要点: 单 boot 平均 ~11 次翻转即致命 (SRAM 里有栈/TCB/指针);
死因全部表现为固件无响应 (STAT? 超时); host 侧 CRC 检出 124 个坏帧
(翻转打在串行化缓冲区的情形), 序号缺口 16,505 (g_seq 被打翻);
万次级注入需长时运行 (约 2 小时: `--bitflip-max-duration 7200`),
由"死亡->复位->再注"循环累积, 插件计数跨复位不清零、跨进程重启由
脚本从 stderr 回收。
