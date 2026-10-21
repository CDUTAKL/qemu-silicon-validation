/*
 * startup.c - vector table and minimal Reset_Handler for Cortex-M3.
 *
 * No FPU on Cortex-M3, no libc init (we are -nostdlib/-ffreestanding).
 * Reset_Handler: copy .data from flash LMA, zero .bss, call main().
 */
#include <stdint.h>

extern uint32_t _estack;
extern uint32_t _sidata;
extern uint32_t _sdata;
extern uint32_t _edata;
extern uint32_t _sbss;
extern uint32_t _ebss;

int main(void);

void Reset_Handler(void);
void Default_Handler(void);
void NMI_Handler(void) __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void MemManage_Handler(void) __attribute__((weak, alias("Default_Handler")));
void BusFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void UsageFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void) __attribute__((weak, alias("Default_Handler")));
void DebugMon_Handler(void) __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void) __attribute__((weak, alias("Default_Handler")));

/* External interrupts: only USART1 (IRQ 37) is used; everything else stays
 * weak-aliased to Default_Handler. The stm32f100 SoC has 61 IRQ lines. */
#define NUM_IRQS 61u

void USART1_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI0_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));

/* Build the external-interrupt block: IRQn at vector slot 16 + IRQn.
 * USART1 (IRQ 37) sits at index 53. */
#define IRQ_ENTRY(i) [16 + (i)] = EXTI0_IRQHandler

typedef void (*isr_t)(void);

__attribute__((section(".isr_vector"), used)) static const isr_t vector_table[16 + NUM_IRQS] = {
    (isr_t)&_estack,
    Reset_Handler,
    NMI_Handler,
    HardFault_Handler,
    MemManage_Handler,
    BusFault_Handler,
    UsageFault_Handler,
    0,
    0,
    0,
    0,
    SVC_Handler,
    DebugMon_Handler,
    0,
    PendSV_Handler,
    SysTick_Handler,
    IRQ_ENTRY(0),  IRQ_ENTRY(1),  IRQ_ENTRY(2),  IRQ_ENTRY(3),
    IRQ_ENTRY(4),  IRQ_ENTRY(5),  IRQ_ENTRY(6),  IRQ_ENTRY(7),
    IRQ_ENTRY(8),  IRQ_ENTRY(9),  IRQ_ENTRY(10), IRQ_ENTRY(11),
    IRQ_ENTRY(12), IRQ_ENTRY(13), IRQ_ENTRY(14), IRQ_ENTRY(15),
    IRQ_ENTRY(16), IRQ_ENTRY(17), IRQ_ENTRY(18), IRQ_ENTRY(19),
    IRQ_ENTRY(20), IRQ_ENTRY(21), IRQ_ENTRY(22), IRQ_ENTRY(23),
    IRQ_ENTRY(24), IRQ_ENTRY(25), IRQ_ENTRY(26), IRQ_ENTRY(27),
    IRQ_ENTRY(28), IRQ_ENTRY(29), IRQ_ENTRY(30), IRQ_ENTRY(31),
    IRQ_ENTRY(32), IRQ_ENTRY(33), IRQ_ENTRY(34), IRQ_ENTRY(35),
    IRQ_ENTRY(36),
    [16 + 37] = USART1_IRQHandler,
    IRQ_ENTRY(38), IRQ_ENTRY(39), IRQ_ENTRY(40), IRQ_ENTRY(41),
    IRQ_ENTRY(42), IRQ_ENTRY(43), IRQ_ENTRY(44), IRQ_ENTRY(45),
    IRQ_ENTRY(46), IRQ_ENTRY(47), IRQ_ENTRY(48), IRQ_ENTRY(49),
    IRQ_ENTRY(50), IRQ_ENTRY(51), IRQ_ENTRY(52), IRQ_ENTRY(53),
    IRQ_ENTRY(54), IRQ_ENTRY(55), IRQ_ENTRY(56), IRQ_ENTRY(57),
    IRQ_ENTRY(58), IRQ_ENTRY(59), IRQ_ENTRY(60),
};

void Default_Handler(void)
{
    for (;;) {
    }
}

void Reset_Handler(void)
{
    uint32_t *src = &_sidata;
    uint32_t *dst = &_sdata;

    while (dst < &_edata) {
        *dst++ = *src++;
    }

    for (dst = &_sbss; dst < &_ebss;) {
        *dst++ = 0u;
    }

    (void)main();

    for (;;) {
    }
}
