#ifndef UART_H
#define UART_H

#include <stddef.h>
#include <stdint.h>

#include "FreeRTOS.h"
#include "semphr.h"

void uart_init(void);

/* Transmit a buffer (blocking, critical-section protected). */
void uart_write(const uint8_t *buf, size_t len);
void uart_write_str(const char *s);

/* Non-blocking ring-buffer pop. Returns 1 when a byte was stored in *out. */
int uart_rx_get(uint8_t *out);

/* Binary semaphore given by the RX ISR; diag_task blocks on it. */
SemaphoreHandle_t uart_rx_sem(void);

#endif /* UART_H */
