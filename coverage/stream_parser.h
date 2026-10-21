/*
 * stream_parser.h - host-side wire parser used by the coverage test drivers.
 *
 * Mirrors the semantics of the Python harness parser (firmware/manual_check.py):
 * hunt for the 0xAA 0x55 sync header in an arbitrary byte stream, read
 * seq/type/payload, reject bad CRCs, resynchronise after garbage, and check
 * sequence-number continuity across accepted frames.
 *
 * This is test tooling, not firmware; it is excluded from coverage scope.
 */
#ifndef STREAM_PARSER_H
#define STREAM_PARSER_H

#include <stddef.h>
#include <stdint.h>

#include "frame.h"

typedef struct parser parser_t;

typedef void (*parser_frame_cb)(const frame_t *f, uint16_t seq, void *ctx);

struct parser {
    /* Sliding window over the stream (max wire frame is 19 bytes). */
    uint8_t win[32];
    uint8_t win_len;

    parser_frame_cb on_frame;
    void *ctx;

    /* Statistics. */
    unsigned frames_ok;   /* accepted, CRC-valid frames */
    unsigned crc_errors;  /* frames dropped on CRC mismatch */
    unsigned resyncs;     /* bytes skipped while hunting for a header */
    unsigned seq_gaps;    /* accepted frames whose seq broke continuity */

    /* Sequence continuity tracking. */
    int have_seq;
    uint16_t last_seq;
};

void parser_init(parser_t *p, parser_frame_cb cb, void *ctx);
void parser_feed(parser_t *p, const uint8_t *data, size_t n);

#endif /* STREAM_PARSER_H */
