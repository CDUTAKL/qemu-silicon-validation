# 需求追踪矩阵 (RTM) 报告

生成时间: 2026-10-22 10:55 CST  
需求基线: `rtm/requirements.yaml`  用例来源: `tests/*.py` (ast 静态扫描)  执行结果: `report/junit.xml`

## 1. 概览统计

| 指标 | 数值 |
|---|---|
| 需求覆盖率 | **15/15** (100.0%) |
| 用例通过率 | 34/34 (100.0%) |
| 追踪到需求的用例数 | 34 |
| 覆盖缺口 | 无 (0 缺口) |

### 分组统计 (按用例组)

| 用例组 | 用例数 | 通过 | 失败/错误 | 未执行 |
|---|---|---|---|---|
| BOOT | 5 | 5 | 0 | 0 |
| FUNC | 10 | 10 | 0 | 0 |
| FLT | 10 | 10 | 0 | 0 |
| RCV | 5 | 5 | 0 | 0 |
| BM | 4 | 4 | 0 | 0 |

## 2. 需求 ↔ 用例 ↔ 执行结果对照表

| 需求 ID | 需求标题 | 优先级 | 覆盖用例数 | 覆盖用例 | 执行结果 | 状态 |
|---|---|---|---|---|---|---|
| FR-01 | 周期数据上报 | must | 4 | TC-BOOT-002<br>TC-FUNC-001<br>TC-FUNC-003<br>TC-FUNC-010 | TC-BOOT-002: PASS<br>TC-FUNC-001: PASS<br>TC-FUNC-003: PASS<br>TC-FUNC-010: PASS | PASS |
| FR-02 | 帧序号连续性 | must | 1 | TC-FUNC-002 | TC-FUNC-002: PASS | PASS |
| FR-03 | 心跳机制 | must | 2 | TC-FUNC-004<br>TC-FUNC-009 | TC-FUNC-004: PASS<br>TC-FUNC-009: PASS | PASS |
| FR-04 | ACK 请求响应 | must | 1 | TC-FUNC-007 | TC-FUNC-007: PASS | PASS |
| FR-05 | 诊断命令 | must | 3 | TC-BOOT-003<br>TC-FUNC-005<br>TC-FUNC-006 | TC-BOOT-003: PASS<br>TC-FUNC-005: PASS<br>TC-FUNC-006: PASS | PASS |
| FR-06 | 故障告警 | must | 5 | TC-FLT-001<br>TC-FLT-002<br>TC-FLT-003<br>TC-FLT-010<br>TC-FUNC-008 | TC-FLT-001: PASS<br>TC-FLT-002: PASS<br>TC-FLT-003: PASS<br>TC-FLT-010: PASS<br>TC-FUNC-008: PASS | PASS |
| FR-07 | 断线检测与恢复 | must | 3 | TC-FLT-004<br>TC-FLT-005<br>TC-FLT-010 | TC-FLT-004: PASS<br>TC-FLT-005: PASS<br>TC-FLT-010: PASS | PASS |
| FR-08 | 任务挂起/恢复 | should | 2 | TC-FLT-006<br>TC-FLT-007 | TC-FLT-006: PASS<br>TC-FLT-007: PASS | PASS |
| FR-09 | 看门狗复位 | must | 1 | TC-RCV-003 | TC-RCV-003: PASS | PASS |
| NFR-01 | 复位自举 | must | 4 | TC-BOOT-001<br>TC-RCV-001<br>TC-RCV-002<br>TC-RCV-005 | TC-BOOT-001: PASS<br>TC-RCV-001: PASS<br>TC-RCV-002: PASS<br>TC-RCV-005: PASS | PASS |
| NFR-02 | 寄存器现场合法 | must | 1 | TC-BOOT-004 | TC-BOOT-004: PASS | PASS |
| NFR-03 | 平台控制 | must | 2 | TC-BOOT-005<br>TC-RCV-004 | TC-BOOT-005: PASS<br>TC-RCV-004: PASS | PASS |
| NFR-04 | 时序性能 | should | 5 | TC-BM-002<br>TC-BM-003<br>TC-BM-004<br>TC-FLT-003<br>TC-FUNC-003 | TC-BM-002: PASS<br>TC-BM-003: PASS<br>TC-BM-004: PASS<br>TC-FLT-003: PASS<br>TC-FUNC-003: PASS | PASS |
| NFR-05 | 异常命令健壮性 | should | 2 | TC-FLT-008<br>TC-FLT-009 | TC-FLT-008: PASS<br>TC-FLT-009: PASS | PASS |
| NFR-06 | 启动时间 | should | 1 | TC-BM-001 | TC-BM-001: PASS | PASS |

