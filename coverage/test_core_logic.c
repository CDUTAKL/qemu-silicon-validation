/*
 * test_core_logic.c - unit tests for firmware/protocol (crc16, frame).
 *
 * Pure C, no FreeRTOS, no pytest: a main() that runs assertions and prints
 * PASS/FAIL statistics. Exit code 0 = all passed, 1 = failures.
 *
 * Covered here:
 *   - CRC16-CCITT-FALSE standard vectors ("123456789" -> 0x29B1, etc.)
 *   - frame_payload_len for every frame type + unknown types
 *   - frame_serialize wire layout for every frame type, incl. seq wrap
 *   - serialize -> host-parse round-trip over a random frame stream
 *   - bad CRC rejection, misaligned/garbage resynchronisation, seq gaps
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* macOS secure-headers macroize memcpy/memmove/... after <string.h>;
 * drop the macros so the firmware's freestanding declarations in
 * minilib.h (and direct calls below) bind to the real symbols. */
#undef memcpy
#undef memmove
#undef memset
#undef memcmp
#undef strcpy
#undef strlen
#undef strcmp
#undef strncmp

#include "crc16.h"
#include "frame.h"
#include "minilib.h"
#include "stream_parser.h"

static unsigned g_checks;
static unsigned g_failures;

#define CHECK(cond)                                                           \
    do {                                                                      \
        g_checks++;                                                           \
        if (!(cond)) {                                                        \
            g_failures++;                                                     \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);           \
        }                                                                     \
    } while (0)

/* ---------------- CRC16 ---------------- */

static void test_crc16_vectors(void)
{
    static const char s_digits[] = "123456789";
    static const uint8_t s_one_zero[] = {0x00u};
    static const uint8_t s_three[] = {0xFFu, 0x00u, 0x55u};

    /* CRC-16/CCITT-FALSE check values (init 0xFFFF, poly 0x1021). */
    CHECK(crc16_ccitt((const uint8_t *)s_digits, 9u) == 0x29B1u);
    CHECK(crc16_ccitt(NULL, 0u) == 0xFFFFu);
    CHECK(crc16_ccitt(s_one_zero, sizeof(s_one_zero)) == 0xE1F0u);
    CHECK(crc16_ccitt(s_three, sizeof(s_three)) == 0x09AFu);
}

static void test_crc16_properties(void)
{
    /* Single-bit flip anywhere in the message must change the CRC. */
    uint8_t msg[16];

    for (size_t i = 0u; i < sizeof(msg); i++) {
        msg[i] = (uint8_t)(i * 37u + 11u);
    }
    uint16_t base = crc16_ccitt(msg, sizeof(msg));
    for (size_t byte = 0u; byte < sizeof(msg); byte++) {
        msg[byte] ^= 0x01u;
        CHECK(crc16_ccitt(msg, sizeof(msg)) != base);
        msg[byte] ^= 0x01u;
    }
}

/* ---------------- frame_payload_len ---------------- */

static void test_payload_len(void)
{
    CHECK(frame_payload_len(FRAME_TYPE_DATA) == 12u);
    CHECK(frame_payload_len(FRAME_TYPE_ALARM) == 4u);
    CHECK(frame_payload_len(FRAME_TYPE_HEARTBEAT) == 3u);
    CHECK(frame_payload_len(FRAME_TYPE_ACK_REQ) == 2u);
    CHECK(frame_payload_len(FRAME_TYPE_ACK) == 2u);

    /* Unknown types must be rejected. */
    CHECK(frame_payload_len(0x00u) == 0u);
    CHECK(frame_payload_len(0x06u) == 0u);
    CHECK(frame_payload_len(0xFFu) == 0u);
}

/* ---------------- frame_serialize ---------------- */

