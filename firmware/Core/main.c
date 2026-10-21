/*
 * main.c - bring up UART + tasks, print the boot banner, run FreeRTOS.
 *
 * Boot banner format (asserted by the test harness):
 *   BOOT v1.0.0 tasks:<n> q:<len>
 */
#include "app.h"

#include "minilib.h"
#include "uart.h"

int main(void)
{
    uart_init();
    app_tasks_create();

    char banner[40];
    size_t n = 0u;

    (void)strcpy(banner, "BOOT v1.0.0 tasks:");
    n = strlen(banner);
    n += u32_to_dec(4u, &banner[n]); /* sensor, comm, diag, watchdog */
    (void)strcpy(&banner[n], " q:");
    n = strlen(banner);
    n += u32_to_dec(TX_QUEUE_LEN, &banner[n]);
    banner[n++] = '\n';
    banner[n] = '\0';
    uart_write_str(banner);

    vTaskStartScheduler();

    for (;;) {
    }
}
