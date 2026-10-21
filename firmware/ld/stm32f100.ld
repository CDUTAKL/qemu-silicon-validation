/*
 * stm32f100.ld - linker script for QEMU stm32vldiscovery (STM32F100).
 *
 * Memory layout verified against QEMU 11.0.3 hw/arm/stm32f100_soc.h:
 *   FLASH: 128 KiB @ 0x08000000 (aliased at 0x00000000 for boot)
 *   SRAM:    8 KiB @ 0x20000000
 *
 * -kernel build/firmware.bin is loaded at address 0, so the vector table
 * must be the very first bytes of the image.
 */

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 128K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 8K
}

ENTRY(Reset_Handler)

_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS
{
    .isr_vector :
    {
        . = ALIGN(4);
        KEEP(*(.isr_vector))
        . = ALIGN(4);
    } > FLASH

    .text :
    {
        . = ALIGN(4);
        *(.text)
        *(.text*)
        *(.rodata)
        *(.rodata*)
        *(.glue_7)
        *(.glue_7t)
        KEEP(*(.init))
        KEEP(*(.fini))
        . = ALIGN(4);
        _etext = .;
    } > FLASH

    .ARM.extab :
    {
        *(.ARM.extab* .gnu.linkonce.armextab.*)
    } > FLASH

    .ARM.exidx :
    {
        __exidx_start = .;
        *(.ARM.exidx*)
        __exidx_end = .;
    } > FLASH

    .data :
    {
        . = ALIGN(4);
        _sdata = .;
        *(.data)
        *(.data*)
        . = ALIGN(4);
        _edata = .;
    } > RAM AT> FLASH
    _sidata = LOADADDR(.data);

    .bss :
    {
        . = ALIGN(4);
        _sbss = .;
        *(.bss)
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        _ebss = .;
    } > RAM

    ._user_heap_stack (NOLOAD):
    {
        . = ALIGN(8);
        _end = .;
        __end__ = .;
    } > RAM

    /DISCARD/ :
    {
        *(.comment)
        *(.note*)
        *(.eh_frame*)
    }
}

/* Hard guarantee that RAM usage (data + bss + FreeRTOS heap array) fits. */
ASSERT(_ebss <= ORIGIN(RAM) + LENGTH(RAM), "RAM overflow")
