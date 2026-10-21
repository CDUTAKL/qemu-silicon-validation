/*
 * crc16.c - CRC16-CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection,
 * no final XOR. Covers the whole frame except the CRC field itself.
 */
#include "crc16.h"

uint16_t crc16_ccitt(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFFu;

    while (len--) {
        crc ^= (uint16_t)(*data++) << 8;
        for (int i = 0; i < 8; i++) {
            if ((crc & 0x8000u) != 0u) {
                crc = (uint16_t)((crc << 1) ^ 0x1021u);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
    }
    return crc;
}
