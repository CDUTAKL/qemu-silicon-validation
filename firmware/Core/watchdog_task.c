/*
 * watchdog_task.c - WatchdogTask (priority 1).
 *
 * Every 500 ms: bump the global hb_count heartbeat. There is no emulated
 * IWDG pressure in QEMU, so hb_count *is* the watchdog signal: the 1 s
 * software timer created in app.c calls NVIC_SystemReset() the moment
 * hb_count stops increasing (e.g. after "FAULT HANG watchdog").
 */
#include "app.h"

void watchdog_task_fn(void *arg)
{
    (void)arg;

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(500));
        hb_count++;
    }
}
