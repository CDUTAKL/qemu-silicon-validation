MEMORY { FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 128K }
SECTIONS {
  .text : { KEEP(*(.isr_vector)) *(.text*) } > FLASH
  /DISCARD/ : { *(.data*) *(.bss*) *(.ARM*) }
}
_estack = 0x20002000;
