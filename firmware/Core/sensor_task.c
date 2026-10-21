/*
 * sensor_task.c - SensorTask (priority 2).
 *
 * Software model of a 3-channel telemetry source, deterministic by design:
 * a fixed-seed LCG plus a 256-entry Q15 sine lookup table. Every run of the
 * firmware produces the exact same sample sequence.
 *
 *   voltage = 12.0 + 2.0*sin(3*phase)  + (noise-0.5)*0.4
 *   current =  1.5 + 0.5*sin(2*phase)  + (noise-0.5)*0.1
 *   temp    = 25.0 + 5.0*sin(phase)    + (noise-0.5)*1.0
 *
 * phase advances once per 100 ms sample (index into the 256-step table).
 * Fault injection (DiagTask) can force the next sample's voltage/current
 * and pause/resume the upstream of data frames. A pending override is
 * consumed only by a sample that is actually transmitted; the alarm frame
 * and the forced data frame are queued inside the same critical section,
 * so they always reach the wire back to back (alarm first).
 */
#include "app.h"

#include "frame.h"
#include "minilib.h"

#define LCG_SEED 0x12345678u

/* sin(2*pi*k/256) in Q15, generated once - fixed data, no libm needed. */
static const int16_t k_sin_lut[256] = {
    0, 804, 1608, 2410, 3212, 4011, 4808, 5602,
    6393, 7179, 7962, 8739, 9512, 10278, 11039, 11793,
    12539, 13279, 14010, 14732, 15446, 16151, 16846, 17530,
    18204, 18868, 19519, 20159, 20787, 21403, 22005, 22594,
    23170, 23731, 24279, 24811, 25329, 25832, 26319, 26790,
    27245, 27683, 28105, 28510, 28898, 29268, 29621, 29956,
    30273, 30571, 30852, 31113, 31356, 31580, 31785, 31971,
    32137, 32285, 32412, 32521, 32609, 32678, 32728, 32757,
    32767, 32757, 32728, 32678, 32609, 32521, 32412, 32285,
    32137, 31971, 31785, 31580, 31356, 31113, 30852, 30571,
    30273, 29956, 29621, 29268, 28898, 28510, 28105, 27683,
    27245, 26790, 26319, 25832, 25329, 24811, 24279, 23731,
    23170, 22594, 22005, 21403, 20787, 20159, 19519, 18868,
    18204, 17530, 16846, 16151, 15446, 14732, 14010, 13279,
    12539, 11793, 11039, 10278, 9512, 8739, 7962, 7179,
    6393, 5602, 4808, 4011, 3212, 2410, 1608, 804,
    0, -804, -1608, -2410, -3212, -4011, -4808, -5602,
    -6393, -7179, -7962, -8739, -9512, -10278, -11039, -11793,
    -12539, -13279, -14010, -14732, -15446, -16151, -16846, -17530,
    -18204, -18868, -19519, -20159, -20787, -21403, -22005, -22594,
    -23170, -23731, -24279, -24811, -25329, -25832, -26319, -26790,
    -27245, -27683, -28105, -28510, -28898, -29268, -29621, -29956,
    -30273, -30571, -30852, -31113, -31356, -31580, -31785, -31971,
    -32137, -32285, -32412, -32521, -32609, -32678, -32728, -32757,
    -32767, -32757, -32728, -32678, -32609, -32521, -32412, -32285,
    -32137, -31971, -31785, -31580, -31356, -31113, -30852, -30571,
    -30273, -29956, -29621, -29268, -28898, -28510, -28105, -27683,
    -27245, -26790, -26319, -25832, -25329, -24811, -24279, -23731,
    -23170, -22594, -22005, -21403, -20787, -20159, -19519, -18868,
    -18204, -17530, -16846, -16151, -15446, -14732, -14010, -13279,
    -12539, -11793, -11039, -10278, -9512, -8739, -7962, -7179,
    -6393, -5602, -4808, -4011, -3212, -2410, -1608, -804,
};

static float sin_q15(uint8_t idx)
{
    return (float)k_sin_lut[idx] / 32767.0f;
}

/* Queue an alarm frame (fault_id, level=2, value=u16 rounded). Caller must
 * hold the critical section when alarm/data adjacency matters. */
static void queue_alarm(uint8_t fault_id, float value)
{
    frame_t f;
    uint16_t v16 = (uint16_t)(value + 0.5f);

    memset(&f, 0, sizeof(f));
    f.type = FRAME_TYPE_ALARM;
    f.len = 4u;
    f.payload[0] = fault_id;
    f.payload[1] = 2u; /* level */
    f.payload[2] = (uint8_t)(v16 >> 8);
    f.payload[3] = (uint8_t)(v16 & 0xFFu);
    (void)xQueueSendToBack(q_tx, &f, 0);
}

static void put_f32be(uint8_t *p, float v)
{
    uint32_t u;

    (void)memcpy(&u, &v, sizeof(u));
    p[0] = (uint8_t)(u >> 24);
    p[1] = (uint8_t)(u >> 16);
    p[2] = (uint8_t)(u >> 8);
    p[3] = (uint8_t)(u);
}

void sensor_task_fn(void *arg)
{
    (void)arg;
    TickType_t last_wake = xTaskGetTickCount();
    uint32_t lcg = LCG_SEED;
    uint8_t phase = 0u;

    for (;;) {
        lcg = lcg * 1103515245u + 12345u;
        float noise = (float)((lcg >> 16) & 0xFFFFu) / 65535.0f;

        float v = 12.0f + 2.0f * sin_q15((uint8_t)(3u * phase)) +
                  (noise - 0.5f) * 0.4f;
        float i = 1.5f + 0.5f * sin_q15((uint8_t)(2u * phase + 64u)) +
                  (noise - 0.5f) * 0.1f;
        float t = 25.0f + 5.0f * sin_q15((uint8_t)(phase + 128u)) +
                  (noise - 0.5f) * 1.0f;

        if (g_stream_paused == 0u) {
            frame_t f;
            uint8_t ov = 0u;
            uint8_t oc = 0u;

            /* One-shot fault overrides apply to the next transmitted
             * sample. Alarm + forced data frame are queued atomically so
             * no heartbeat can slip between them on the wire. */
            taskENTER_CRITICAL();
            if (g_ov_pending != 0u) {
                v = g_ov_value;
                g_ov_pending = 0u;
                ov = 1u;
            }
            if (g_oc_pending != 0u) {
                i = g_oc_value;
                g_oc_pending = 0u;
                oc = 1u;
            }
            if (ov != 0u) {
                queue_alarm(1u, v);
            }
            if (oc != 0u) {
                queue_alarm(2u, i);
            }

            memset(&f, 0, sizeof(f));
            f.type = FRAME_TYPE_DATA;
            f.len = 12u;
            put_f32be(&f.payload[0], v);
            put_f32be(&f.payload[4], i);
            put_f32be(&f.payload[8], t);
            (void)xQueueSend(q_tx, &f, 0);
            taskEXIT_CRITICAL();
        }

        phase++;
        xTaskDelayUntil(&last_wake, pdMS_TO_TICKS(100));
    }
}
