/*
 * FreeRTOSConfig.h - FreeRTOS Kernel V11.1.0 configuration.
 *
 * Target: Cortex-M3 on QEMU stm32vldiscovery (SYSCLK fixed at 24 MHz).
 * RAM is only 8 KiB, so the heap is sized tightly (see build map).
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

/* ------------------------------------------------------------------
 * Application specific definitions
 * ------------------------------------------------------------------ */
#define configUSE_PREEMPTION 1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 1
#define configUSE_TICKLESS_IDLE 0
#define configCPU_CLOCK_HZ (24000000UL)
#define configTICK_RATE_HZ (1000UL)
#define configMAX_PRIORITIES 8
#define configMINIMAL_STACK_SIZE 128 /* words */
#define configMAX_TASK_NAME_LEN 12
#define configUSE_16_BIT_TICKS 0
#define configIDLE_SHOULD_YIELD 1

/* Memory: heap_4 holds task stacks + TCBs + queues inside 8 KiB SRAM. */
#define configTOTAL_HEAP_SIZE (6144)
#define configUSE_HEAP_MALLOC_DEBUG 0
#define configUSE_MALLOC_FAILED_HOOK 1

/* Software timers (used by the heartbeat-loss watchdog checker). */
#define configUSE_TIMERS 1
#define configTIMER_TASK_PRIORITY (configMAX_PRIORITIES - 1)
#define configTIMER_QUEUE_LENGTH 8
#define configTIMER_TASK_STACK_DEPTH 192

/* Synchronisation primitives. */
#define configUSE_MUTEXES 1
#define configUSE_COUNTING_SEMAPHORES 1
#define configUSE_TASK_NOTIFICATIONS 0
#define configUSE_RECURSIVE_MUTEXES 0
#define configUSE_QUEUE_SETS 0
#define configUSE_STREAM_BUFFERS 0

/* Hooks. */
#define configUSE_IDLE_HOOK 0
#define configUSE_TICK_HOOK 0
#define configCHECK_FOR_STACK_OVERFLOW 2
#define configUSE_DAEMON_TASK_STARTUP_HOOK 0

/* Run-time stats / diagnostics (uxTaskGetSystemState, stack high-water). */
#define configUSE_TRACE_FACILITY 1
#define configUSE_STATS_FORMATTING_FUNCTIONS 0
#define configGENERATE_RUN_TIME_STATS 0

/* Interrupt nesting (4 priority bits on this NVIC model).
 * Kernel calls happen at priority 5; the UART ISR runs at priority 5 too,
 * so it is allowed to use FromISR APIs. */
#define configKERNEL_INTERRUPT_PRIORITY 255
#define configMAX_SYSCALL_INTERRUPT_PRIORITY 80 /* 5 << 4 */

#define configUSE_NEWLIB_REENTRANT 0
#define configENABLE_MPU 0
#define configENABLE_FPU 0
#define configENABLE_TRUSTZONE 0
#define configRUN_FREERTOS_SECURE_ONLY 0

/* API inclusion. */
#define INCLUDE_vTaskPrioritySet 0
#define INCLUDE_uxTaskPriorityGet 0
#define INCLUDE_vTaskDelete 0
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

/* Map the FreeRTOS port handler names onto the CMSIS vector names. */
#define vPortSVCHandler SVC_Handler
#define xPortPendSVHandler PendSV_Handler
#define xPortSysTickHandler SysTick_Handler

/* Assert: report on UART (polling) then trap. */
void uart_write_str(const char *s);
#define configASSERT(x)                                                       \
    do {                                                                      \
        if ((x) == 0) {                                                       \
            uart_write_str("ASSERT FAIL " __FILE__ "\n");                     \
            taskDISABLE_INTERRUPTS();                                         \
            for (;;) {                                                        \
            }                                                                 \
        }                                                                     \
    } while (0)

#endif /* FREERTOS_CONFIG_H */
