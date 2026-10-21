#include "frame.h"

#include "crc16.h"

uint8_t frame_payload_len(uint8_t type)
{
    switch (type) {
    case FRAME_TYPE_DATA:
        return 12u;
    case FRAME_TYPE_ALARM:
        return 4u;
    case FRAME_TYPE_HEARTBEAT:
        return 3u;
    case FRAME_TYPE_ACK_REQ:
    case FRAME_TYPE_ACK:
        return 2u;
    default:
        return 0u;
    }
}

uint16_t frame_serialize(uint8_t *out, const frame_t *f, uint16_t seq)
{
    uint16_t idx = 0u;

    out[idx++] = FRAME_HDR0;
    out[idx++] = FRAME_HDR1;
    out[idx++] = (uint8_t)(seq >> 8);
    out[idx++] = (uint8_t)(seq & 0xFFu);
    out[idx++] = f->type;
    for (uint8_t i = 0u; i < f->len; i++) {
        out[idx++] = f->payload[i];
    }

    uint16_t crc = crc16_ccitt(out, idx);
    out[idx++] = (uint8_t)(crc >> 8);
    out[idx++] = (uint8_t)(crc & 0xFFu);
    return idx;
}
