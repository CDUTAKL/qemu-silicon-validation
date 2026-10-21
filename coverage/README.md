# coverage/ — 固件核心逻辑覆盖率闭环（POSIX 轨）

在 macOS 本机用 **FreeRTOS POSIX port + gcc --coverage** 直接跑真实固件代码，
产出语句/分支覆盖率。一键复现：

```bash
./coverage/run_coverage.sh
```

产物：

- `report/coverage_summary.json` — 机器可读摘要（被 `rtm/rtm_report.py`、`report/final_report.py` 消费）
- `report/coverage_html/index.html` — lcov/genhtml 逐行标注报告
- 终端逐文件覆盖率表 + 达标判定（语句 ≥85%、分支 ≥75%，未达标退出码非 0）

当前结果：**语句 95.94%（449/468）、分支 86.76%（190/219），双达标**。

## 双轨方案的取舍（为什么这么设计）

| | ARM 目标轨（QEMU） | POSIX 目标轨（本目录） |
|---|---|---|
| 跑的是什么 | 完整固件镜像，Cortex-M3 裸机 | protocol/ + Core/ 任务逻辑，pthread 仿真调度 |
| 验证什么 | 端到端行为（启动 banner、寄存器级 UART、复位恢复） | 核心逻辑正确性 + 语句/分支覆盖率 |
| 覆盖率 | 裸机 gcov 需要把 `.gcda` 从 SRAM 经串口转储（`__gcov_flush` + 自定义 I/O），搭建成本高、拖慢 CI | `gcc --coverage` 本机直出 `.gcda`，lcov 一键成报 |

关键判断：**被测代码的价值密度在纯逻辑里**（CRC16、组帧/解析状态机、命令分发、
故障注入、心跳/看门狗逻辑），这些代码一行都不用改就能在宿主编译——真正的
硬件耦合只有 `uart.c`（MMIO 寄存器）和 `board.h`（NVIC 操作）两个点。
因此在 POSIX 侧用 shim 替换这两个点，就能让 90%+ 的固件代码进入覆盖率闭环；
裸机 gcov 转储的边际收益（多覆盖 uart.c 的几十行寄存器读写）远不抵其工程成本。

一句话面试版：*把"硬件相关"压缩到两个可 shim 的边界文件，换取全部协议与任务
逻辑在宿主机的秒级覆盖率闭环；ARM/QEMU 轨只负责 POSIX 轨覆盖不到的行为验证。*

## 覆盖范围声明

**在覆盖范围内**（9 个文件，全部真实执行 + 断言）：

- `firmware/protocol/crc16.c`、`frame.c` — **100% 语句 / 100% 分支**
  （标准向量 `"123456789"→0x29B1`、全 type 组帧、坏 CRC、错位同步头、序号连续性）
- `firmware/Core/sensor_task.c`、`watchdog_task.c`、`comm_task.c` — 真实任务在
  FreeRTOS POSIX port 上跑，数据帧/心跳帧经 wire 字节级校验
- `firmware/Core/diag_task.c` — 96% 语句：文本命令全表（STAT?/FAULT OV/OC/DISCONN/
  CONN/HANG/RESUME/RESET）、二进制主机帧（ACKREQ→ACK）、坏 CRC/坏头/未知 type 拒收、
  超长行丢弃、`\r` 处理
- `firmware/Core/app.c` — 83%：任务创建、心跳帧回调、**心跳丢失→复位**（FAULT HANG
  watchdog 触发）；未覆盖的是 malloc/stack-overflow 两个致命错误钩子（不破坏进程
  就无法触发，属于 dead-error-path）
- `firmware/Core/minilib.c`/`minilib.h` — **100%**：freestanding libc（memcpy/
  memmove 双方向重叠/str*/u32_to_dec/parse_f32/__aeabi_* EABI 别名）经
  volatile 函数指针直测（防止 gcc 内建替换）

**不在覆盖范围内**（及原因）：

- `firmware/Core/uart.c` — 纯 MMIO 寄存器读写（USART1 寄存器、RX 中断），宿主无对应
  硬件语义；POSIX 侧由 `posix_uart_shim.c` 替换同一接口（`uart.h`），该 shim 属测试
  工装不计入覆盖
- `firmware/Core/main.c`、`startup.c` — 启动汇编/C runtime 初始化、向量表，仅目标机
  有意义；由 QEMU 行为验证轨覆盖
- FreeRTOS 内核自身（deps/）— 第三方代码，从报告中过滤

## 目录结构

```
coverage/
├── run_coverage.sh            # 一键：构建→测试→gcov/lcov→JSON 摘要
├── gen_summary.py             # lcov info → report/coverage_summary.json
├── test_core_logic.c          # 单元测试驱动（400 项断言，纯 C main）
├── stream_parser.{c,h}        # 测试侧 wire 解析器（同步扫描/坏 CRC 拒收/序号检查）
└── posix_build/
    ├── build.sh               # gcc-16 -O0 -g --coverage 双二进制构建
    ├── FreeRTOSConfig.h       # POSIX 版内核配置（区别于 ARM 版）
    ├── port_posix_macos.c     # POSIX port 的 macOS 修复副本（见下）
    ├── posix_main.c           # 集成测试驱动（55 项断言，跑真实四任务应用）
    ├── posix_uart_shim.c      # uart.h 的宿主实现：TX 捕获 / RX 注入环形缓冲
    ├── shim_uart.h            # 测试侧控制 API
    ├── shim_board.c           # system_reset() 记录型 shim
    └── shim/board.h           # 经 -include 强制前置，用同名 BOARD_H 守卫
                               # 中和 Core 源文件同目录的 MMIO board.h
```

## macOS 适配说明（踩过的坑）

1. **clang 伪 gcc**：macOS 的 `gcc` 是 clang 别名，`--coverage` 产出 llvm-cov
   格式，gcov 不认。本流水线显式选用 Homebrew 真 gcc（当前 gcc-16）+ 配套
   `gcov-16`。安装：`brew install gcc lcov`。
2. **FreeRTOS POSIX port 的 16KB 页 bug**：upstream `port.c` 的 `__APPLE__` 路径
   用 `mach_vm_round_page()` 向上取整栈底地址；heap_4 的栈块只保证 8 字节对齐，
   在 arm64 macOS（16KB 页）下取整结果可能越过栈顶，栈大小下溢成 ~2^64，
   `pthread_create` 挂死。修复副本 `port_posix_macos.c` 去掉页面对齐、直接按
   原始边界计算并设 64KB 下限；`deps/` 保持原样未动。
3. **macOS secure headers**：`<string.h>` 会把 `memcpy` 等宏化为
   `__builtin___memcpy_chk`，与 `minilib.h` 的声明冲突；测试驱动在包含固件头
   之前 `#undef` 这些宏解决（firmware 源码零改动）。
4. `diag_task.c` 的 `LINE_MAX` 与系统 `<limits.h>` 重定义，仅有告警、无行为差异
   （固件侧 48 字节生效），属 firmware 既有问题，未改动。

## 复现步骤

```bash
# 依赖（如缺失）
env -u HOMEBREW_BOTTLE_DOMAIN -u HOMEBREW_API_DOMAIN brew install gcc lcov

# 一键
./coverage/run_coverage.sh

# 或分步
coverage/posix_build/build.sh                                # 构建
(cd coverage/posix_build/out/unit  && ./test_core_logic)     # 单元：400 checks
(cd coverage/posix_build/out/posix && ./posix_firmware)      # 集成：55 checks
```

退出码约定：测试全过 **且** 覆盖率双达标 → 0，否则非 0（可直接挂 CI gate）。
