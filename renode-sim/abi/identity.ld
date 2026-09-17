/* Link the objectified app (abi/blobify.py) against nothing but itself.
 *
 * out/relink/stock-defs.o binds every library symbol back to the blob's own
 * copy, at the address the instruction held before blobify blanked it, and
 * out/relink-place.ld puts every section back where it came from, so the
 * output has to be the stock app image byte for byte. It is the proof that
 * cutting the image into sections and turning its branches into relocations
 * lost nothing; abi/relink.sh runs it before the link against the source
 * kernel. LIB holds whatever a moved layout could not fit
 * back into the image's own span; in the link against the source kernel the
 * library follows it in the same region. */

MEMORY
{
    APP   (rx)  : ORIGIN = 0x00027000, LENGTH = 0x000ca17c
    LIB   (rx)  : ORIGIN = 0x000f1180, LENGTH = 0x0000ae80
}

SECTIONS
{
    INCLUDE relink-place.ld

    /DISCARD/ : { *(.ARM.exidx*) *(.comment) *(.note*) *(.debug*) }
}
