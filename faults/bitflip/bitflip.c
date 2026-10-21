/*
 * bitflip.c - QEMU TCG plugin: single-event-upset (SEU) RAM bit-flip injector.
 *
 * 原理
 * ----
 * QEMU TCG 把 guest 指令翻译为宿主机 TB (translation block)。本插件在翻译
 * 回调 (qemu_plugin_register_vcpu_tb_trans_cb) 里给 TB 内每条指令挂上内存
 * 访问回调 (qemu_plugin_register_vcpu_mem_cb)。回调在访问完成后于 vCPU
 * 上下文触发, 此时:
 *   1. qemu_plugin_get_hwaddr + qemu_plugin_hwaddr_phys_addr 取出物理地址,
 *      跳过 MMIO (qemu_plugin_hwaddr_is_io);
 *   2. 地址落在目标 RAM 窗口 [addr, addr+size) 内时, 以概率 rate 用插件
 *      自带的 xorshift64* PRNG 抽中本次访问;
 *   3. 抽中则经 qemu_plugin_read/write_memory_hwaddr 对本次访问覆盖的
 *      字节做读-改-写, 翻转其中一个随机比特 —— 等效于该 RAM 单元发生
 *      单粒子翻转 (SEU)。对 store 而言回调在写完成后触发, 翻转直接落在
 *      最终值上; 对 load 而言等效于该单元在读后翻转, 下次读取才可见。
 *
 * 线程模型: vCPU 回调并发执行, PRNG 与计数器按 vCPU 分槽 (无锁);
 * 读-改-写临界区由一把 GMutex 串行化。统计在 QEMU 退出时
 * (qemu_plugin_register_atexit_cb) 打到 stderr。
 *
 * 参数 (-plugin bitflip.so,addr=...,size=...,rate=...,mode=...,seed=...,maxflips=...):
 *   addr     RAM 窗口起始物理地址 (hex/dec, 缺省 0x20000000 = STM32 SRAM)
 *   size     窗口大小             (hex/dec, 缺省 0xC000)
 *   rate     每次窗口内访问的翻转概率 (浮点, 缺省 1e-6; 0 = 只统计不注入)
 *   mode     w = 只注入写访问 (缺省) | rw = 读写都注入
 *   seed     PRNG 种子 (hex/dec, 缺省 0 = 由时间+PID 派生, 不可复现)
 *   maxflips 注入次数上限, 达到后不再注入 (缺省 0 = 不限; 便于可控战役)
 *   logevery 每 N 次注入向 stderr 打一行进度 (缺省 0 = 关; 固件 Lockup
 *            导致 QEMU abort 时 atexit 不执行, 进度行是崩溃前统计的
 *            唯一留存, 战役脚本取最后一行为准)
 */

#include <glib.h>
#include <inttypes.h>
#include <qemu-plugin.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

/* ------------------------------------------------------------------ */
/* 配置                                                                */
/* ------------------------------------------------------------------ */
static uint64_t cfg_addr = 0x20000000;   /* STM32 SRAM base */
static uint64_t cfg_size = 0xC000;
static double cfg_rate = 1e-6;
static enum qemu_plugin_mem_rw cfg_mode = QEMU_PLUGIN_MEM_W;
static uint64_t cfg_seed = 0;
static uint64_t cfg_maxflips = 0;        /* 0 = unlimited */
static uint64_t cfg_logevery = 0;        /* >0: 每 N 次注入打一行进度到 stderr
                                            (Lockup abort 时 atexit 不跑,
                                            进度行是崩溃前统计的唯一留存) */

/* ------------------------------------------------------------------ */
/* 每 vCPU 状态 (数组槽位, 下标即 vcpu_index; 同一 vCPU 的回调串行)     */
/* ------------------------------------------------------------------ */
typedef struct {
    uint64_t prng_state;        /* xorshift64* 状态, 永不为 0 */
    uint64_t accesses;          /* 落在窗口内的内存访问次数 */
    uint64_t flips;             /* 实际注入的翻转次数 */
    uint64_t io_skipped;        /* MMIO 访问次数 (只统计, 不注入) */
} VcpuSlot;

static VcpuSlot *slots;
static int n_slots;
static GMutex inject_lock;      /* 串行化 read-modify-write 与全局统计 */

static uint64_t last_flip_phys;        /* 最近一次翻转的物理地址 */
static uint8_t last_flip_mask;         /* 最近一次翻转的位掩码 */
static uint64_t total_flips;           /* 跨 vCPU 累计 (只在 inject_lock 内访问) */

