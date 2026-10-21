#!/usr/bin/env python3
"""
manual_check.py - W1 acceptance smoke test for the DUT firmware.

Spawns its own QEMU (stm32vldiscovery), opens the serial pty, then verifies:

  [01] boot banner            BOOT v1.0.0 tasks:4 q:64 (via QMP system_reset)
  [02] data frames            type 0x01, CRC16-CCITT ok, seq from 0 contiguous
  [03] heartbeat frames       type 0x03 every 1 s, bitmap 0x0F, stack HWM sane
  [04] STAT?                  exact "OK STAT ..." layout, run/seq consistent
                              with the number of frames received
  [05] FAULT OV 15            alarm fid=1 level=2 value=15 BEFORE next data
                              frame; next data frame voltage forced to 15.0
  [06] FAULT OC 2.5           alarm fid=2 level=2, current forced to 2.5
  [07] ACK request            host type 0x04 -> DUT type 0x05, seq continues
  [08] FAULT DISCONN/CONN     data frames pause, heartbeat keeps ticking
  [09] FAULT HANG/RESUME      sensor task suspended (tasks=3, bitmap 1110)
                              and recovered
  [10] RESET command          NVIC_SystemReset, banner again, seq restarts
  [11] reproducible data      first data frames bit-identical across resets
  [12] watchdog recovery      HANG watchdog -> hb stall -> reset -> banner

Exit code 0 iff every check passes.
"""

import fcntl
import json
import os
import re
import select
import socket
import struct
import subprocess
import sys
import termios
import time
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
FW_BIN = os.path.join(HERE, "build", "firmware.bin")
QMP_SOCK = "/tmp/qmp-11.sock"
BANNER = b"BOOT v1.0.0 tasks:4 q:64"

# ---------------------------------------------------------------- protocol

PAYLOAD_LEN = {0x01: 12, 0x02: 4, 0x03: 3, 0x04: 2, 0x05: 2}


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def build_frame(ftype: int, payload: bytes, seq: int) -> bytes:
    body = bytes([0xAA, 0x55, (seq >> 8) & 0xFF, seq & 0xFF, ftype]) + payload
    return body + crc16_ccitt(body).to_bytes(2, "big")


class Parser:
    """Byte-stream splitter: text lines + binary frames, CRC-guarded resync.

    The DUT only ever writes whole messages (one frame or one text line),
    so message boundaries on the wire are always clean.
    """

    def __init__(self):
        self.buf = b""
        self.frames = []   # (seq, type, payload, crc_ok)
        self.lines = []    # bytes, without trailing \n/\r
        self.events = []   # ("f", seq) / ("l", line) in wire order
        self.crc_errors = 0
        self.resyncs = 0

    def _line(self, raw: bytes):
        line = raw.replace(b"\r", b"")
        if line:
            self.lines.append(line)
            self.events.append(("l", line))

    def feed(self, data: bytes):
        self.buf += data
        while True:
            k = self.buf.find(b"\xAA\x55")
            if k < 0:
                # frame-free region: emit complete lines, keep the partial one
                while True:
                    nl = self.buf.find(b"\n")
                    if nl < 0:
                        if len(self.buf) > 512:
                            self.buf = b""
                        return
                    self._line(self.buf[:nl])
                    self.buf = self.buf[nl + 1:]

            if k > 0:
                seg = self.buf[:k]
                while True:
                    nl = seg.find(b"\n")
                    if nl < 0:
                        break
                    self._line(seg[:nl])
                    seg = seg[nl + 1:]
                if seg:
                    self._line(seg)
                self.buf = self.buf[k:]

            # buf starts with a frame header
            if len(self.buf) < 5:
                return
            plen = PAYLOAD_LEN.get(self.buf[4])
            if plen is None:
                # not a known type: bogus marker, skip the AA55 and resync
                self.buf = self.buf[2:]
                self.resyncs += 1
                continue
            total = 7 + plen
            if len(self.buf) < total:
                return
            wire = self.buf[: 5 + plen]
            crc = (self.buf[5 + plen] << 8) | self.buf[6 + plen]
            if crc16_ccitt(wire) != crc:
                # corrupt byte somewhere: drop the header, rescan
                self.buf = self.buf[2:]
                self.crc_errors += 1
                continue
            seq = (wire[2] << 8) | wire[3]
            self.frames.append((seq, wire[4], wire[5:], True))
            self.events.append(("f", seq))
            self.buf = self.buf[total:]

    def take(self):
        frames, lines, events = self.frames, self.lines, self.events
        self.frames, self.lines, self.events = [], [], []
        return frames, lines, events