/* Verify one serialized frame byte-by-byte against the wire format. */
static void check_wire(const uint8_t *wire, uint16_t ret, const frame_t *f,
                       uint16_t seq)
{
    uint8_t plen = frame_payload_len(f->type);

    CHECK(ret == (uint16_t)(5u + plen + 2u));
    CHECK(wire[0] == FRAME_HDR0);
    CHECK(wire[1] == FRAME_HDR1);
    CHECK(wire[2] == (uint8_t)(seq >> 8));
    CHECK(wire[3] == (uint8_t)(seq & 0xFFu));
    CHECK(wire[4] == f->type);
    CHECK(memcmp(wire + 5u, f->payload, plen) == 0);
    uint16_t crc = crc16_ccitt(wire, (size_t)(5u + plen));
    CHECK(wire[5u + plen] == (uint8_t)(crc >> 8));
    CHECK(wire[5u + plen + 1u] == (uint8_t)(crc & 0xFFu));
}

static void test_serialize_all_types(void)
{
    static const uint8_t s_types[] = {
        FRAME_TYPE_DATA, FRAME_TYPE_ALARM, FRAME_TYPE_HEARTBEAT,
        FRAME_TYPE_ACK_REQ, FRAME_TYPE_ACK,
    };
    uint8_t wire[FRAME_WIRE_SIZE_DATA];

    for (size_t t = 0u; t < sizeof(s_types); t++) {
        frame_t f;

        memset(&f, 0, sizeof(f));
        f.type = s_types[t];
        f.len = frame_payload_len(f.type);
        for (uint8_t i = 0u; i < f.len; i++) {
            f.payload[i] = (uint8_t)(0xA0u + i);
        }
        uint16_t ret = frame_serialize(wire, &f, 0x1234u);
        check_wire(wire, ret, &f, 0x1234u);
    }
}

static void test_serialize_seq_endianness(void)
{
    frame_t f;
    uint8_t wire[FRAME_WIRE_SIZE_DATA];

    memset(&f, 0, sizeof(f));
    f.type = FRAME_TYPE_ACK;
    f.len = 2u;

    /* Sequence number must be big-endian, including at the wrap point. */
    (void)frame_serialize(wire, &f, 0x0000u);
    CHECK(wire[2] == 0x00u && wire[3] == 0x00u);
    (void)frame_serialize(wire, &f, 0xFFFFu);
    CHECK(wire[2] == 0xFFu && wire[3] == 0xFFu);
    (void)frame_serialize(wire, &f, 0x00FFu);
    CHECK(wire[2] == 0x00u && wire[3] == 0xFFu);
}

/* ---------------- serialize -> parse round-trip ---------------- */

typedef struct {
    frame_t frames[64];
    uint16_t seqs[64];
    unsigned count;
} capture_t;

static void capture_cb(const frame_t *f, uint16_t seq, void *ctx)
{
    capture_t *cap = (capture_t *)ctx;

    if (cap->count < 64u) {
        cap->frames[cap->count] = *f;
        cap->seqs[cap->count] = seq;
        cap->count++;
    }
}

/* Build a wire stream of one frame per type with strictly increasing seq. */
static size_t build_stream(uint8_t *stream, frame_t *frames, uint16_t seq0,
                           unsigned n)
{
    static const uint8_t s_types[] = {
        FRAME_TYPE_DATA, FRAME_TYPE_ALARM, FRAME_TYPE_HEARTBEAT,
        FRAME_TYPE_ACK_REQ, FRAME_TYPE_ACK,
    };
    size_t off = 0u;

    for (unsigned i = 0u; i < n; i++) {
        memset(&frames[i], 0, sizeof(frames[i]));
        frames[i].type = s_types[i % sizeof(s_types)];
        frames[i].len = frame_payload_len(frames[i].type);
        for (uint8_t b = 0u; b < frames[i].len; b++) {
            frames[i].payload[b] = (uint8_t)(i * 13u + b * 7u + 1u);
        }
        off += frame_serialize(stream + off, &frames[i],
                               (uint16_t)(seq0 + i));
    }
    return off;
}

static void test_roundtrip_stream(void)
{
    static uint8_t stream[64u * FRAME_WIRE_SIZE_DATA];
    static frame_t frames[64];
    capture_t cap;
    parser_t p;

    parser_init(&p, capture_cb, &cap);
    memset(&cap, 0, sizeof(cap));

    size_t n = build_stream(stream, frames, 0u, 64u);
    /* Feed byte-by-byte to stress the incremental path. */
    for (size_t i = 0u; i < n; i++) {
        parser_feed(&p, stream + i, 1u);
    }

    CHECK(p.frames_ok == 64u);
    CHECK(p.crc_errors == 0u);
    CHECK(p.resyncs == 0u);
    CHECK(p.seq_gaps == 0u);
    CHECK(cap.count == 64u);
    for (unsigned i = 0u; i < 64u && i < cap.count; i++) {
        CHECK(cap.seqs[i] == (uint16_t)i);
        CHECK(cap.frames[i].type == frames[i].type);
        CHECK(cap.frames[i].len == frames[i].len);
        CHECK(memcmp(cap.frames[i].payload, frames[i].payload,
                     frames[i].len) == 0);
    }
}

