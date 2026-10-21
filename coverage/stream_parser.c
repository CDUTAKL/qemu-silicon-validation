/*
 * stream_parser.c - see stream_parser.h.
 */
#include "stream_parser.h"

#include <string.h>

#include "crc16.h"

void parser_init(parser_t *p, parser_frame_cb cb, void *ctx)
{
    memset(p, 0, sizeof(*p));
    p->on_frame = cb;
    p->ctx = ctx;
}

/* Drop n leading bytes from the scan window (resynchronisation). */
static void win_drop(parser_t *p, uint8_t n)
{
    if (n >= p->win_len) {
        p->win_len = 0u;
        return;
    }
    memmove(p->win, p->win + n, (size_t)(p->win_len - n));
    p->win_len = (uint8_t)(p->win_len - n);
    p->resyncs += n;
}

/* Try to consume one complete frame from the front of the window.
 * Returns 1 when progress was made (frame consumed or bytes dropped). */
static int win_step(parser_t *p)
{
    if (p->win_len < 2u) {
        return 0;
    }
    if (p->win[0] != FRAME_HDR0) {
        win_drop(p, 1u);
        return 1;
    }
    if (p->win[1] != FRAME_HDR1) {
        win_drop(p, 1u);
        return 1;
    }
    if (p->win_len < 5u) {
        return 0; /* wait for seq + type */
    }
    uint8_t type = p->win[4];
    uint8_t plen = frame_payload_len(type);
    if (plen == 0u) {
        win_drop(p, 1u); /* unknown type: keep hunting from the next byte */
        return 1;
    }
    uint8_t total = (uint8_t)(5u + plen + 2u);
    if (p->win_len < total) {
        return 0; /* wait for payload + crc */
    }

    uint16_t seq = (uint16_t)(((uint16_t)p->win[2] << 8) | p->win[3]);
    uint16_t crc_got =
        (uint16_t)(((uint16_t)p->win[5u + plen] << 8) | p->win[5u + plen + 1u]);
    uint16_t crc_want = crc16_ccitt(p->win, (size_t)(5u + plen));

    if (crc_got != crc_want) {
        p->crc_errors++;
        win_drop(p, 1u); /* resync: the real header may start inside */
        return 1;
    }

    if (p->have_seq != 0 &&
        seq != (uint16_t)(p->last_seq + 1u)) {
        p->seq_gaps++;
    }
    p->last_seq = seq;
    p->have_seq = 1;
    p->frames_ok++;

    if (p->on_frame != NULL) {
        frame_t f;

        f.type = type;
        f.len = plen;
        memcpy(f.payload, p->win + 5u, plen);
        p->on_frame(&f, seq, p->ctx);
    }

    /* Consume without counting as resync. */
    if (total >= p->win_len) {
        p->win_len = 0u;
    } else {
        memmove(p->win, p->win + total, (size_t)(p->win_len - total));
        p->win_len = (uint8_t)(p->win_len - total);
    }
    return 1;
}

void parser_feed(parser_t *p, const uint8_t *data, size_t n)
{
    for (size_t i = 0u; i < n; i++) {
        if (p->win_len < (uint8_t)sizeof(p->win)) {
            p->win[p->win_len++] = data[i];
        } else {
            /* Window full of unresolvable bytes: drop the oldest. */
            win_drop(p, 1u);
            p->win[p->win_len++] = data[i];
        }
        while (win_step(p) != 0) {
        }
    }
}