# ---------------------------------------------------------------- qemu i/o

class Dut:
    def __init__(self):
        if not os.path.exists(FW_BIN):
            raise SystemExit(f"missing {FW_BIN} - run build.sh first")
        try:
            os.unlink(QMP_SOCK)
        except FileNotFoundError:
            pass
        self.proc = subprocess.Popen(
            [
                "qemu-system-arm",
                "-M", "stm32vldiscovery",
                "-kernel", FW_BIN,
                "-nographic",
                "-monitor", "none",
                "-serial", "pty",
                "-qmp", f"unix:{QMP_SOCK},server=on,wait=off",
            ],
            stdout=subprocess.PIPE,   # "char device redirected to ..." lands here
            stderr=subprocess.DEVNULL,
        )
        self.pty = self._wait_pty()
        self.fd = os.open(self.pty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(self.fd)
        self.parser = Parser()
        self.frames = []       # frames since last reset_streams()
        self.lines = []        # lines since last reset_streams()
        self.events = []       # ("f", seq)/("l", line) since last reset_streams()
        self.total_frames = 0  # CRC-ok frames since last mark_reset()
        self.last_seq = None   # last CRC-ok frame seq seen

    def _wait_pty(self, timeout=10.0):
        t0 = time.time()
        data = b""
        while time.time() - t0 < timeout:
            r, _, _ = select.select([self.proc.stdout], [], [], 0.2)
            if r:
                chunk = os.read(self.proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                data += chunk
                m = re.search(rb"/dev/ttys[0-9]+", data)
                if m:
                    return m.group(0).decode()
            if self.proc.poll() is not None:
                break
        raise SystemExit(f"no pty from QEMU; stdout so far: {data!r}")

    def qmp_reset(self):
        """Reset the guest through the QMP monitor."""
        self.qmp_log = []
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(QMP_SOCK)
        f = s.makefile("rw")

        def cmd(c):
            f.write(json.dumps(c) + "\n")
            f.flush()
            while True:
                msg = json.loads(f.readline())
                if "return" in msg or "error" in msg:
                    return msg

        self.qmp_log.append(f.readline())  # greeting
        self.qmp_log.append(cmd({"execute": "qmp_capabilities"}))
        self.qmp_log.append(cmd({"execute": "system_reset"}))
        s.close()

    def pump(self, duration):
        end = time.time() + duration
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], min(0.05, end - time.time()))
            if not r:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                continue
            self.parser.feed(chunk)
            frames, lines, events = self.parser.take()
            self.frames.extend(frames)
            self.lines.extend(lines)
            self.events.extend(events)
            for seq, _, _, _ in frames:
                self.total_frames += 1
                self.last_seq = seq

    def send_line(self, s: str):
        os.write(self.fd, s.encode() + b"\n")

    def send_frame(self, ftype: int, payload: bytes, seq: int):
        os.write(self.fd, build_frame(ftype, payload, seq))

    def reset_streams(self):
        self.frames.clear()
        self.lines.clear()
        self.events.clear()

    def mark_reset(self):
        """Call after any full-chip reset: seq counters restart at 0."""
        self.reset_streams()
        self.total_frames = 0
        self.last_seq = None

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            os.unlink(QMP_SOCK)
        except FileNotFoundError:
            pass