static void test_bad_crc_rejected(void)
{
    static uint8_t stream[8u * FRAME_WIRE_SIZE_DATA];
    static frame_t frames[8];
    capture_t cap;
    parser_t p;

    size_t n = build_stream(stream, frames, 100u, 4u);

    /* Corrupt one payload byte in frame #1: CRC no longer matches. */
    stream[FRAME_WIRE_SIZE_DATA + 5u] ^= 0x40u;

    parser_init(&p, capture_cb, &cap);
    memset(&cap, 0, sizeof(cap));
    parser_feed(&p, stream, n);

    CHECK(p.frames_ok == 3u);
    CHECK(p.crc_errors == 1u);
    /* The corrupted frame's bytes get scanned until the next header. */
    CHECK(p.resyncs > 0u);
}

static void test_garbage_resync(void)
{
    static uint8_t stream[4u * FRAME_WIRE_SIZE_DATA];
    static frame_t frames[4];
    capture_t cap;
    parser_t p;

    size_t n = build_stream(stream, frames, 0u, 4u);

    /* Garbage ahead of the stream, including a lone 0xAA and a false
     * 0xAA 0x00 start, plus garbage between frames. */
    static const uint8_t junk1[] = {0x11u, 0xAAu, 0xAAu, 0x00u, 0x55u};
    static const uint8_t junk2[] = {0xAAu, 0x55u, 0x00u}; /* seq then EOF of
        header validity is impossible; this forms a header with unknown type
        after the next byte, forcing resync */
    uint8_t buf[sizeof(junk1) + sizeof(junk2) + sizeof(stream)];
    size_t off = 0u;

    memcpy(buf + off, junk1, sizeof(junk1));
    off += sizeof(junk1);
    memcpy(buf + off, stream, FRAME_WIRE_SIZE_DATA);
    off += FRAME_WIRE_SIZE_DATA;
    memcpy(buf + off, junk2, sizeof(junk2));
    off += sizeof(junk2);
    memcpy(buf + off, stream + FRAME_WIRE_SIZE_DATA,
           n - FRAME_WIRE_SIZE_DATA);
    off += n - FRAME_WIRE_SIZE_DATA;

    parser_init(&p, capture_cb, &cap);
    memset(&cap, 0, sizeof(cap));
    parser_feed(&p, buf, off);

    /* All four real frames must still be recovered. */
    CHECK(p.frames_ok == 4u);
    CHECK(p.resyncs > 0u);
    CHECK(cap.count == 4u);
    for (unsigned i = 0u; i < 4u && i < cap.count; i++) {
        CHECK(cap.seqs[i] == (uint16_t)i);
    }
}

static void test_unknown_type_resync(void)
{
    capture_t cap;
    parser_t p;
    frame_t f;
    uint8_t wire[FRAME_WIRE_SIZE_DATA];

    /* A "frame" with type 0x77 (unknown) then a valid ACK frame. */
    static const uint8_t bogus[] = {0xAAu, 0x55u, 0x00u, 0x07u,
                                    0x77u, 0x01u, 0x02u, 0x03u};

    parser_init(&p, capture_cb, &cap);
    memset(&cap, 0, sizeof(cap));
    parser_feed(&p, bogus, sizeof(bogus));
    CHECK(p.frames_ok == 0u);
    CHECK(p.resyncs > 0u);

    memset(&f, 0, sizeof(f));
    f.type = FRAME_TYPE_ACK;
    f.len = 2u;
    uint16_t len = frame_serialize(wire, &f, 7u);
    parser_feed(&p, wire, len);
    CHECK(p.frames_ok == 1u);
    CHECK(cap.count == 1u);
    if (cap.count == 1u) {
        CHECK(cap.frames[0].type == FRAME_TYPE_ACK);
    }
}

