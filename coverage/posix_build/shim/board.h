/*
 * board.h - POSIX shim that replaces firmware/Core/board.h in the coverage
 * build. It is force-included (-include) before every Core source file, so
 * the real board.h (MMIO register definitions for STM32F100) is neutralised
 * by the shared BOARD_H include guard without modifying firmware sources.
 *
 * system_reset() is implemented in shim_board.c: it records the request so
 * the test driver can assert on it, instead of writing SCB->AIRCR (which
 * would segfault on the host).
 */
#ifndef BOARD_H
#define BOARD_H

#include <stdint.h>

static inline void nvic_enable_irq(uint32_t irq)
{
    (void)irq;
}

static inline void nvic_set_irq_priority(uint32_t irq, uint32_t prio)
{
    (void)irq;
    (void)prio;
}

void system_reset(void);

#endif /* BOARD_H */
