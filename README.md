# QEMU 虚拟板卡硅后验证框架（qemu-silicon-validation）

硅后验证（post-silicon validation）风格的自动化测试框架：在 QEMU 虚拟板卡
`stm32vldiscovery`（Cortex-M3）上运行 FreeRTOS 四任务固件（采集上报终端，
0xAA55 + CRC16 自定义帧协议），用 pytest + 自实现 QMP 客户端 + pyserial 做
测试控制，支持三档故障注入（UART 命令级 / QMP 平台级 / TCG 插件比特级），
配 RTM 需求追踪矩阵、gcov 覆盖率（FreeRTOS POSIX port 双轨）与
GitHub Actions nightly 回归。对标岗位：芯片系统验证 / 硅后验证工程师
（系统级测试、原型阶段功能性能测试、测试方案 / 用例 / 报告、协同定位问题）。

## 架构（分层）

```
┌──────────────────────────────────────────────────────────────┐
│ 报告层    final_report.html · rtm.md · pytest_report.html     │
│           coverage_summary.json · benchmark_history.jsonl     │
├──────────────────────────────────────────────────────────────┤
│ 编排层    main.py（[1/6]~[6/6] 阶段化）· Makefile · CI nightly│
├──────────────────────────────────────────────────────────────┤
│ 用例层    tests/: boot / functional / fault / recovery /      │
│           benchmark（pytest，fixture=board，RTM 需求编号）    │
├──────────────────────────────────────────────────────────────┤
│ 注入层    faults/: UART 命令级 │ QMP 平台级 │ TCG 插件比特级  │
├──────────────────────────────────────────────────────────────┤
│ 控制层    harness/: QemuBoard（进程管理）· QmpClient（自实现  │
│           asyncio QMP）· UartChannel（pyserial 帧状态机）     │
├──────────────────────────────────────────────────────────────┤
│ 平台层    QEMU -M stm32vldiscovery（pty UART + unix QMP）     │
├──────────────────────────────────────────────────────────────┤
│ 被测层    firmware/: FreeRTOS V11.1.0 四任务（sensor/comm/    │
│           diag/watchdog），0xAA55+CRC16 帧，inject 文本命令   │
└──────────────────────────────────────────────────────────────┘
```

（详见设计文档 §2.1，本图为该节的 ASCII 复述。）

## 快速开始

```bash
# 依赖：qemu-system-arm、arm-none-eabi-gcc（macOS: brew install qemu；gcc-arm-embedded）
uv venv && uv pip install pytest pytest-html pyserial

make build     # 固件构建 → firmware/build/firmware.elf
make test      # pytest 全量 → report/junit.xml + report/pytest_report.html

# 一键全流程（阶段化、每阶段打印耗时与产物、退出码反映总成败）
.venv/bin/python main.py
.venv/bin/python main.py --skip faults coverage   # 跳过慢阶段
```

CI：push 与每日 nightly（`.github/workflows/ci.yml`）自动跑全量并上传
`report/` 整目录为 artifact。

## 目录结构

| 路径 | 职责 |
|---|---|
| `firmware/` | 被测固件：FreeRTOS 四任务 + 自定义帧协议 + `build.sh` |
| `harness/` | 测试控制层：`QemuBoard` / `QmpClient`（自实现 asyncio QMP）/ `UartChannel` / pytest fixture `board` |
| `tests/` | 五组用例 + conftest；benchmark 写 `report/benchmark_history.jsonl` |
| `faults/` | 三档故障注入 + `injection_campaign.py`（写 `fault_injection_stats.json`） |
| `coverage/` | `run_coverage.sh`：gcov 双轨 → `coverage_summary.json` |
| `rtm/` | `rtm_report.py`（吃 junit.xml 出 `rtm.md/json`）+ `test_plan.md` |
| `report/` | 各层 JSON 产物 + `final_report.py`（自包含 HTML 总报告） |
| `main.py` | 一键全流程编排（阶段化、可 --skip、固定种子可复现） |
| `.github/workflows/ci.yml` | nightly + push 回归，上传 report/ artifacts |