/* ------------------------------------------------------------------ */
/* xorshift64* PRNG: 不用 rand()/random(), 避免共享全局状态的线程问题  */
/* ------------------------------------------------------------------ */
static inline uint64_t xs64_next(uint64_t *s)
{
    uint64_t x = *s;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    *s = x;
    return x * UINT64_C(2685821657736338717);
}

/* 均匀 [0,1) 双精度: 取高 53 位 */
static inline double xs64_next_double(uint64_t *s)
{
    return (double)(xs64_next(s) >> 11) * (1.0 / 9007199254740992.0);
}

/* ------------------------------------------------------------------ */
/* 内存访问回调 (vCPU 上下文, 访问完成后触发)                            */
/* ------------------------------------------------------------------ */
static void mem_cb(unsigned int vcpu_index, qemu_plugin_meminfo_t info,
                   uint64_t vaddr, void *userdata)
{
    (void)userdata;
    struct qemu_plugin_hwaddr *hw;
    uint64_t phys;
    VcpuSlot *slot;

    if ((int)vcpu_index >= n_slots) {
        return;                 /* 热插 vCPU 超出建表, 防御性忽略 */
    }
    slot = &slots[vcpu_index];

    hw = qemu_plugin_get_hwaddr(info, vaddr);
    if (!hw) {
        return;
    }
    if (qemu_plugin_hwaddr_is_io(hw)) {
        slot->io_skipped++;
        return;
    }
    phys = qemu_plugin_hwaddr_phys_addr(hw);
    if (phys < cfg_addr || phys >= cfg_addr + cfg_size) {
        return;
    }
    slot->accesses++;

    if (cfg_rate <= 0.0) {
        return;                 /* 统计模式 */
    }
    if (xs64_next_double(&slot->prng_state) >= cfg_rate) {
        return;
    }

    /* 抽中: 对本次访问覆盖的字节做读-改-写, 翻一个随机比特 */
    g_mutex_lock(&inject_lock);
    if (cfg_maxflips) {
        uint64_t done = 0;
        for (int i = 0; i < n_slots; i++) {
            done += slots[i].flips;
        }
        if (done >= cfg_maxflips) {
            g_mutex_unlock(&inject_lock);
            return;
        }
    }

    size_t access_len = (size_t)1 << qemu_plugin_mem_size_shift(info);
    if (access_len > 8) {
        access_len = 8;         /* 128-bit 访问只处理低 8 字节, 保守截断 */
    }

    GByteArray *buf = g_byte_array_sized_new((guint)access_len);
    enum qemu_plugin_hwaddr_operation_result rc =
        qemu_plugin_read_memory_hwaddr(phys, buf, access_len);
    if (rc == QEMU_PLUGIN_HWADDR_OPERATION_OK && buf->len == access_len) {
        unsigned byte_idx = (unsigned)(xs64_next(&slot->prng_state) % access_len);
        unsigned bit_idx = (unsigned)(xs64_next(&slot->prng_state) % 8u);
        uint8_t mask = (uint8_t)(1u << bit_idx);
        buf->data[byte_idx] ^= mask;
        rc = qemu_plugin_write_memory_hwaddr(phys, buf);
        if (rc == QEMU_PLUGIN_HWADDR_OPERATION_OK) {
            slot->flips++;
            total_flips++;
            last_flip_phys = phys + byte_idx;
            last_flip_mask = mask;
            if (cfg_logevery && total_flips % cfg_logevery == 0) {
                uint64_t acc = 0;
                for (int i = 0; i < n_slots; i++) {
                    acc += slots[i].accesses;
                }
                fprintf(stderr,
                        "[bitflip] progress: accesses_in_window=%" PRIu64
                        " flips=%" PRIu64 "\n", acc, total_flips);
                fflush(stderr);
            }
        }
    }
    g_byte_array_unref(buf);
    g_mutex_unlock(&inject_lock);
}

/* ------------------------------------------------------------------ */
/* 翻译回调: 给 TB 内每条指令挂内存访问回调                              */
/* ------------------------------------------------------------------ */
static void tb_trans_cb(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    (void)id;
    size_t n = qemu_plugin_tb_n_insns(tb);
    for (size_t i = 0; i < n; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        qemu_plugin_register_vcpu_mem_cb(insn, mem_cb,
                                         QEMU_PLUGIN_CB_NO_REGS,
                                         cfg_mode, NULL);
    }
}

