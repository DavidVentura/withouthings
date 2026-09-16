/* Link the app blob (abi/blobify.py) against a FreeRTOS built from source.
 *
 * The blob stays at its original 0x27000; the source kernel goes into the free
 * flash argued for in hwa10.yaml's OVERLAY_FLASH, and its statics into
 * OVERLAY_RAM. The blob's own copy of the kernel stays in place and becomes
 * dead: the relocations point every app call at LIB_FLASH instead. */

MEMORY
{
    APP   (rx)  : ORIGIN = 0x00027000, LENGTH = 0x000ca17c
    LIB   (rx)  : ORIGIN = 0x000f4000, LENGTH = 0x00008000
    /* LRAM is hwa10.yaml's OVERLAY_RAM grown to 0x6000: the source heap_4 brings
     * its own 16 KB ucHeap, and 0x2003a000 is still 0x5000 below the deepest the
     * main stack reached in a 600 s run. */
    LRAM  (rw)  : ORIGIN = 0x20034000, LENGTH = 0x00006000
}

ENTRY(Reset_Handler)

/* port.c reads the initial MSP out of the app's vector table. */
PROVIDE(__isr_vector = appl_vector_table);

SECTIONS
{
    .blob : { *(.blob) } > APP

    .libtext : ALIGN(4)
    {
        *(.text .text.*)
        *(.rodata .rodata.*)
        . = ALIGN(4);
    } > LIB

    .libbss (NOLOAD) : ALIGN(4)
    {
        *(.bss .bss.* COMMON)
        . = ALIGN(4);
    } > LRAM

    /* Nothing copies the source library's .data: the blob's Reset_Handler only
     * knows about the blob's own image. Every kernel static must therefore be
     * zero-initialised and land in .bss. port.c's uxCriticalNesting is the one
     * that is not, and xPortStartScheduler assigns it before the first task
     * runs, so -fno-zero-initialized-in-bss is not needed and this holds. */
    .libdata : ALIGN(4) { *(.data .data.*) } > LRAM
    ASSERT(SIZEOF(.libdata) == 0,
           "source library has initialised data but nothing copies it; add an init stub")

    /DISCARD/ : { *(.ARM.exidx*) *(.comment) *(.note*) *(.debug*) }
}
