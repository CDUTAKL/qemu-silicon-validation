/*
 * uart.c - USART1 driver for QEMU stm32f2xx_usart model (F1 register map).
 *
 * TX: polling (the emulated UART shifts out synchronously; SR.TXE is
 * effectively always set). Writes run inside a short critical section so
 * CommTask frames and DiagTask text lines can never interleave.
 *
 * RX: interrupt-driven into a ring buffer; a binary semaphore wakes the
 * DiagTask parser. The ISR is safe before the scheduler starts (the
 * semaphore handle is NULL-checked).
 */
#include "uart.h"

#include "board.h"
#include "minilib.h"

/* FreeRTOS headers needed for the ISR semaphore / critical sections. */
#include "FreeRTOS.h"
#include "queue.h"
#include "semphr.h"
#include "task.h"

#define UART_RX_RING_SIZE 128u

static volatile uint8_t s_ring[UART_RX_RING_SIZE];
static volatile uint16_t s_ring_wr = 0u;
static volatile uint16_t s_ring_rd = 0u;

static SemaphoreHandle_t s_rx_sem;

SemaphoreHandle_t uart_rx_sem(void)
{
    return s_rx_sem;
}

void uart_init(void)
{
    s_rx_sem = xSemaphoreCreateBinary();
    /* Buffer indexes already zero (bss); no flush needed at boot. */

    USART_BRR = USART1_BRR_115200;
    USART_CR2 = 0u;
    USART_CR3 = 0u;

    /* Priority 5 (numerically >= configMAX_SYSCALL_INTERRUPT_PRIORITY) so the
     * ISR may use FromISR APIs. IPR fields are 8-bit, shifted by 4. */
    nvic_set_irq_priority(IRQ_USART1, 0x50u);
    nvic_enable_irq(IRQ_USART1);

    USART_CR1 = USART_CR1_UE | USART_CR1_RE | USART_CR1_TE | USART_CR1_RXNEIE;
}

void uart_write(const uint8_t *buf, size_t len)
{
    taskENTER_CRITICAL();
    for (size_t i = 0u; i < len; i++) {
        while ((USART_SR & USART_SR_TXE) == 0u) {
        }
        USART_DR = buf[i];
    }
    /* Wait for the last byte to leave the shift register. */
    while ((USART_SR & USART_SR_TC) == 0u) {
    }
    taskEXIT_CRITICAL();
}

void uart_write_str(const char *s)
{
    uart_write((const uint8_t *)s, strlen(s));
}

static int ring_put(uint8_t b)
{
    uint16_t next = (uint16_t)((s_ring_wr + 1u) % UART_RX_RING_SIZE);

    if (next == s_ring_rd) {
        return -1; /* full */
    }
    s_ring[s_ring_wr] = b;
    s_ring_wr = next;
    return 0;
}

int uart_rx_get(uint8_t *out)
{
    int got = 0;

    taskENTER_CRITICAL();
    if (s_ring_rd != s_ring_wr) {
        *out = s_ring[s_ring_rd];
        s_ring_rd = (uint16_t)((s_ring_rd + 1u) % UART_RX_RING_SIZE);
        got = 1;
    }
    taskEXIT_CRITICAL();
    return got;
}

/* Declared weak in startup.c's vector table. */
void USART1_IRQHandler(void);

void USART1_IRQHandler(void)
{
    BaseType_t higher_prio_woken = pdFALSE;

    while ((USART_SR & USART_SR_RXNE) != 0u) {
        uint8_t b = (uint8_t)(USART_DR & 0xFFu);
        if (ring_put(b) == 0 && s_rx_sem != NULL) {
            xSemaphoreGiveFromISR(s_rx_sem, &higher_prio_woken);
        }
    }

    if (higher_prio_woken != pdFALSE) {
        portYIELD_FROM_ISR(higher_prio_woken);
    }
}