static void test_seq_gap_detected(void)
{
    static uint8_t stream[4u * FRAME_WIRE_SIZE_DATA];
    static frame_t frames[4];
    capture_t cap;
    parser_t p;
    size_t off = 0u;

    /* seq jumps 10 -> 12: one gap must be flagged. */
    memset(&frames[0], 0, sizeof(frames[0]));
    frames[0].type = FRAME_TYPE_HEARTBEAT;
    frames[0].len = 3u;
    off += frame_serialize(stream + off, &frames[0], 10u);
    off += frame_serialize(stream + off, &frames[0], 12u);

    parser_init(&p, capture_cb, &cap);
    memset(&cap, 0, sizeof(cap));
    parser_feed(&p, stream, off);

    CHECK(p.frames_ok == 2u);
    CHECK(p.seq_gaps == 1u);
}

/* ---------------- minilib (freestanding libc used by the firmware) ---------------- */

/* Calls go through volatile function pointers so gcc cannot substitute its
 * own builtins — the point is to execute the firmware's implementations. */
static void test_minilib_mem(void)
{
    void *(*volatile v_memcpy)(void *, const void *, size_t) = memcpy;
    void *(*volatile v_memmove)(void *, const void *, size_t) = memmove;
    void *(*volatile v_memset)(void *, int, size_t) = memset;
    int (*volatile v_memcmp)(const void *, const void *, size_t) = memcmp;

    uint8_t dst[16];
    static const uint8_t src[16] = {0, 1, 2, 3, 4, 5, 6, 7,
                                    8, 9, 10, 11, 12, 13, 14, 15};

    CHECK(v_memcpy(dst, src, 16u) == dst);
    CHECK(v_memcmp(dst, src, 16u) == 0);
    CHECK(v_memcmp(src, dst, 0u) == 0);

    /* memmove: disjoint, dst==src, n==0, overlap forward, overlap backward. */
    uint8_t buf[16] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15};

    CHECK(v_memmove(buf, buf, 16u) == buf);
    CHECK(v_memcmp(buf, src, 16u) == 0);
    CHECK(v_memmove(buf, src, 0u) == buf);
    CHECK(v_memcmp(buf, src, 16u) == 0);

    v_memmove(buf + 2, buf, 8u); /* overlapping, dst > src */
    static const uint8_t ov1[16] = {0, 1, 0, 1, 2, 3, 4, 5,
                                    6, 7, 10, 11, 12, 13, 14, 15};
    CHECK(v_memcmp(buf, ov1, 16u) == 0);

    v_memmove(buf, buf + 2, 8u); /* overlapping, dst < src */
    static const uint8_t ov2[16] = {0, 1, 2, 3, 4, 5, 6, 7,
                                    6, 7, 10, 11, 12, 13, 14, 15};
    CHECK(v_memcmp(buf, ov2, 16u) == 0);

    CHECK(v_memset(buf, 0xA5, 16u) == buf);
    CHECK(buf[0] == 0xA5u && buf[15] == 0xA5u);

    static const uint8_t a[4] = {1, 2, 3, 4};
    static const uint8_t b[4] = {1, 2, 3, 5};

    CHECK(v_memcmp(a, b, 4u) < 0);
    CHECK(v_memcmp(b, a, 4u) > 0);
    CHECK(v_memcmp(a, b, 3u) == 0);
}

static void test_minilib_str(void)
{
    size_t (*volatile v_strlen)(const char *) = strlen;
    int (*volatile v_strcmp)(const char *, const char *) = strcmp;
    int (*volatile v_strncmp)(const char *, const char *, size_t) = strncmp;
    char *(*volatile v_strcpy)(char *, const char *) = strcpy;
    char buf[16];

    CHECK(v_strlen("") == 0u);
    CHECK(v_strlen("abc") == 3u);

    CHECK(v_strcmp("abc", "abc") == 0);
    CHECK(v_strcmp("abc", "abd") < 0);
    CHECK(v_strcmp("abd", "abc") > 0);
    CHECK(v_strcmp("abc", "abcd") < 0);

    CHECK(v_strncmp("abc", "abd", 2u) == 0);
    CHECK(v_strncmp("abc", "abd", 3u) < 0);
    CHECK(v_strncmp("abc", "abc", 0u) == 0);
    CHECK(v_strncmp("abc", "abcd", 4u) < 0);

    CHECK(v_strcpy(buf, "hello") == buf);
    CHECK(v_strcmp(buf, "hello") == 0);
}

