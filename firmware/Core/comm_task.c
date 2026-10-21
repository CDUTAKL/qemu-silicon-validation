/*
 * comm_task.c - CommTask (priority 2).
 *
 * Sole owner of the wire: takes frame_t items from q_tx, stamps the shared
 * 16-bit sequence number, appends the CRC and transmits the exact bytes.
 * Because only this task assigns sequence numbers, the host always sees one
 * strictly increasing counter across data/alarm/heartbeat/ACK frames.
 */
#include "app.h"

#include "frame.h"
#include "minilib.h"
#include "uart.h"

void comm_task_fn(void *arg)
{
    (void)arg;
    frame_t f;
    uint8_t wire[FRAME_WIRE_SIZE_DATA];

    for (;;) {
        if (xQueueReceive(q_tx, &f, portMAX_DELAY) == pdPASS) {
            uint16_t n = frame_serialize(wire, &f, g_seq);
            g_seq++;
            g_run_count++;
            uart_write(wire, n);
        }
    }
}
