/*
 * FreeRTOSConfig.h - FreeRTOS Kernel V11.1.0 configuration for the
 * POSIX (pthread) port on macOS, used ONLY by the coverage build.
 *
 * The ARM target uses firmware/Core/FreeRTOSConfig.h; this file tunes the
 * same application for the host port: bigger heap, byte-counted stack sizes
 * are still expressed in StackType_t words (8 bytes each on a 64-bit host),
 * and configASSERT goes to stderr + abort instead of the target UART.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#define configUSE_PREEMPTION 1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 0
#define configUSE_TICKLESS_IDLE 0
#define configCPU_CLOCK_HZ (1000000UL)
#define configTICK_RATE_HZ (1000UL)
#define configMAX_PRIORITIES 8
/* The POSIX port stores Thread_t at the start of the allocated stack and
 * the real C stack belongs to the pthread, so the nominal depth only needs
 * to hold the thread struct; the app values (96..192 words) are fine. */
#define configMINIMAL_STACK_SIZE 128 /* words */
#define configMAX_TASK_NAME_LEN 12
#define configUSE_16_BIT_TICKS 0
#define configIDLE_SHOULD_YIELD 1

#define configTOTAL_HEAP_SIZE (256 * 1024)
#define configUSE_MALLOC_FAILED_HOOK 1

#define configUSE_TIMERS 1
#define configTIMER_TASK_PRIORITY (configMAX_PRIORITIES - 1)
#define configTIMER_QUEUE_LENGTH 8
#define configTIMER_TASK_STACK_DEPTH 256

#define configUSE_MUTEXES 1
#define configUSE_COUNTING_SEMAPHORES 1
#define configUSE_TASK_NOTIFICATIONS 0
#define configUSE_RECURSIVE_MUTEXES 0
#define configUSE_QUEUE_SETS 0
#define configUSE_STREAM_BUFFERS 0

#define configUSE_IDLE_HOOK 0
#define configUSE_TICK_HOOK 0
#define configCHECK_FOR_STACK_OVERFLOW 2
#define configUSE_DAEMON_TASK_STARTUP_HOOK 0

#define configUSE_TRACE_FACILITY 1
#define configUSE_STATS_FORMATTING_FUNCTIONS 0
#define configGENERATE_RUN_TIME_STATS 0

#define configUSE_NEWLIB_REENTRANT 0

#define INCLUDE_vTaskPrioritySet 0
#define INCLUDE_uxTaskPriorityGet 0
#define INCLUDE_vTaskDelete 1
#define INCLUDE_vTaskCleanUpResources 0
#define INCLUDE_vTaskSuspend 1
#define INCLUDE_xTaskDelayUntil 1
#define INCLUDE_vTaskDelay 1
#define INCLUDE_xTaskGetTickCount 1
#define INCLUDE_uxTaskGetStackHighWaterMark 1
#define INCLUDE_xTaskGetIdleTaskHandle 0
#define INCLUDE_eTaskGetState 1
#define INCLUDE_xEventGroupSetBitFromISR 0
#define INCLUDE_xTimerPendFunctionCall 0
#define INCLUDE_xTaskAbortDelay 0
#define INCLUDE_xTaskGetHandle 0
#define INCLUDE_xTaskResumeFromISR 0

#include <stdio.h>
#include <stdlib.h>
#define configASSERT(x)                                                       \
    do {                                                                      \
        if ((x) == 0) {                                                       \
            fprintf(stderr, "ASSERT FAIL %s:%d\n", __FILE__, __LINE__);       \
            abort();                                                          \
        }                                                                     \
    } while (0)

#endif /* FREERTOS_CONFIG_H */
