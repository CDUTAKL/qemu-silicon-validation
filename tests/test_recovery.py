"""TC-RCV: 恢复用例 (TC-RCV-001 ~ 005).

被测对象: QEMU -M stm32vldiscovery 上运行的 FreeRTOS 固件。
每条用例的 docstring 标注关联需求 ID (见 tests/README.md RTM 表)。

看门狗语义 (读 firmware/Core/watchdog_task.c + app.c 确认, manual_check.py
用例 12 实测验证):
    hb_count 只由 WatchdogTask 每 500ms 递增; app.c 的 1s 软件定时器
    (hb_check_cb) 发现 hb_count 停增即 NVIC_SystemReset()。
    因此触发看门狗复位的条件是 "FAULT HANG watchdog" (挂起 WatchdogTask
    本身); 挂起 sensor/comm 不会停 hb, 不会复位 (该面由 TC-FLT-006 覆盖)。

两条实测坑 (本文件实现已规避):
  1. 上电首条 BOOT 横幅不可靠: 固件在 harness 打开 pty 前就已打印
     (test_boot.py TC-BOOT-001/002 观察到同样现象)。故每个用例开头用
     board.reset() 显式复位同步流位置, 之后的等待均针对复位后的新流。
  2. harness 的 wait_text 在 pump 时会丢弃同批解析出的帧, 而复位后
     BOOT 横幅与 seq=0 首帧常落在同一批字节里; 因此复位后的自举等待
     一律走 _await_reboot() (自 pump, 帧与文本行同时收集, 互不吞没)。
"""

from __future__ import annotations

import re
import time

from harness.uart import TYPE_DATA, UartTimeoutError

from conftest import BOOT_PATTERN, STAT_RE, send_text_command, stat_query

BOOT_BANNER_TIMEOUT = 5.0    # NFR-01: 复位后启动横幅上限
FIRST_FRAME_TIMEOUT = 3.0    # 复位->首条数据帧(seq=0)上限
WATCHDOG_RESET_TIMEOUT = 8.0 # HANG watchdog -> 1s 定时器复位 -> 横幅, 留足余量

# TC-RCV-004 冻结时长: 需求原文 30s, 本用例取 5s —— 冻结/恢复语义与时长无关
# (QMP stop 后 guest 时钟整体停走), 5s 已 50 倍于 100ms 帧周期, 足以验证
# 冻结期无帧、恢复后序号衔接, 同时控制套件整体耗时。
FREEZE_DURATION = 5.0

# harness.qmp 的后台读循环对每次 readline 设了 5s 超时: QMP 静默超过 5s
# 读泵即自尽, 后续命令全部超时。QMP 空闲期 (如 guest 冻结) 必须周期性
# 发 query-status 保活 (响应本身就是 QMP 流量, 可刷新读泵窗口)。
QMP_KEEPALIVE_PERIOD = 2.0


def _await_reboot(board, timeout: float, *, expect_echo: str | None = None) -> dict:
    """等复位后的自举证据: BOOT 横幅 + seq=0 首条数据帧 (+可选命令回显).

    自 pump 并同时收集帧与文本行 (wait_text 会吞掉同批帧, 见模块 docstring)。
    复位前旧流残余帧 seq 恒 > 0, 新流从 0 重新打号, 故 seq==0 的 DATA 帧
    可唯一识别为复位后的首帧 (与 test_boot.py TC-BOOT-002 同一判据)。

    Returns:
        dict: banner/banner_at (相对调用时刻秒), echo, first (Frame),
        first_at, elapsed。超时未凑齐时对应字段为 None。
    """
    t0 = time.monotonic()
    deadline = t0 + timeout
    banner = echo = first = None
    banner_at = first_at = None
    boot_rx = re.compile(BOOT_PATTERN)
    echo_rx = re.compile(expect_echo) if expect_echo else None
    while time.monotonic() < deadline and (
        banner is None or first is None or (echo_rx is not None and echo is None)
    ):
        for fr in board.uart._pump():  # noqa: SLF001  与 harness.fixture 同包用法
            if first is None and fr.type == TYPE_DATA and fr.seq == 0:
                first, first_at = fr, time.monotonic() - t0
        while True:
            line = board.uart.parser.pop_text_line()
            if line is None:
                break
            if banner is None and boot_rx.search(line):
                banner, banner_at = line, time.monotonic() - t0
            if echo_rx is not None and echo is None and echo_rx.search(line):
                echo = line
        time.sleep(board.uart.poll_interval)
    return {
        "banner": banner, "banner_at": banner_at,
        "echo": echo, "first": first, "first_at": first_at,
        "elapsed": time.monotonic() - t0,
    }


