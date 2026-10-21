# tests/ — 用例库与需求映射 (RTM)

> 本文件当前含 TC-BOOT / TC-FUNC / TC-RCV 组映射; TC-FLT / TC-BM
> 各组的映射表由对应代理追加合并。

## 运行

```bash
.venv/bin/python -m pytest tests/ -v          # 全量
.venv/bin/python -m pytest tests/test_boot.py tests/test_functional.py -v
.venv/bin/python -m pytest tests/test_recovery.py -v
```

被测固件路径由 `conftest.py` 默认指向 `firmware/build/firmware.elf`
(可用环境变量 `QSV_KERNEL` 覆盖); 启动横幅正则 `QSV_BOOT_BANNER` 默认 `BOOT`。

## 用例 ↔ 需求映射

### TC-BOOT 启动组 (tests/test_boot.py)

| 用例 | 需求 | 内容 | 关键断言 |
|------|------|------|----------|
| TC-BOOT-001 | NFR-01 | 启动日志 | QMP 复位后 5s 内串口出现 BOOT 横幅 (含耗时断言) |
| TC-BOOT-002 | FR-01 | 首帧到达时间 | 复位 -> 首条数据帧 < 3s; 首帧 seq=0 |
| TC-BOOT-003 | FR-05 | 任务初始化证据 | STAT? 回显 tasks=4 且 hb 字段存在 |
| TC-BOOT-004 | NFR-02 | 复位后寄存器合法 | SP∈SRAM [0x20000000,0x2000C000]; PC∈Flash [0x08000000,0x08040000]; XPSR.T=1 |
| TC-BOOT-005 | NFR-03 | QMP stop/cont 冒烟 | stop 后 status=paused; cont 后 running |

### TC-FUNC 功能组 (tests/test_functional.py)

| 用例 | 需求 | 内容 | 关键断言 |
|------|------|------|----------|
| TC-FUNC-001 | FR-01 | 数据帧格式 | 20 帧全过 CRC (bad_frames=0)、type=0x01、v/i/t 在量程 |
| TC-FUNC-002 | FR-02 | 序号连续性 | 30s 窗口混合帧流 seq 无跳变无重复 (seq_continuity) |
| TC-FUNC-003 | FR-01/NFR-04 | 上报周期 | 帧数 ≥60/10s; 间隔均值 ∈[70,170]ms (实测基线 ~133ms, 见备注); std<40ms; P99<300ms |
| TC-FUNC-004 | FR-03 | 心跳周期 | 心跳帧间隔均值 ∈[500,1700]ms (标称 1s±50%, 上限含 QEMU 时钟漂移余量) |
| TC-FUNC-005 | FR-05 | STAT? 字段完整性 | tasks/qdepth/run/hb/seq 五字段正则全中, 数值合理 |
| TC-FUNC-006 | FR-05 | STAT? 幂等 | 连发 3 次格式一致, tasks 恒为 4 |
| TC-FUNC-007 | FR-04 | ACK 链路 | 发 0x04 请求 (payload 0x0001), 2s 内收 0x05 ACK (payload 0x0000) |
| TC-FUNC-008 | FR-06 | 告警帧 | `FAULT OV 15` 后 2s 内收 type=0x02/fault_id=1/level=2/value=15 |
| TC-FUNC-009 | FR-03 | 心跳字段 | 任务位图低 4 位全 1, 栈高水位 0<hwm<4096 |
| TC-FUNC-010 | FR-01 | 物理量有界/连续 | 50 帧无 NaN/Inf, 相邻帧差值有界 (dv≤2/di≤1/dt≤3) |

备注:

- 上电首条 BOOT 横幅不可靠: 固件在 harness 打开 pty 之前即已打印。
  TC-BOOT 用例一律以 `board.reset()` (QMP system_reset) 显式复位同步
  流位置, 等待复位后的新横幅, 不等上电横幅。
- 0x04/0x05 帧 payload 长度口径: 固件 `protocol/frame.h` 规定 2B
  (u16 请求码/状态码), `harness.uart.PAYLOAD_LEN` 记为 0B。TC-FUNC-007
  以固件 (DUT) 为准, 经 `conftest.ack_payload_compat()` 临时对齐解析表。
- 固件文本命令应答均为 `OK <detail>` 形式 (如 `OK STAT ...`),
  `UartChannel.send_command` 只认裸 `OK`/`ERR` 行, 故文本命令统一走
  `conftest.send_text_command` + `wait_text` 组合。
- TC-FUNC-003 阈值说明: guest 100ms 节拍在 QEMU TCG 下存在恒定 ~1.33x
  wall-time 比率失配 (时钟树建模差异, 非丢帧; 实测均值 ≈133ms, std ≈17ms,
  随宿主机负载漂移 128~145ms), 故不锚定 100ms±30%, 断言改为: 帧数 ≥60/10s、
  均值 ∈ [70,170]ms、std < 40ms (周期抖动有界, NFR-04 核心)、P99 < 300ms。
- TC-FUNC-004 心跳周期上限由规格的 +50% (1500ms) 放宽至 1700ms, 覆盖同一
  时钟比率漂移 (实测至 ~1.45x), 下限保持 500ms。

### TC-RCV 恢复组 (tests/test_recovery.py)

| 用例 | 需求 | 内容 | 关键断言 |
|------|------|------|----------|
| TC-RCV-001 | NFR-01 | QMP system_reset 后自举 | 5s 内 BOOT 横幅; 新流捕获 seq=0 首帧 (seq 归零重启) |
| TC-RCV-002 | NFR-01 | RESET 命令软复位 | `OK RESET` 回显 -> 5s 内 BOOT 横幅 -> seq=0 首帧 |
| TC-RCV-003 | FR-09 | 看门狗复位恢复 | `FAULT HANG watchdog` 后 hb 停增 -> 1s 定时器 NVIC_SystemReset -> 横幅复现 + seq 归零 + STAT? tasks=4 |
| TC-RCV-004 | NFR-03 | QMP stop 5s -> cont 冻结恢复 | 冻结期无新帧 (guest 全程 paused); cont 后首帧 seq 恰好衔接 (+1); 遥测持续 |
| TC-RCV-005 | NFR-01 | 复位循环稳定性 | system_reset x20, 每轮横幅耗时均 < 5s, 循环后遥测活着 |

备注:

- 看门狗触发条件以固件实际实现为准: `hb_count` 仅由 WatchdogTask 递增,
  只有挂起 **watchdog** 任务本身才会触发复位; 挂起 sensor/comm 不复位
  (由 TC-FLT-006 反向覆盖)。
- TC-RCV-004 冻结时长从需求原文 30s 调整为 5s: QMP stop 后 guest 时钟
  整体停走, 冻结/恢复语义与时长无关, 5s 已 50 倍于 100ms 帧周期。
