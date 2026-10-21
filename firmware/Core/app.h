/*
 * app.h - shared application state between the four DUT tasks.
 */
#ifndef APP_H
#define APP_H

#include <stdint.h>

#include "FreeRTOS.h"
#include "queue.h"
#include "task.h"
#include "timers.h"

/* TX queue: every outbound frame (data/alarm/heartbeat/ACK) passes through
 * here so the sequence number is stamped by CommTask alone and frame bytes
 * on the wire can never interleave. Length is part of the boot banner. */
#define TX_QUEUE_LEN 64u

extern QueueHandle_t q_tx;

/* Watchdog heartbeat counter: incremented by WatchdogTask every 500 ms.
 * A 1 s software timer resets the chip if it ever stops increasing. */
extern volatile uint32_t hb_count;

/* Total frames transmitted by CommTask (all types). */
extern volatile uint32_t g_run_count;

/* Next sequence number to stamp (also reported by STAT?). Only CommTask
 * writes it; other tasks read. Reset to 0 on every boot via bss clear. */
extern volatile uint16_t g_seq;

/* Fault-injection state, written by DiagTask, consumed by SensorTask. */
extern volatile uint8_t g_stream_paused; /* FAULT DISCONN/CONN */
extern volatile uint8_t g_ov_pending;    /* one-shot voltage override */
extern volatile uint8_t g_oc_pending;    /* one-shot current override */
extern volatile float g_ov_value;
extern volatile float g_oc_value;

extern TaskHandle_t th_sensor;
extern TaskHandle_t th_comm;
extern TaskHandle_t th_diag;
extern TaskHandle_t th_watchdog;

void sensor_task_fn(void *arg);
void comm_task_fn(void *arg);
void diag_task_fn(void *arg);
void watchdog_task_fn(void *arg);

void app_tasks_create(void);

/* Number of the four application tasks that are not suspended. */
uint8_t app_running_task_count(void);

/* Heartbeat task bitmap: bit0 sensor, bit1 comm, bit2 diag, bit3 watchdog. */
uint8_t app_task_bitmap(void);

/* Maximum stack usage in bytes across the four application tasks. */
uint16_t app_stack_hwm_bytes(void);

#endif /* APP_H */