def test_tc_rcv_001_qmp_system_reset_reboot(board):
    """TC-RCV-001 (NFR-01): QMP system_reset 后 5s 内完成自举.

    断言: 复位 -> BOOT 横幅复现且耗时 < 5s; 新遥测流 seq 从 0 重新开始
    (复位后捕获 seq=0 的首条数据帧)。
    """
    board.reset(timeout=BOOT_BANNER_TIMEOUT)  # 同步: 显式复位并消费新横幅

    board._run(board.qmp.system_reset(resume=True))  # noqa: SLF001
    r = _await_reboot(board, BOOT_BANNER_TIMEOUT + FIRST_FRAME_TIMEOUT)

    assert r["banner"] is not None, "复位后 5s 内未捕获 BOOT 横幅"
    assert r["banner_at"] < BOOT_BANNER_TIMEOUT, (
        f"自举耗时 {r['banner_at']:.3f}s 超过 {BOOT_BANNER_TIMEOUT}s"
    )
    assert r["first"] is not None, "复位后未捕获 seq=0 的首条数据帧 (seq 未从 0 重启)"


def test_tc_rcv_002_reset_command_reboot(board):
    """TC-RCV-002 (NFR-01): RESET 文本命令软复位, 5s 内自举且 seq 从 0 重启.

    固件对 RESET 先回显 "OK RESET" 再 NVIC_SystemReset() (见 diag_task.c),
    故依次断言: OK RESET 回显 -> BOOT 横幅 -> 首帧 seq=0。
    """
    board.reset(timeout=BOOT_BANNER_TIMEOUT)  # 同步: 显式复位并消费新横幅
    board.uart.wait_frame(type=TYPE_DATA, timeout=BOOT_BANNER_TIMEOUT)  # 确认旧流在跑

    send_text_command(board, "RESET")
    r = _await_reboot(
        board, BOOT_BANNER_TIMEOUT + FIRST_FRAME_TIMEOUT, expect_echo=r"^OK RESET"
    )

    assert r["echo"] is not None, "RESET 命令未收到 OK RESET 回显"
    assert r["banner"] is not None, "软复位后 5s 内未捕获 BOOT 横幅"
    assert r["banner_at"] < BOOT_BANNER_TIMEOUT, (
        f"软复位自举耗时 {r['banner_at']:.3f}s 超过 {BOOT_BANNER_TIMEOUT}s"
    )
    assert r["first"] is not None, "软复位后未捕获 seq=0 的首条数据帧 (seq 未从 0 重启)"


def test_tc_rcv_003_watchdog_reset(board):
    """TC-RCV-003 (FR-09): 看门狗复位 —— FAULT HANG watchdog 触发系统复位并自举恢复.

    触发条件 (以固件实际实现为准): hb_count 仅由 WatchdogTask 递增, 挂起
    WatchdogTask 后 hb 停增, app.c 的 1s 软件定时器下一次检查即
    NVIC_SystemReset()。本用例未下发任何 RESET 命令, 横幅复现本身即证明
    复位由看门狗路径触发; 复位后断言遥测恢复 (seq 从 0 重启) 且 STAT? 正常。
    """
    board.reset(timeout=BOOT_BANNER_TIMEOUT)  # 同步: 显式复位并消费新横幅

    # 注入前确认 hb 在递增 (看门狗活着)
    m0 = STAT_RE.search(stat_query(board))
    assert m0 is not None
    time.sleep(1.2)
    m1 = STAT_RE.search(stat_query(board))
    assert m1 is not None
    assert int(m1.group(4)) > int(m0.group(4)), (
        f"注入前 hb 应递增: {m0.group(4)} -> {m1.group(4)}"
    )

    send_text_command(board, "FAULT HANG watchdog")
    r = _await_reboot(
        board, WATCHDOG_RESET_TIMEOUT + FIRST_FRAME_TIMEOUT,
        expect_echo=r"^OK FAULT HANG",
    )

    assert r["echo"] is not None, "FAULT HANG watchdog 未收到 OK 回显"
    assert r["banner"] is not None, (
        f"HANG watchdog 后 {WATCHDOG_RESET_TIMEOUT}s 内未见 BOOT 横幅 "
        "(看门狗未触发复位)"
    )
    assert r["first"] is not None, "看门狗复位后未捕获 seq=0 的首条数据帧"

    m2 = STAT_RE.search(stat_query(board))
    assert m2 is not None, "看门狗复位后 STAT? 无应答"
    assert int(m2.group(1)) == 4, f"复位后 tasks 应为 4: {m2.group(0)!r}"


