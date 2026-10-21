"""TC-BOOT: 启动链路与平台控制用例 (TC-BOOT-001 ~ 005).

被测对象: QEMU -M stm32vldiscovery 上运行的 FreeRTOS 固件。
每条用例的 docstring 标注关联需求 ID (见 tests/README.md RTM 表)。
"""

from __future__ import annotations

import time

from harness.uart import TYPE_DATA

from conftest import STAT_RE, stat_query

BOOT_BANNER_TIMEOUT = 5.0    # NFR-01: 复位后启动横幅上限
FIRST_FRAME_TIMEOUT = 3.0    # FR-01: 复位->首条数据帧上限

# stm32vldiscovery (STM32F100RB): Flash 128KB @0x08000000, SRAM 8KB @0x20000000
# (断言上界按规格放宽到 0x2000C000 / 0x08040000)
SRAM_BASE, SRAM_END = 0x20000000, 0x2000C000
FLASH_BASE, FLASH_END = 0x08000000, 0x08040000
XPSR_THUMB_BIT = 24


def test_tc_boot_001_boot_banner(board):
    """TC-BOOT-001 (NFR-01): 复位后 5s 内串口出现 BOOT 启动横幅.

    注意: 上电首条横幅不可靠 (固件在 harness 打开 pty 前即已打印),
    因此先用一次 board.reset() 同步流位置, 再做计时复位:
    QMP system_reset -> 等复位后新横幅, 断言横幅文本与到达耗时。
    """
    board.reset(timeout=BOOT_BANNER_TIMEOUT)  # 同步: 显式复位并消费新横幅

    res = board.reset(timeout=BOOT_BANNER_TIMEOUT)
    assert res["banner"] is not None and "BOOT" in res["banner"], (
        f"复位后未捕获 BOOT 横幅: {res}"
    )
    assert res["elapsed"] < BOOT_BANNER_TIMEOUT, (
        f"横幅到达耗时 {res['elapsed']:.3f}s 超过 {BOOT_BANNER_TIMEOUT}s"
    )


def test_tc_boot_002_first_data_frame(board):
    """TC-BOOT-002 (FR-01): 复位 -> 首条数据帧耗时 < 3s, 且首帧 seq=0.

    实现要点: 上电首条横幅不可靠 (固件在 harness 打开 pty 前即已打印),
    故先用 board.reset() 显式复位同步流位置; 复位后确认遥测流在跑
    (拿到一帧, 此后旧流残余帧序号恒 > 0), 因而下一次复位后第一帧
    seq==0 的 DATA 帧可唯一识别为新一轮启动的首帧; 计时窗口内全程
    自行 pump, 不经 banner 等待, 避免首帧被 wait_text 吞掉造成误判。
    """
    board.reset(timeout=BOOT_BANNER_TIMEOUT)  # 同步: 显式复位并消费新横幅
    pre = board.uart.wait_frame(type=TYPE_DATA, timeout=BOOT_BANNER_TIMEOUT)
    assert pre.seq >= 0  # 仅确认旧流在跑

    t0 = time.monotonic()
    board._run(board.qmp.system_reset(resume=True))
    deadline = t0 + FIRST_FRAME_TIMEOUT
    first = None
    while time.monotonic() < deadline:
        for fr in board.uart._pump():  # noqa: SLF001  与 harness.fixture 同包用法
            if fr.type == TYPE_DATA and fr.seq == 0:
                first = fr
                break
        if first is not None:
            break
        time.sleep(board.uart.poll_interval)

    assert first is not None, (
        f"复位后 {FIRST_FRAME_TIMEOUT}s 内未收到 seq=0 的首条数据帧"
    )
    elapsed = time.monotonic() - t0
    assert elapsed < FIRST_FRAME_TIMEOUT, (
        f"首帧耗时 {elapsed:.3f}s 超过阈值 {FIRST_FRAME_TIMEOUT}s"
    )
    assert first.seq == 0


def test_tc_boot_003_task_init_evidence(board):
    """TC-BOOT-003 (FR-05): STAT? 回显给出任务初始化证据: tasks=4 且 hb 字段存在."""
    board.reset()
    line = stat_query(board)
    m = STAT_RE.search(line)
    assert m is not None, f"STAT? 回显格式不符: {line!r}"
    tasks = int(m.group(1))
    assert tasks == 4, f"期望 4 个任务 (sensor/comm/diag/watchdog), 实际 {tasks}"
    assert m.group(4) is not None  # hb 字段存在 (数值由 TC-FUNC-005 校验)


def test_tc_boot_004_reset_registers(board):
    """TC-BOOT-004 (NFR-02): 复位后寄存器上下文合法.

    QMP system_reset 后 CPU 立即投入运行 (QEMU 行为), 无法停在复位向量入口,
    因此本用例断言的是"复位后任意时刻采样到的寄存器处于合法地址范围":
      - SP (r13) ∈ SRAM [0x20000000, 0x2000C000]
      - PC (r15) ∈ Flash [0x08000000, 0x08040000]
      - XPSR.T (bit24) = 1 (Thumb 状态, Cortex-M3 恒定)
    采样后立即 cont 恢复运行, 避免影响板卡状态。
    """
    board._run(board.qmp.system_reset(resume=True))
    try:
        board._run(board.qmp.stop())
        regs = board._run(board.qmp.info_registers())
    finally:
        board._run(board.qmp.cont())

    sp = regs.get("sp", regs.get("r13"))
    pc = regs.get("pc", regs.get("r15"))
    xpsr = regs.get("xpsr")
    assert sp is not None and SRAM_BASE <= sp <= SRAM_END, (
        f"SP={sp and hex(sp)} 不在 SRAM 范围 [{hex(SRAM_BASE)}, {hex(SRAM_END)}]"
    )
    assert pc is not None and FLASH_BASE <= pc <= FLASH_END, (
        f"PC={pc and hex(pc)} 不在 Flash 范围 [{hex(FLASH_BASE)}, {hex(FLASH_END)}]"
    )
    assert xpsr is not None and (xpsr >> XPSR_THUMB_BIT) & 1 == 1, (
        f"XPSR={xpsr and hex(xpsr)} 未处于 Thumb 状态"
    )


def test_tc_boot_005_qmp_stop_cont(board):
    """TC-BOOT-005 (NFR-03): QMP stop/cont 平台控制冒烟.

    stop 后 query-status 应为 paused (running=false);
    cont 后应恢复 running (running=true)。
    """
    try:
        board._run(board.qmp.stop())
        st = board._run(board.qmp.query_status())
        assert st["status"] == "paused" and st["running"] is False, (
            f"stop 后状态异常: {st}"
        )
        board._run(board.qmp.cont())
        st = board._run(board.qmp.query_status())
        assert st["status"] == "running" and st["running"] is True, (
            f"cont 后状态异常: {st}"
        )
    finally:
        board._run(board.qmp.cont())  # 兜底: 不留暂停态