static void test_minilib_aeabi(void)
{
    /* ARM EABI aliases: dead on the host but part of the bare-metal image. */
    void *(*volatile v_aeabi_memcpy)(void *, const void *, size_t) =
        __aeabi_memcpy;
    void *(*volatile v_aeabi_memmove)(void *, const void *, size_t) =
        __aeabi_memmove;
    void *(*volatile v_aeabi_memset)(void *, int, size_t) = __aeabi_memset;
    void (*volatile v_aeabi_memclr)(void *, size_t) = __aeabi_memclr;
    void (*volatile v_aeabi_memclr4)(void *, size_t) = __aeabi_memclr4;
    void (*volatile v_aeabi_memclr8)(void *, size_t) = __aeabi_memclr8;
    void (*volatile v_aeabi_memcpy4)(void *, const void *, size_t) =
        __aeabi_memcpy4;
    void (*volatile v_aeabi_memcpy8)(void *, const void *, size_t) =
        __aeabi_memcpy8;

    uint8_t dst[8];
    static const uint8_t src[8] = {1, 2, 3, 4, 5, 6, 7, 8};

    CHECK(v_aeabi_memcpy(dst, src, 8u) == dst);
    CHECK(memcmp(dst, src, 8u) == 0);
    v_aeabi_memcpy4(dst, src, 4u);
    v_aeabi_memcpy8(dst, src, 8u);
    CHECK(v_aeabi_memmove(dst, src, 8u) == dst);
    CHECK(v_aeabi_memset(dst, 0xFF, 8u) == dst);
    CHECK(dst[7] == 0xFFu);
    v_aeabi_memclr(dst, 8u);
    CHECK(dst[0] == 0u && dst[7] == 0u);
    v_aeabi_memclr4(dst, 4u);
    CHECK(dst[3] == 0u);
    v_aeabi_memclr8(dst, 8u);
    CHECK(dst[7] == 0u);
}

static void test_minilib_u32_to_dec(void)
{
    char out[16];

    CHECK(u32_to_dec(0u, out) == 1u && memcmp(out, "0", 1u) == 0);
    CHECK(u32_to_dec(7u, out) == 1u && memcmp(out, "7", 1u) == 0);
    CHECK(u32_to_dec(123456u, out) == 6u && memcmp(out, "123456", 6u) == 0);
    CHECK(u32_to_dec(4294967295u, out) == 10u &&
          memcmp(out, "4294967295", 10u) == 0);
}

static void test_minilib_parse_f32(void)
{
    float v;

    CHECK(parse_f32("15.25", &v) == 0 && v > 15.24f && v < 15.26f);
    CHECK(parse_f32("0", &v) == 0 && v == 0.0f);
    CHECK(parse_f32("15", &v) == 0 && v == 15.0f);
    CHECK(parse_f32("", &v) != 0);
    CHECK(parse_f32("abc", &v) != 0);
    CHECK(parse_f32("1.2.3", &v) != 0);
    CHECK(parse_f32(".", &v) != 0);
    CHECK(parse_f32("12a", &v) != 0);
}

/* ---------------- main ---------------- */

int main(void)
{
    test_crc16_vectors();
    test_crc16_properties();
    test_payload_len();
    test_serialize_all_types();
    test_serialize_seq_endianness();
    test_roundtrip_stream();
    test_bad_crc_rejected();
    test_garbage_resync();
    test_unknown_type_resync();
    test_seq_gap_detected();
    test_minilib_mem();
    test_minilib_str();
    test_minilib_aeabi();
    test_minilib_u32_to_dec();
    test_minilib_parse_f32();

    printf("core-logic unit tests: %u checks, %u failures -> %s\n", g_checks,
           g_failures, g_failures == 0u ? "PASS" : "FAIL");
    return g_failures == 0u ? 0 : 1;
}