def lock_path(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


# ---------------------------------------------------------------- checks

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def f32be(b):
    return struct.unpack(">f", b)[0]


def wait_line(dut, pred, timeout):
    """Pump until a line matching pred(bytes)->bool appears. Returns the line."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        dut.pump(0.1)
        for ln in dut.lines:
            if pred(ln):
                return ln
    return None


def wait_frame(dut, pred, timeout):
    """Pump until a frame matching pred(seq,type,payload)->bool appears."""
    t0 = time.time()
    seen = 0
    while time.time() - t0 < timeout:
        dut.pump(0.1)
        while seen < len(dut.frames):
            seq, ty, pl, _ = dut.frames[seen]
            seen += 1
            if pred(seq, ty, pl):
                return dut.frames[seen - 1]
    return None


def post_reset_frames(dut):
    """Slice dut.frames at the first seq-0 frame.

    Stale pre-reset frames still sitting in the pty buffer land in
    dut.frames ahead of the post-reset stream; the post-reset stream is
    exactly the suffix that starts at seq 0.
    """
    idx = next((i for i, f in enumerate(dut.frames) if f[0] == 0), None)
    return dut.frames[idx:] if idx is not None else []


def main():
    lock = lock_path("/tmp/manual_check.lock")
    if lock is None:
        print("another manual_check.py instance is running (lock held)")
        return 2
    try:
        return main_locked()
    finally:
        os.close(lock)


def main_locked():
    dut = Dut()
    try:
        boot_samples = phase_banner(dut)
        phase_data_frames(dut)
        phase_heartbeat(dut)
        phase_stat(dut)
        phase_fault_ov(dut)
        phase_fault_oc(dut)
        phase_ack(dut)
        phase_disconn_conn(dut)
        phase_hang_resume(dut)
        reset_samples = phase_reset(dut)
        phase_repro(dut, boot_samples, reset_samples)
        phase_watchdog(dut)
    finally:
        dut.close()

    print("\n================ W1 smoke summary ================")
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"  {len(RESULTS) - n_fail}/{len(RESULTS)} checks passed")
    return 0 if n_fail == 0 else 1


def phase_banner(dut):
    """Deterministic banner check: pty is open, then QMP resets the guest."""
    dut.pump(0.5)
    dut.mark_reset()
    debug = os.environ.get("MC_DEBUG") == "1"
    if debug:
        print(f"[debug] qemu pid={dut.proc.pid} pty={dut.pty}", flush=True)
        os.system(f"ps -o pid,ppid,command -p {dut.proc.pid}")
    dut.qmp_reset()
    if debug:
        print(f"[debug] qmp_log={dut.qmp_log}", flush=True)
    ln = wait_line(dut, lambda l: l.startswith(BANNER), 6.0)
    if debug:
        print(f"[debug] after wait: lines={dut.lines} last_seq={dut.last_seq} "
              f"total={dut.total_frames}", flush=True)
    check("01 boot banner 'BOOT v1.0.0 tasks:4 q:64' (QMP system_reset)",
          ln is not None,
          f"banner={ln!r} qmp={getattr(dut, 'qmp_log', None)}" if ln is None
          else f"banner={ln!r}")
    # First 3 data payloads of the post-reset stream (starts at seq 0).
    t0 = time.time()
    while True:
        data = [f[2] for f in post_reset_frames(dut) if f[1] == 0x01]
        if len(data) >= 3 or time.time() - t0 >= 3.0:
            return data[:3]
        dut.pump(0.1)


def phase_data_frames(dut):
    # Fresh boot via QMP; the post-reset stream must start at seq 0 and
    # increment by one across all frame types. Stale pre-reset frames that
    # were still buffered on the pty are sliced off by post_reset_frames().
    dut.qmp_reset()
    dut.mark_reset()
    if wait_line(dut, lambda l: l.startswith(BANNER), 6.0) is None:
        check("02 data frames: >=8 in 2.5s, CRC ok, seq 0-based contiguous",
              False, "no banner after QMP reset")
        return
    dut.pump(2.5)
    frames = post_reset_frames(dut)
    seqs = [f[0] for f in frames]
    data = [f for f in frames if f[1] == 0x01]
    contiguous = bool(seqs) and seqs[0] == 0 and all(
        b == a + 1 for a, b in zip(seqs, seqs[1:])
    )
    sane = all(
        5.0 < f32be(f[2][0:4]) < 20.0 and 0.0 < f32be(f[2][4:8]) < 5.0
        and -20.0 < f32be(f[2][8:12]) < 80.0
        for f in data
    )
    ok = (len(data) >= 8 and contiguous and sane
          and dut.parser.crc_errors == 0)
    check("02 data frames: >=8 in 2.5s, CRC ok, seq 0-based contiguous",
          ok, f"data={len(data)} total={len(seqs)} contiguous={contiguous} "
             f"sane={sane} crc_err={dut.parser.crc_errors} resync={dut.parser.resyncs}")


def phase_heartbeat(dut):
    dut.reset_streams()
    dut.pump(2.6)
    hbs = [f for f in dut.frames if f[1] == 0x03]
    ok = len(hbs) >= 2
    detail = f"count={len(hbs)}"
    if hbs:
        bmp = hbs[0][2][0]
        hwm = (hbs[0][2][1] << 8) | hbs[0][2][2]
        detail += f" bitmap={bmp:04b} hwm={hwm}B"
        ok = ok and bmp == 0x0F and 0 < hwm < 2048
    check("03 heartbeat frame 1s cadence, bitmap=1111, sane stack HWM", ok, detail)


def phase_stat(dut):
    # Self-contained: fresh boot, then compare run/seq against the exact
    # number of frames put on the wire between the banner and the OK STAT
    # reply. DiagTask (prio 3) cannot be preempted by CommTask (prio 2)
    # between sampling g_run_count and writing the reply, so the wire
    # count is deterministic.
    dut.qmp_reset()
    dut.mark_reset()
    if wait_line(dut, lambda l: l.startswith(BANNER), 6.0) is None:
        check("04 STAT? reply", False, "no banner after QMP reset")
        return
    # hb increments every 500 ms; wait out the first tick so hb > 0.
    if wait_frame(dut, lambda s, t, p: t == 0x03, 2.5) is None:
        check("04 STAT? reply", False, "no heartbeat frame after reset")
        return
    dut.send_line("STAT?")
    stat = wait_line(dut, lambda l: l.startswith(b"OK STAT"), 3.0)
    if stat is None:
        check("04 STAT? reply", False, "no OK STAT line")
        return
    m = re.fullmatch(
        rb"OK STAT tasks=(\d+) qdepth=(\d+) run=(\d+) hb=(\d+) seq=(\d+)", stat
    )
    if not m:
        check("04 STAT? reply", False, f"bad format: {stat!r}")
        return
    tasks, qdepth, run, hb, seq = (int(g) for g in m.groups())
    # Count frames on the wire after the (last) banner, up to the reply.
    start = max(
        (i for i, (k, v) in enumerate(dut.events)
         if k == "l" and v.startswith(BANNER)),
        default=-1,
    ) + 1
    n_wire = 0
    for k, _ in dut.events[start:]:
        if k == "l":
            break  # the OK STAT reply line itself
        n_wire += 1
    ok = (tasks == 4 and 0 <= qdepth <= 64 and run == n_wire
          and seq == n_wire and hb > 0)
    check("04 STAT? exact field layout, run/seq match received frame count",
          ok, f"tasks={tasks} qdepth={qdepth} run={run} hb={hb} seq={seq} "
              f"(frames on wire before reply={n_wire})")


def phase_fault_ov(dut):
    dut.reset_streams()
    dut.send_line("FAULT OV 15")
    got_ok = wait_line(dut, lambda l: l == b"OK FAULT OV", 2.0) is not None
    alarm = wait_frame(dut, lambda s, t, p: t == 0x02, 3.0)
    nxt = None
    if alarm:
        nxt = wait_frame(dut, lambda s, t, p, a=alarm[0]: t == 0x01 and s == a + 1, 3.0)
    ok = got_ok and alarm is not None
    detail = f"echo={got_ok}"
    if alarm:
        fid, lvl, val = alarm[2][0], alarm[2][1], (alarm[2][2] << 8) | alarm[2][3]
        ok = ok and fid == 1 and lvl == 2 and val == 15
        detail += f" alarm(fid={fid} lvl={lvl} val={val})"
    if nxt:
        v = f32be(nxt[2][0:4])
        ok = ok and abs(v - 15.0) < 1e-6
        detail += f" next_data_v={v}"
    else:
        ok = False
        detail += " next_data=MISSING"
    check("05 FAULT OV 15: alarm(1,2,15) before next data, v forced to 15", ok, detail)


def phase_fault_oc(dut):
    dut.reset_streams()
    dut.send_line("FAULT OC 2.5")
    got_ok = wait_line(dut, lambda l: l == b"OK FAULT OC", 2.0) is not None
    alarm = wait_frame(dut, lambda s, t, p: t == 0x02 and p[0] == 2, 3.0)
    nxt = None
    if alarm:
        nxt = wait_frame(dut, lambda s, t, p, a=alarm[0]: t == 0x01 and s == a + 1, 3.0)
    ok = got_ok and alarm is not None
    detail = f"echo={got_ok}"
    if alarm:
        ok = ok and alarm[2][1] == 2
        detail += f" alarm(fid={alarm[2][0]} lvl={alarm[2][1]})"
    if nxt:
        i = f32be(nxt[2][4:8])
        ok = ok and abs(i - 2.5) < 1e-6
        detail += f" next_data_i={i}"
    else:
        ok = False
        detail += " next_data=MISSING"
    check("06 FAULT OC 2.5: alarm(2,2) + current forced to 2.5", ok, detail)


def phase_ack(dut):
    dut.reset_streams()
    before = dut.last_seq
    dut.send_frame(0x04, b"\x00\x01", seq=0x1234)  # ACK request (host seq is arbitrary)
    ack = wait_frame(dut, lambda s, t, p: t == 0x05, 3.0)
    ok = ack is not None and ack[2] == b"\x00\x00"
    if ack:
        # The ACK must be the very next frame on the wire.
        contiguous = before is None or ack[0] == before + 1
        ok = ok and contiguous
        detail = f"ack seq={ack[0]} prev={before} status={ack[2].hex()} contiguous={contiguous}"
    else:
        detail = "no 0x05 ACK frame"
    check("07 ACK request 0x04 -> reply 0x05, seq contiguous", ok, detail)


def phase_disconn_conn(dut):
    dut.reset_streams()
    got_ok = True
    dut.send_line("FAULT DISCONN")
    if wait_line(dut, lambda l: l == b"OK FAULT DISCONN", 2.0) is None:
        got_ok = False
    dut.reset_streams()
    dut.pump(2.2)
    data = [f for f in dut.frames if f[1] == 0x01]
    hbs = [f for f in dut.frames if f[1] == 0x03]
    paused_ok = got_ok and len(data) == 0 and len(hbs) >= 1

    dut.reset_streams()
    dut.send_line("FAULT CONN")
    got_ok2 = wait_line(dut, lambda l: l == b"OK FAULT CONN", 2.0) is not None
    dut.reset_streams()
    dut.pump(1.0)
    data2 = [f for f in dut.frames if f[1] == 0x01]
    check("08 FAULT DISCONN pauses data (heartbeat on), CONN resumes",
          paused_ok and got_ok2 and len(data2) >= 3,
          f"paused_ok={paused_ok} (data={len(data)} hb={len(hbs)}) "
          f"conn_echo={got_ok2} resumed_data={len(data2)}")


def phase_hang_resume(dut):
    dut.reset_streams()
    dut.send_line("FAULT HANG sensor")
    got_ok = wait_line(dut, lambda l: l == b"OK FAULT HANG", 2.0) is not None
    dut.reset_streams()
    dut.pump(1.5)
    data = [f for f in dut.frames if f[1] == 0x01]
    hbs = [f for f in dut.frames if f[1] == 0x03]
    bmp = hbs[0][2][0] if hbs else None

    dut.reset_streams()
    dut.send_line("STAT?")
    tasks = None
    ln = wait_line(dut, lambda l: l.startswith(b"OK STAT"), 3.0)
    if ln:
        m = re.match(rb"OK STAT tasks=(\d+)", ln)
        if m:
            tasks = int(m.group(1))
    hung_ok = got_ok and len(data) == 0 and len(hbs) >= 1 and tasks == 3 and bmp == 0x0E

    dut.reset_streams()
    dut.send_line("RESUME sensor")
    got_ok2 = wait_line(dut, lambda l: l == b"OK RESUME", 2.0) is not None
    dut.reset_streams()
    dut.pump(1.0)
    data2 = [f for f in dut.frames if f[1] == 0x01]
    check("09 FAULT HANG sensor: tasks=3, data stops; RESUME recovers",
          hung_ok and got_ok2 and len(data2) >= 3,
          f"echo={got_ok} data_stopped={len(data) == 0} hb={len(hbs)} "
          f"bitmap={f'{bmp:04b}' if bmp is not None else None} tasks={tasks} "
          f"resume_echo={got_ok2} resumed={len(data2)}")


def phase_reset(dut):
    """RESET command -> OK echo + banner again + seq restarts at 0."""
    dut.reset_streams()
    dut.send_line("RESET")
    got_ok = wait_line(dut, lambda l: l == b"OK RESET", 2.0) is not None
    ln = wait_line(dut, lambda l: l.startswith(BANNER), 6.0)
    # Frames seen so far may include pre-reset frames still buffered on the
    # pty; the post-reset stream is the suffix starting at seq 0.
    dut.pump(1.2)
    frames = post_reset_frames(dut)
    seqs = [f[0] for f in frames]
    ok = (got_ok and ln is not None and seqs and seqs[0] == 0
          and all(b == a + 1 for a, b in zip(seqs, seqs[1:])))
    check("10 RESET command: OK echo, banner again, seq restarts at 0",
          ok, f"echo={got_ok} banner={ln is not None} first_seqs={seqs[:6]}")
    return [f[2] for f in frames if f[1] == 0x01][:3]


def phase_repro(dut, boot_samples, reset_samples):
    ok = (len(boot_samples) >= 3 and len(reset_samples) >= 3
          and boot_samples[:3] == reset_samples[:3])
    check("11 sensor data reproducible across resets (bit-exact)", ok,
          f"boot_samples={len(boot_samples)} reset_samples={len(reset_samples)} "
          f"first_equal={boot_samples[:1] == reset_samples[:1]}")


def phase_watchdog(dut):
    """Hang the watchdog task: hb stalls -> NVIC_SystemReset -> banner."""
    dut.reset_streams()
    dut.send_line("FAULT HANG watchdog")
    got_ok = wait_line(dut, lambda l: l == b"OK FAULT HANG", 2.0) is not None
    ln = wait_line(dut, lambda l: l.startswith(BANNER), 8.0)
    recovered = False
    hb_seen = None
    if ln is not None:
        dut.mark_reset()
        t1 = time.time()
        while time.time() - t1 < 5.0:
            dut.send_line("STAT?")
            dut.pump(0.4)
            for l in dut.lines:
                if l.startswith(b"OK STAT"):
                    recovered = True
                    m = re.search(rb"hb=(\d+)", l)
                    hb_seen = int(m.group(1)) if m else None
                    break
            if recovered:
                break
    check("12 watchdog recovery: HANG watchdog -> reset -> banner -> alive",
          got_ok and ln is not None and recovered,
          f"echo={got_ok} banner={ln is not None} alive={recovered} hb={hb_seen}")


if __name__ == "__main__":
    sys.exit(main())