/* ------------------------------------------------------------------ */
/* 退出统计 -> stderr                                                   */
/* ------------------------------------------------------------------ */
static void atexit_cb(qemu_plugin_id_t id, void *userdata)
{
    (void)id;
    (void)userdata;
    uint64_t accesses = 0, flips = 0, io_skipped = 0;

    for (int i = 0; i < n_slots; i++) {
        accesses += slots[i].accesses;
        flips += slots[i].flips;
        io_skipped += slots[i].io_skipped;
    }
    fprintf(stderr,
            "[bitflip] window=[0x%" PRIx64 ",0x%" PRIx64 ") rate=%g mode=%s\n"
            "[bitflip] accesses_in_window=%" PRIu64 " flips=%" PRIu64
            " io_skipped=%" PRIu64 "\n",
            cfg_addr, cfg_addr + cfg_size, cfg_rate,
            cfg_mode == QEMU_PLUGIN_MEM_W ? "w" : "rw",
            accesses, flips, io_skipped);
    if (flips > 0) {
        fprintf(stderr, "[bitflip] last flip: phys=0x%" PRIx64 " mask=0x%02x\n",
                last_flip_phys, last_flip_mask);
    }
    fflush(stderr);
}

/* ------------------------------------------------------------------ */
/* 参数解析与安装                                                       */
/* ------------------------------------------------------------------ */
static int parse_u64(const char *s, uint64_t *out)
{
    uint64_t v = g_ascii_strtoull(s, NULL, 0);
    if (v == 0 && s[0] != '0') {
        return -1;
    }
    *out = v;
    return 0;
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    if (!info->system_emulation) {
        fprintf(stderr, "[bitflip] 仅支持系统仿真 (qemu-system-*)\n");
        return 1;
    }

    for (int i = 0; i < argc; i++) {
        const char *eq = strchr(argv[i], '=');
        if (!eq) {
            eq = argv[i] + strlen(argv[i]);
        }
        g_autofree char *key = g_strndup(argv[i], eq - argv[i]);
        const char *val = *eq ? eq + 1 : "";
        if (g_strcmp0(key, "addr") == 0) {
            if (parse_u64(val, &cfg_addr)) {
                goto bad_arg;
            }
        } else if (g_strcmp0(key, "size") == 0) {
            if (parse_u64(val, &cfg_size)) {
                goto bad_arg;
            }
        } else if (g_strcmp0(key, "rate") == 0) {
            cfg_rate = g_ascii_strtod(val, NULL);
            if (cfg_rate < 0.0 || cfg_rate > 1.0) {
                goto bad_arg;
            }
        } else if (g_strcmp0(key, "seed") == 0) {
            if (parse_u64(val, &cfg_seed)) {
                goto bad_arg;
            }
        } else if (g_strcmp0(key, "maxflips") == 0) {
            if (parse_u64(val, &cfg_maxflips)) {
                goto bad_arg;
            }
        } else if (g_strcmp0(key, "logevery") == 0) {
            if (parse_u64(val, &cfg_logevery)) {
                goto bad_arg;
            }
        } else if (g_strcmp0(key, "mode") == 0) {
            if (g_strcmp0(val, "w") == 0) {
                cfg_mode = QEMU_PLUGIN_MEM_W;
            } else if (g_strcmp0(val, "rw") == 0) {
                cfg_mode = QEMU_PLUGIN_MEM_RW;
            } else {
                goto bad_arg;
            }
        } else {
        bad_arg:
            fprintf(stderr, "[bitflip] 非法参数: %s\n", argv[i]);
            return 1;
        }
    }
    if (cfg_size == 0) {
        fprintf(stderr, "[bitflip] size 不能为 0\n");
        return 1;
    }

    n_slots = info->system.max_vcpus > 0 ? info->system.max_vcpus : 1;
    slots = g_new0(VcpuSlot, n_slots);
    uint64_t seed_base = cfg_seed ? cfg_seed
        : ((uint64_t)time(NULL) << 20) ^ (uint64_t)getpid() ^ UINT64_C(0x9e3779b97f4a7c15);
    for (int i = 0; i < n_slots; i++) {
        slots[i].prng_state = seed_base + UINT64_C(0x9e3779b97f4a7c15) * (uint64_t)(i + 1);
    }

    qemu_plugin_register_vcpu_tb_trans_cb(id, tb_trans_cb);
    qemu_plugin_register_atexit_cb(id, atexit_cb, NULL);
    fprintf(stderr,
            "[bitflip] loaded: window=[0x%" PRIx64 ",0x%" PRIx64 ") rate=%g"
            " mode=%s seed=%" PRIu64 " maxflips=%" PRIu64 " vcpus=%d\n",
            cfg_addr, cfg_addr + cfg_size, cfg_rate,
            cfg_mode == QEMU_PLUGIN_MEM_W ? "w" : "rw",
            seed_base, cfg_maxflips, n_slots);
    fflush(stderr);
    return 0;
}
