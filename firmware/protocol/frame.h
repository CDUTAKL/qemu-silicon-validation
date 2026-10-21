/*
 * frame.h - DUT reporting frame protocol shared by firmware and test tools.
 *
 * Wire format (all multi-byte fields big-endian):
 *
 *   byte 0      1      2-3        4      5-N        N+1..N+2
 *   | 0xAA | 0x55 | seq_u16(BE) | type | payload  | crc16_ccitt(BE) |
 *
 * CRC16-CCITT-FALSE (init 0xFFFF, poly 0x1021) over bytes 0..N, i.e. the
 * whole frame except the CRC itself.
 *
 * Frame types / payloads:
 *   0x01 DATA   12 B : three big-endian IEEE754 floats (voltage, current, temp)
 *   0x02 ALARM   4 B : fault_id(1B) + level(1B) + value(u16 BE)
 *   0x03 HEARTBEAT 3B: task bitmap(1B, bit0..3 = sensor/comm/diag/watchdog,
 *                      1 = running) + stack high-water mark in bytes (u16 BE)
 *   0x04 ACKREQ  2 B : request code (u16 BE), host -> DUT (0x0001 = ping)
 *   0x05 ACK     2 B : status (u16 BE, 0 = ok), DUT -> host
 *
 * The 16-bit sequence number is shared by every frame the DUT transmits,
 * increments by one per frame, starts at 0 after reset.
 */
#ifndef FRAME_H
#define FRAME_H

#include <stdint.h>

#define FRAME_HDR0 0xAAu
#define FRAME_HDR1 0x55u

#define FRAME_TYPE_DATA 0x01u
#define FRAME_TYPE_ALARM 0x02u
#define FRAME_TYPE_HEARTBEAT 0x03u
#define FRAME_TYPE_ACK_REQ 0x04u
#define FRAME_TYPE_ACK 0x05u

#define FRAME_MAX_PAYLOAD 12u

#define FRAME_ACKREQ_PING 0x0001u

/* Serialized sizes including header(5) + payload + crc(2). */
#define FRAME_WIRE_SIZE_DATA (5u + 12u + 2u)      /* 19 */
#define FRAME_WIRE_SIZE_ALARM (5u + 4u + 2u)      /* 11 */
#define FRAME_WIRE_SIZE_HEARTBEAT (5u + 3u + 2u)  /* 10 */
#define FRAME_WIRE_SIZE_ACK (5u + 2u + 2u)        /* 9 */

typedef struct {
    uint8_t type;
    uint8_t len; /* payload length, <= FRAME_MAX_PAYLOAD */
    uint8_t payload[FRAME_MAX_PAYLOAD];
} frame_t;

/* Payload length for a frame type, or 0 for an unknown type. */
uint8_t frame_payload_len(uint8_t type);

/* Serialize a frame into out[] (>= FRAME_WIRE_SIZE_DATA bytes) with the
 * given sequence number and trailing CRC. Returns total wire length. */
uint16_t frame_serialize(uint8_t *out, const frame_t *f, uint16_t seq);

#endif /* FRAME_H */
