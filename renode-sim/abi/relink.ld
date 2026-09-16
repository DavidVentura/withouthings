/* Link the app blob (abi/blobify.py) against a FreeRTOS built from source.
 *
 * The blob stays at its original 0x27000; the source kernel goes into the free
 * flash argued for in hwa10.yaml's OVERLAY_FLASH, which starts at the app image
 * end so app and library are one contiguous block, and its statics into
 * OVERLAY_RAM. The blob's own copy of the kernel stays in place and becomes
 * dead: the relocations point every app call at LIB_FLASH instead. */

MEMORY
{
    APP   (rx)  : ORIGIN = 0x00027000, LENGTH = 0x000ca17c
    /* Immediately after the app image (0xf117c) so that `appl` stays one part:
     * a package describes each part by one address and one length, and the
     * bootloader copies that many bytes to 0x27000 in one go. The four bytes of
     * padding are the section's own alignment -- relink_glue.c's naked
     * vPortEnableVFP ends in `.align 4`, so .libtext wants 16 -- and
     * relink_image.py takes the placed address out of the ELF rather than
     * trusting this number. */
    LIB   (rx)  : ORIGIN = 0x000f1180, LENGTH = 0x0000ae80
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
        __libbss_start = .;
        *(.bss .bss.* COMMON)
        . = ALIGN(4);
        __libbss_end = .;
    } > LRAM

    /* relink_startup (relink_glue.c) zeroes .libbss, but nothing copies the
     * source library's .data: the blob's Reset_Handler only knows about the
     * blob's own image. Every kernel static must therefore be zero-initialised
     * and land in .bss. port.c's uxCriticalNesting is the one that is not, and
     * xPortStartScheduler assigns it before the first task runs, so
     * -fno-zero-initialized-in-bss is not needed and this holds. If a static
     * ever does need a non-zero initialiser, relink_startup is where the .data
     * copy goes, with a LOAD region in .libtext to copy it from. */
    .libdata : ALIGN(4) { *(.data .data.*) } > LRAM
    ASSERT(SIZEOF(.libdata) == 0,
           "source library has initialised data but nothing copies it; add an init stub")

    /DISCARD/ : { *(.ARM.exidx*) *(.comment) *(.note*) *(.debug*) }
}
