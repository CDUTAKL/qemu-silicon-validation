# faults/bitflip — QEMU TCG 单比特翻转 (SEU) 注入插件

本项目的比特级故障注入层: 一个 QEMU TCG 插件 (`bitflip.c`), 在仿真器
指令翻译层对 guest RAM 访问按概率注入**单比特翻转**, 对应航天/车规
场景中的 SEU (Single Event Upset, 单粒子翻转) 故障模型。

## 原理

```
guest 指令 ──TCG 翻译──> TB (translation block)
                              │
              tb_trans_cb 逐指令挂 mem_cb (qemu_plugin_register_vcpu_mem_cb)
                              │
              vCPU 执行 store/load ──> mem_cb (访问完成后触发)
                              │
                ┌─ qemu_plugin_get_hwaddr → 物理地址, 跳过 MMIO
                ├─ 落在 [addr, addr+size) 窗口内?
                ├─ xorshift64* PRNG 按 rate 抽中?
                └─ qemu_plugin_read/write_memory_hwaddr 读-改-写,
                   翻转访问字节中的 1 个随机比特
```

关键点:

- **翻译块回调**: 内存回调按指令注册, 只对产生内存访问的指令生效;
  回调在 vCPU 上下文、访问完成后触发, 因此对 store 的翻转直接落在
  最终写入值上 (等效 RAM 单元翻转); 对 load 的翻转下次读取才可见。
- **物理地址过滤**: 经 `qemu_plugin_hwaddr_phys_addr` 拿物理地址,
  `qemu_plugin_hwaddr_is_io` 排除 MMIO, 只打 RAM 窗口
  (缺省 STM32 SRAM `0x20000000` 起的 `0xC000` 字节)。
- **真翻转**: API v5+ 的 `qemu_plugin_write_memory_hwaddr` 允许插件
  回写 guest 物理内存 —— 不是观测, 是真实注错。
- **PRNG 自带**: xorshift64* 按 vCPU 分槽, 不用 `rand()`
  (共享全局状态, 多 vCPU 回调并发下既不可复现也不线程安全)。
- **线程模型**: 每 vCPU 独立 PRNG/计数器 (无锁), 读-改-写临界区
  由一把 GMutex 串行化; 统计在 `qemu_plugin_register_atexit_cb`
  里打到 stderr (QMP `quit` 正常退出时触发; SIGKILL 不会触发)。

## 构建

```bash
faults/bitflip/build.sh        # 产出 faults/bitflip/bitflip.so
```

clang `-dynamiclib -undefined dynamic_lookup` (插件符号由 QEMU 进程
dlopen 时解析); 头文件: `-I/opt/homebrew/include` (qemu-plugin.h) +
glib-2.0 双路径。pkg-config 可用时自动采用, 否则回退手写 `-I`
(可用 `QEMU_PREFIX` / `GLIB_PREFIX` 环境变量覆盖)。

## 使用 (QEMU 11.0.3 实测)

```bash
qemu-system-arm -M stm32vldiscovery -kernel firmware.elf \
    -serial pty -qmp unix:/tmp/qmp.sock,server=on,wait=off -nographic \
    -plugin faults/bitflip/bitflip.so,addr=0x20000000,size=0xC000,rate=0.001,seed=42
```

**参数格式实测结论 (QEMU 11.0.3 homebrew)**: `-plugin` 只接受一个
参数串, 插件参数以 `key=val` 逗号拼接在 `.so` 路径之后
(`-plugin file.so,addr=...,rate=...`)。**不能**把参数拆成独立 argv
(`-plugin file.so addr=...` 会被当成未知位置参数)。harness 的
`QemuBoard(plugin=...)` 请把整个逗号串放进 `plugin` 一个参数、
`plugin_args` 留空。

插件参数:

| 参数      | 缺省         | 说明                                   |
|-----------|--------------|----------------------------------------|
| addr      | 0x20000000   | RAM 窗口起始物理地址 (hex/dec)         |
| size      | 0xC000       | 窗口大小                               |
| rate      | 1e-6         | 每次窗口内访问的翻转概率; 0=只统计     |
| mode      | w            | w=只注入写访问 / rw=读写都注入         |
| seed      | 0            | PRNG 种子; 0=时间+PID 派生 (不可复现)  |
| maxflips  | 0            | 注入次数上限; 0=不限                   |
| logevery  | 0            | 每 N 次注入向 stderr 打进度行; 见下     |

`logevery` 的用途: 翻转打中关键结构时固件 Lockup, QEMU 直接 `abort()`,
**atexit 统计不会执行** —— 进度行是崩溃前计数的唯一留存
(战役脚本取 stderr 中最后一次出现的计数为准)。

## 战役实测结论 (QEMU 11.0.3 + firmware.elf, rate=0.001, 全 SRAM 窗口)

- 窗口内 RAM 写访问 ~1.1 万次/秒, 即每小时可注入的规模上限由
  "翻转致死率" 决定而非注入速度;
- 固件平均在 **~13-17 次翻转 / ~8 秒** 内死亡 (栈/TCB/指针区在 SRAM,
  被打中即挂); 死因以 "固件无响应 (STAT? 超时)" 为主, 部分直接
  Lockup 导致 QEMU abort;
- host 侧 CRC 对这类翻转**基本无检出**: 固件在帧出队串行化时才计算
  CRC, 队列内/数据区的翻转属于静默数据错误 (silicon data corruption),
  可观测量是固件死亡与帧流中断 —— 这正是 SEU 场景下需要
  EDC/ECC 或关键数据冗余的实证依据;
- 累积注入量靠 "死亡 -> system_reset/整机重启 -> 再注" 循环堆
  (injection_campaign.py 的 bit_level 已实现, 插件计数跨复位不清零,
  跨进程重启由脚本从 stderr 回收累计)。

## 最小验证

```bash
.venv/bin/python faults/bitflip/smoke_plugin.py --rate 0.0001 --seconds 6
```

实测输出 (firmware.elf 跑 FreeRTOS 四任务, 6 秒):

```
[bitflip] loaded: window=[0x20000000,0x2000c000) rate=0.0001 mode=w seed=42 maxflips=0 vcpus=1
[bitflip] accesses_in_window=64991 flips=8 io_skipped=0
[bitflip] last flip: phys=0x20001fd5 mask=0x01
[smoke] PASS: 插件加载+统计输出验证
```

插件加载打印在加载时即出; 统计行在 **QMP quit 正常退出**时经 atexit
回调输出 (SIGTERM/SIGKILL 直接杀进程拿不到统计)。
