/*
 * board.h - STM32F100 (QEMU stm32vldiscovery machine) register definitions.
 *
 * Verified against QEMU 11.0.3 source:
 *   - hw/arm/stm32vldiscovery.c : fixed 24 MHz SYSCLK
 *   - hw/arm/stm32f100_soc.h    : 128 KiB flash @0x08000000, 8 KiB SRAM @0x20000000
 *   - hw/arm/stm32f100_soc.c    : USART1 @0x40013800, IRQ 37, wired to serial_hd(0)
 *                                 (-serial). RCC/GPIO are unimplemented devices,
 *                                 so this firmware deliberately touches neither.
 */
#ifndef BOARD_H
#define BOARD_H

#include <stdint.h>

/* ---- NVIC (Cortex-M3 core) ---- */
#define NVIC_ISER_BASE   0xE000E100u
#define NVIC_IPR_BASE    0xE000E400u
#define SCB_AIRCR_ADDR   0xE000ED0Cu
#define SCB_AIRCR_VECTKEY 0x05FA0000u
#define SCB_AIRCR_SYSRESETREQ (1u << 2)

#define IRQ_USART1 37u

static inline void nvic_enable_irq(uint32_t irq)
{
    *(volatile uint32_t *)(NVIC_ISER_BASE + 4u * (irq >> 5)) = 1u << (irq & 31u);
}

static inline void nvic_set_irq_priority(uint32_t irq, uint32_t prio)
{
    *(volatile uint8_t *)(NVIC_IPR_BASE + irq) = (uint8_t)prio;
}

static inline void system_reset(void)
{
    *(volatile uint32_t *)SCB_AIRCR_ADDR =
        SCB_AIRCR_VECTKEY | SCB_AIRCR_SYSRESETREQ;
    for (;;) {
        /* wait for reset */
    }
}

/* ---- USART1 (QEMU stm32f2xx_usart model, F1-style register layout) ---- */
#define USART1_BASE 0x40013800u

#define USART_SR   (*(volatile uint32_t *)(USART1_BASE + 0x00u))
#define USART_DR   (*(volatile uint32_t *)(USART1_BASE + 0x04u))
#define USART_BRR  (*(volatile uint32_t *)(USART1_BASE + 0x08u))
#define USART_CR1  (*(volatile uint32_t *)(USART1_BASE + 0x0Cu))
#define USART_CR2  (*(volatile uint32_t *)(USART1_BASE + 0x10u))
#define USART_CR3  (*(volatile uint32_t *)(USART1_BASE + 0x14u))

#define USART_SR_RXNE (1u << 5)
#define USART_SR_TC   (1u << 6)
#define USART_SR_TXE  (1u << 7)

#define USART_CR1_RE      (1u << 2)
#define USART_CR1_TE      (1u << 3)
#define USART_CR1_RXNEIE  (1u << 5)
#define USART_CR1_UE      (1u << 13)

/* APB2 = 24 MHz in QEMU; 24e6/115200 = 208.33 -> BRR 0x0D05 (cosmetic only,
 * the emulated UART has no baud timing). */
#define USART1_BRR_115200 0x00000D05u

#endif /* BOARD_H */
