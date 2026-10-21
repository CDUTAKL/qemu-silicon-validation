/*
 * posix_uart_shim.c - POSIX implementation of firmware/Core/uart.h.
 *
 * On target, uart.c drives USART1 MMIO registers and gives a semaphore from
 * the RX ISR. Here the "wire" is two pthread-mutex-protected ring buffers
 * so the test driver can inject host bytes and capture everything the
 * firmware transmits, byte-exact.
 */
#include "uart.h"

#include <pthread.h>
#include <string.h>

#include "shim_uart.h"

#define TX_CAP 8192u
#define RX_CAP 1024u

static pthread_mutex_t s_lock = PTHREAD_MUTEX_INITIALIZER;

static uint8_t s_tx[TX_CAP];
static size_t s_tx_head; /* write */
static size_t s_tx_tail; /* read */

static uint8_t s_rx[RX_CAP];
static size_t s_rx_head;
static size_t s_rx_tail;

static SemaphoreHandle_t s_rx_sem;

void uart_init(void)
{
    if (s_rx_sem == NULL) {
        s_rx_sem = xSemaphoreCreateBinary();
    }
}

/* ---- firmware end (uart.h) ---- */

void uart_write(const uint8_t *buf, size_t len)
{
    pthread_mutex_lock(&s_lock);
    for (size_t i = 0u; i < len; i++) {
        size_t next = (s_tx_head + 1u) % TX_CAP;
        if (next == s_tx_tail) {
            break; /* capture full: drop (test drains regularly) */
        }
        s_tx[s_tx_head] = buf[i];
        s_tx_head = next;
    }
    pthread_mutex_unlock(&s_lock);
}

void uart_write_str(const char *s)
{
    uart_write((const uint8_t *)s, strlen(s));
}

int uart_rx_get(uint8_t *out)
{
    int got = 0;

    pthread_mutex_lock(&s_lock);
    if (s_rx_tail != s_rx_head) {
        *out = s_rx[s_rx_tail];
        s_rx_tail = (s_rx_tail + 1u) % RX_CAP;
        got = 1;
    }
    pthread_mutex_unlock(&s_lock);
    return got;
}

SemaphoreHandle_t uart_rx_sem(void)
{
    return s_rx_sem;
}

/* ---- test end (shim_uart.h) ---- */

size_t shim_uart_host_send(const uint8_t *data, size_t len)
{
    size_t n = 0u;

    pthread_mutex_lock(&s_lock);
    for (size_t i = 0u; i < len; i++) {
        size_t next = (s_rx_head + 1u) % RX_CAP;
        if (next == s_rx_tail) {
            break;
        }
        s_rx[s_rx_head] = data[i];
        s_rx_head = next;
        n++;
    }
    pthread_mutex_unlock(&s_lock);

    if (n > 0u && s_rx_sem != NULL) {
        xSemaphoreGive(s_rx_sem);
    }
    return n;
}

size_t shim_uart_host_send_str(const char *s)
{
    return shim_uart_host_send((const uint8_t *)s, strlen(s));
}

size_t shim_uart_tx_read(uint8_t *buf, size_t max)
{
    size_t n = 0u;

    pthread_mutex_lock(&s_lock);
    while (n < max && s_tx_tail != s_tx_head) {
        buf[n++] = s_tx[s_tx_tail];
        s_tx_tail = (s_tx_tail + 1u) % TX_CAP;
    }
    pthread_mutex_unlock(&s_lock);
    return n;
}

size_t shim_uart_tx_pending(void)
{
    size_t n;

    pthread_mutex_lock(&s_lock);
    n = (s_tx_head - s_tx_tail) % TX_CAP;
    pthread_mutex_unlock(&s_lock);
    return n;
}