## 3. 覆盖缺口 (未覆盖需求)

无缺口。15 条需求全部有至少一条 测试用例覆盖, 需求覆盖率 100%。

## 4. 用例清单与需求标注审计

| 用例 ID | 函数 | 文件 | 标注需求 | 结果 |
|---|---|---|---|---|
| TC-BM-001 | `test_tc_bm_001_boot_time` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_benchmark.py` | NFR-06 | PASS |
| TC-BM-002 | `test_tc_bm_002_data_period` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_benchmark.py` | NFR-04 | PASS |
| TC-BM-003 | `test_tc_bm_003_command_rtt` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_benchmark.py` | NFR-04 | PASS |
| TC-BM-004 | `test_tc_bm_004_heartbeat_period` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_benchmark.py` | NFR-04 | PASS |
| TC-BOOT-001 | `test_tc_boot_001_boot_banner` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_boot.py` | NFR-01 | PASS |
| TC-BOOT-002 | `test_tc_boot_002_first_data_frame` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_boot.py` | FR-01 | PASS |
| TC-BOOT-003 | `test_tc_boot_003_task_init_evidence` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_boot.py` | FR-05 | PASS |
| TC-BOOT-004 | `test_tc_boot_004_reset_registers` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_boot.py` | NFR-02 | PASS |
| TC-BOOT-005 | `test_tc_boot_005_qmp_stop_cont` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_boot.py` | NFR-03 | PASS |
| TC-FLT-001 | `test_tc_flt_001_fault_ov_alert` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | FR-06 | PASS |
| TC-FLT-002 | `test_tc_flt_002_fault_oc_alert` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | FR-06 | PASS |
| TC-FLT-003 | `test_tc_flt_003_alert_response_time` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | FR-06, NFR-04 | PASS |
| TC-FLT-004 | `test_tc_flt_004_disconn_frame_gap` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | FR-07 | PASS |
| TC-FLT-005 | `test_tc_flt_005_disconn_conn_roundtrip` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | FR-07 | PASS |
| TC-FLT-006 | `test_tc_flt_006_hang_sensor` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | FR-08 | PASS |
| TC-FLT-007 | `test_tc_flt_007_resume_sensor` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | FR-08 | PASS |
| TC-FLT-008 | `test_tc_flt_008_invalid_command` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | NFR-05 | PASS |
| TC-FLT-009 | `test_tc_flt_009_command_flood` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | NFR-05 | PASS |
| TC-FLT-010 | `test_tc_flt_010_combined_faults` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_fault.py` | FR-06, FR-07 | PASS |
| TC-FUNC-001 | `test_tc_func_001_frame_format` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-01 | PASS |
| TC-FUNC-002 | `test_tc_func_002_seq_continuity` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-02 | PASS |
| TC-FUNC-003 | `test_tc_func_003_data_period` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-01, NFR-04 | PASS |
| TC-FUNC-004 | `test_tc_func_004_heartbeat_period` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-03 | PASS |
| TC-FUNC-005 | `test_tc_func_005_stat_fields` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-05 | PASS |
| TC-FUNC-006 | `test_tc_func_006_stat_idempotent` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-05 | PASS |
| TC-FUNC-007 | `test_tc_func_007_ack_link` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-04 | PASS |
| TC-FUNC-008 | `test_tc_func_008_alert_frame` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-06 | PASS |
| TC-FUNC-009 | `test_tc_func_009_heartbeat_fields` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-03 | PASS |
| TC-FUNC-010 | `test_tc_func_010_data_bounded_continuity` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_functional.py` | FR-01 | PASS |
| TC-RCV-001 | `test_tc_rcv_001_qmp_system_reset_reboot` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_recovery.py` | NFR-01 | PASS |
| TC-RCV-002 | `test_tc_rcv_002_reset_command_reboot` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_recovery.py` | NFR-01 | PASS |
| TC-RCV-003 | `test_tc_rcv_003_watchdog_reset` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_recovery.py` | FR-09 | PASS |
| TC-RCV-004 | `test_tc_rcv_004_qmp_freeze_resume` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_recovery.py` | NFR-03 | PASS |
| TC-RCV-005 | `test_tc_rcv_005_reset_loop_stability` | `/Users/xushuyuan/Documents/qemu-silicon-validation/tests/test_recovery.py` | NFR-01 | PASS |

## 5. 代码覆盖率摘要

- 行覆盖率: **95.94%** (449/468)

---
*本报告由 `rtm/rtm_report.py` 自动生成, 请勿手工编辑。*
