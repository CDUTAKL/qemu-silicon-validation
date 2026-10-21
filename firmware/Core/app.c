/*
 * app.c - task creation + helpers shared across tasks.
 */
#include "app.h"

#include "board.h"
#include "frame.h"
#include "minilib.h"
#include "uart.h"

/* Task stack sizes in words. Tuned for the 8 KiB SRAM budget; the measured
 * high-water marks are reported in the heartbeat frame and by STAT?. */
#define SENSOR_STACK_WORDS 128
#define COMM_STACK_WORDS 160
#define DIAG_STACK_WORDS 192
#define WATCHDOG_STACK_WORDS 96

#define PRIO_WATCHDOG 1
#define PRIO_SENSOR 2
#define PRIO_COMM 2
#define PRIO_DIAG 3

QueueHandle_t q_tx;

volatile uint32_t hb_count;
volatile uint32_t g_run_count;
volatile uint16_t g_seq;
volatile uint8_t g_stream_paused;
volatile uint8_t g_ov_pending;
volatile uint8_t g_oc_pending;
volatile float g_ov_value;
volatile float g_oc_value;

TaskHandle_t th_sensor;
TaskHandle_t th_comm;
TaskHandle_t th_diag;
TaskHandle_t th_watchdog;

/* Heartbeat/watchdog check software timer. */
static TimerHandle_t s_hb_check_timer;
static TimerHandle_t s_hb_frame_timer;
static volatile uint32_t s_hb_last = 0xFFFFFFFFu;

static void hb_frame_cb(TimerHandle_t timer)
{
    (void)timer;
    frame_t f;

    memset(&f, 0, sizeof(f));
    f.type = FRAME_TYPE_HEARTBEAT;
    f.len = 3u;
    f.payload[0] = app_task_bitmap();
    uint16_t hwm = app_stack_hwm_bytes();
    f.payload[1] = (uint8_t)(hwm >> 8);
    f.payload[2] = (uint8_t)(hwm & 0xFFu);
    (void)xQueueSendToBack(q_tx, &f, 0);
}

static void hb_check_cb(TimerHandle_t timer)
{
    (void)timer;
    uint32_t now = hb_count;
    if (now == s_hb_last) {
        /* Heartbeat stopped: somebody hung the watchdog task. Reset so the
         * host recovery test sees a clean reboot (BOOT banner reappears). */
        system_reset();
    }
    s_hb_last = now;
}

void app_tasks_create(void)
{
    q_tx = xQueueCreate(TX_QUEUE_LEN, sizeof(frame_t));
    configASSERT(q_tx != NULL);

    BaseType_t ok;
    ok = xTaskCreate(sensor_task_fn, "sensor", SENSOR_STACK_WORDS, NULL,
                     PRIO_SENSOR, &th_sensor);
    configASSERT(ok == pdPASS);
    ok = xTaskCreate(comm_task_fn, "comm", COMM_STACK_WORDS, NULL, PRIO_COMM,
                     &th_comm);
    configASSERT(ok == pdPASS);
    ok = xTaskCreate(diag_task_fn, "diag", DIAG_STACK_WORDS, NULL, PRIO_DIAG,
                     &th_diag);
    configASSERT(ok == pdPASS);
    ok = xTaskCreate(watchdog_task_fn, "watchdog", WATCHDOG_STACK_WORDS, NULL,
                     PRIO_WATCHDOG, &th_watchdog);
    configASSERT(ok == pdPASS);

    s_hb_check_timer = xTimerCreate("hbcheck", pdMS_TO_TICKS(1000), pdTRUE,
                                    NULL, hb_check_cb);
    configASSERT(s_hb_check_timer != NULL);
    (void)xTimerStart(s_hb_check_timer, 0);

    s_hb_frame_timer = xTimerCreate("hbframe", pdMS_TO_TICKS(1000), pdTRUE,
                                    NULL, hb_frame_cb);
    configASSERT(s_hb_frame_timer != NULL);
    (void)xTimerStart(s_hb_frame_timer, 0);
}

typedef struct {
    TaskHandle_t *handle;
    uint16_t stack_words;
} task_info_t;

static const task_info_t s_tasks[] = {
    {&th_sensor, SENSOR_STACK_WORDS},
    {&th_comm, COMM_STACK_WORDS},
    {&th_diag, DIAG_STACK_WORDS},
    {&th_watchdog, WATCHDOG_STACK_WORDS},
};

#define NUM_APP_TASKS (sizeof(s_tasks) / sizeof(s_tasks[0]))

static uint8_t task_is_running(uint32_t idx)
{
    TaskHandle_t h = *s_tasks[idx].handle;

    if (h == NULL) {
        return 0u;
    }
    return eTaskGetState(h) != eSuspended ? 1u : 0u;
}

uint8_t app_running_task_count(void)
{
    uint8_t n = 0u;

    for (uint32_t i = 0u; i < NUM_APP_TASKS; i++) {
        n += task_is_running(i);
    }
    return n;
}

uint8_t app_task_bitmap(void)
{
    uint8_t bmp = 0u;

    for (uint32_t i = 0u; i < NUM_APP_TASKS; i++) {
        if (task_is_running(i) != 0u) {
            bmp |= (uint8_t)(1u << i);
        }
    }
    return bmp;
}

uint16_t app_stack_hwm_bytes(void)
{
    uint32_t worst_used = 0u;

    for (uint32_t i = 0u; i < NUM_APP_TASKS; i++) {
        TaskHandle_t h = *s_tasks[i].handle;

        if (h == NULL) {
            continue;
        }
        uint32_t used =
            ((uint32_t)s_tasks[i].stack_words -
             (uint32_t)uxTaskGetStackHighWaterMark(h)) * 4u;
        if (used > worst_used) {
            worst_used = used;
        }
    }
    return (uint16_t)worst_used;
}

/* Fault hooks required by FreeRTOSConfig.h. */
void vApplicationMallocFailedHook(void)
{
    uart_write_str("MALLOC FAILED\n");
    taskDISABLE_INTERRUPTS();
    for (;;) {
    }
}

void vApplicationStackOverflowHook(TaskHandle_t xTask, char *pcTaskName)
{
    (void)xTask;
    uart_write_str("STACK OVERFLOW ");
    uart_write_str(pcTaskName != NULL ? pcTaskName : "?");
    uart_write_str("\n");
    taskDISABLE_INTERRUPTS();
    for (;;) {
    }
}
