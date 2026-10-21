/* Minimal Cortex-M3 idle ELF for harness bring-up.
 * Vector table: MSP + 15 exception vectors -> Default_Handler (thumb bit via +1).
 * Handler: WFI loop so the simulation stays alive and QMP remains responsive. */
    .syntax unified
    .cpu cortex-m3
    .thumb
    .section .isr_vector,"a",%progbits
    .word _estack
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1
    .word Default_Handler + 1

    .thumb_func
    .section .text
    .global Default_Handler
Default_Handler:
    wfi
    b Default_Handler