def test_tc_rcv_004_qmp_freeze_resume(board):
    """TC-RCV-004 (NFR-03): QMP stop 冻结 -> cont 恢复, 通信恢复且帧序号衔接.

    允许 stop 期间无帧 (guest 时钟停走); cont 后第一帧 seq 必须恰好衔接
    冻结前最后一帧 (+1), 随后遥测流持续。冻结时长取 5s (原为 30s, 理由见
    模块常量 FREEZE_DURATION 注释)。冻结期间周期性 query-status 既是
    "仍在暂停态"的断言, 也是 QMP 读泵保活 (见 QMP_KEEPALIVE_PERIOD 注释)。
    """
    board.reset(timeout=BOOT_BANNER_TIMEOUT)  # 同步: 显式复位并消费新横幅

    before = board.uart.collect_frames(1.0)
    assert before, "冻结前未收到任何帧"
    last_seq = before[-1].seq

    try:
        board._run(board.qmp.stop())  # noqa: SLF001
        st = board._run(board.qmp.query_status())  # noqa: SLF001
        assert st["running"] is False, f"stop 未生效: {st}"

        deadline = time.monotonic() + FREEZE_DURATION
        while time.monotonic() < deadline:
            time.sleep(min(QMP_KEEPALIVE_PERIOD, deadline - time.monotonic()))
            st = board._run(board.qmp.query_status())  # noqa: SLF001  保活+断言
            assert st["running"] is False, f"冻结期间 guest 意外恢复运行: {st}"

        during = board.uart.collect_frames(0.5)
        assert len(during) <= 1, (
            f"冻结期间不应有新帧产生 (容忍 stop 前已在路上的残余): {len(during)}"
        )
        last_seen = during[-1].seq if during else last_seq  # 残余帧也算进衔接链

        board._run(board.qmp.cont())  # noqa: SLF001
        st = board._run(board.qmp.query_status())  # noqa: SLF001
        assert st["running"] is True, f"cont 未生效: {st}"
    finally:
        board._run(board.qmp.cont())  # noqa: SLF001  兜底: 不留暂停态

    try:
        nxt = board.uart.wait_frame(timeout=BOOT_BANNER_TIMEOUT)
    except UartTimeoutError:
        raise AssertionError("cont 后 5s 内通信未恢复 (无任何帧)") from None
    assert (nxt.seq - last_seen) & 0xFFFF == 1, (
        f"冻结恢复后 seq 未衔接: 冻结前 last={last_seen}, 恢复后 first={nxt.seq}"
    )

    after = board.uart.collect_frames(1.0)
    n_data = sum(1 for f in after if f.type == TYPE_DATA)
    assert n_data >= 5, f"cont 后遥测未恢复正常: 1s 仅 {n_data} 帧数据"


def test_tc_rcv_005_reset_loop_stability(board):
    """TC-RCV-005 (NFR-01): QMP system_reset x20 次, 每次均在 5s 内完成自举.

    每轮复位产生且仅产生一条 BOOT 横幅, 逐轮经 _await_reboot 消费, 不会
    串轮; 记录每轮横幅耗时, 全部 < 5s; 循环结束后再确认遥测流活着
    (能收到数据帧)。
    """
    board.reset(timeout=BOOT_BANNER_TIMEOUT)  # 同步: 显式复位并消费新横幅

    samples: list[float] = []
    for i in range(20):
        board._run(board.qmp.system_reset(resume=True))  # noqa: SLF001
        r = _await_reboot(board, BOOT_BANNER_TIMEOUT)
        assert r["banner"] is not None, f"第 {i} 轮复位后 5s 内未捕获 BOOT 横幅"
        samples.append(r["banner_at"])

    worst = max(samples)
    assert worst < BOOT_BANNER_TIMEOUT, (
        f"20 轮复位中最慢自举 {worst:.3f}s 超过 {BOOT_BANNER_TIMEOUT}s"
    )

    board.uart.wait_frame(type=TYPE_DATA, timeout=FIRST_FRAME_TIMEOUT)