## 用例分组与故障注入档位

五组用例（RTM 需求编号 TC-XXX-NNN）：

| 组 | 关注点 | 示例 |
|---|---|---|
| `test_boot` | 上电启动 | 启动横幅、首帧时间、任务心跳位图 |
| `test_functional` | 功能正确性 | DATA 帧周期 100ms、序号连续、CRC 全对 |
| `test_fault` | 故障注入响应 | `inject` 命令 → ALERT 帧（fault_id/level/value） |
| `test_recovery` | 恢复能力 | QMP system_reset 后重启恢复、看门狗行为 |
| `benchmark` | 性能基线 | 告警响应时间分位、上报周期抖动 → JSONL 留史 |

三档故障注入：

1. **UART 命令级**：固件内建 `inject <fault_id> <level> <duration_ms>` 文本命令，业务语义级注入（传感器越限等）。
2. **QMP 平台级**：QMP `system_reset` / stop-cont / HMP，平台事件级注入（复位、暂停）。
3. **TCG 插件比特级**：QEMU TCG plugin 在指令流上翻转比特，软错误（SEU 风格）注入；macOS 产物为 `.dylib`，CI 中用 cc 重编 `.so`（experimental，continue-on-error）。

## 数据口径

> 下表为本机（macOS, QEMU 11.0.3）全量回归实测值（2026-10-21）；CI nightly 数字以 artifacts 为准。

| 指标 | 口径 | 数值 |
|---|---|---|
| 用例总数 / 通过率 | report/junit.xml，pytest 全量 | 34/34 通过（100%） |
| 需求覆盖率 | rtm.json，有用例覆盖的需求占比 | 15/15（100%，34 用例追踪，无缺口） |
| 代码覆盖率 | coverage_summary.json，gcov 双轨 | 行 95.94%（449/468）、分支 86.76%（190/219），目标 行≥85%/分支≥75% 达成 |
| 故障注入检出率 | fault_injection_stats.json | 命令级 300 次检出 100%；平台级 200 次恢复 100%；比特级 rate=0.001 下 214 次翻转 / 16 轮启动，MTBF 8.25 s |
| 告警响应时间 | alert_stats.json，inject 命令→ALERT 帧（TC-FLT-003，n=50） | P50 123.1 ms / P95 142.1 ms（判定准则 P95<500 ms） |
| DATA 帧上报周期 | benchmark_history.jsonl（TC-BM-002，n=299） | 均值 126.9 ms（P95 142.0 ms，虚拟平台、随宿主机负载漂移） |
| 本地全流程耗时 | main.py 六阶段（构建/pytest/故障注入/覆盖率/RTM/总报告） | 710 s（pytest 199 s + 故障注入战役 494 s） |

## 边界与诚实声明

- **虚拟板卡**：被测对象是 QEMU `stm32vldiscovery` 机器模型，非真实 STM32F100 硅片；外设时序与真机存在差异。
- **软件模拟传感器**：传感器数据由固件内软件模型产生，未接真实传感器与真实仪器。
- **TCG 比特翻转 ≠ 真实 SEU**：插件注入用于演示软错误注入方法学，不等价于器件级单粒子翻转实验。
- **覆盖率口径**：gcov 经 FreeRTOS POSIX port 双轨获得，与片上运行统计口径略有出入。
- 性能数字（响应时间 / 上报周期）反映虚拟平台行为，用于展示验证流程与数据口径，不代表物理芯片指标。

（对应设计文档 §7，以该节为准。）

## 面试问答索引

常见面试问题（为什么用 QEMU 而不是真板子？QMP 为什么不现成库？三档注入怎么分工？RTM 怎么闭环？覆盖率双轨的意义？ nightly 回归怎么设计？）的参考答案见设计文档 §5 面试问答一节。
