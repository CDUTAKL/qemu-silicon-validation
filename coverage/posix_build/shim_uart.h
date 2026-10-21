/*
 * shim_uart.h - test-side control API for the POSIX UART shim.
 *
 * The shim implements firmware/Core/uart.h (uart_init/uart_write/
 * uart_write_str/uart_rx_get/uart_rx_sem) on top of two mutex-protected
 * ring buffers: TX capture (firmware -> test) and RX inject (test ->
 * firmware). This header exposes the test end of the pipe.
 */
#ifndef SHIM_UART_H
#define SHIM_UART_H

#include <stddef.h>
#include <stdint.h>

/* Push host bytes into the firmware RX path and wake DiagTask.
 * Returns the number of bytes accepted (may be less than len if the RX
 * ring is full). Safe to call from any task/thread. */
size_t shim_uart_host_send(const uint8_t *data, size_t len);

/* Convenience wrapper for NUL-terminated ASCII command lines. */
size_t shim_uart_host_send_str(const char *s);

/* Copy up to max bytes out of the TX capture buffer (consuming them).
 * Returns the number of bytes copied. */
size_t shim_uart_tx_read(uint8_t *buf, size_t max);

/* Number of bytes currently waiting in the TX capture buffer. */
size_t shim_uart_tx_pending(void);

#endif /* SHIM_UART_H */
