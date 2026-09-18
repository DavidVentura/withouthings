/* HWA10 (ScanWatch 2) application firmware v3411: the flash module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_FLASH_H
#define WITHINGS_FLASH_H

/* what fwblk_table_parse fills at 0x4cb5c..0x4cb66: the external-flash
   offset of the payload (the header offset plus 4), the u16 length from the
   header widened to a word, and the buffer holding header + payload + CRC32
   */
struct fwblk_table {
    unsigned int payload_offset;
    unsigned int len;
    void *body;
};

/* params are not decoded; only jedec_id is matched */
struct flash_id {
    unsigned int jedec_id;
    unsigned char params[12];
};

/* functions */
/* Reads the {u16 ver, u16 len} header at flash_offset out of the external
   flash through spi_flash_handle (0xb244c is its only pool word), rejects
   the version with fwblk_unsupported_ext_table_ver (0x4ca64), reads 4 +
   align4(len) + 4 bytes into body_buf, and checks the trailing CRC32 against
   crc32(body, 4 + align4(len)) -- the log lines "version %d", "size %d" and
   "[FWBLK] ext. table corrupted (crc computed=0x%lx, extracted=0x%lx)" are
   its own. Both pointer arguments are optional: a NULL body_buf is malloc'd
   (and freed again on any failure, which is how the caller-supplied case is
   told apart at 0x4cb38), a NULL out is a malloc of 12, the struct's size.
   Returns out, or NULL on any failure. Declared here because seeding the
   neighbour 0x9800a cost this body its function start: 0x9800e/0x98012
   tail-call into it, so with no start of its own the whole body became a
   second range of 0x9800a.
   */
extern struct fwblk_table *fwblk_table_parse(unsigned int flash_offset, struct fwblk_table *out, void *body_buf);
/* `movs r2,#0; mov r1,r2; b.w fwblk_table_parse` -- the both-allocated form.
   Its caller 0x655f0 is the shell's bank dump, which passes 0x6000 for bank
   1 and 0x11f000 for bank 2, the two bank offsets FIRMWARE.md gives, so
   flash_offset is a byte offset into the external flash.
   */
extern struct fwblk_table *fwblk_table_open(unsigned int flash_offset);
/* frees table->body then table, NULL-safe; returns its argument because the
   free is a tail call
   */
extern struct fwblk_table *fwblk_table_free(struct fwblk_table *table);
/* hands back body+4 and the u16 length the header holds at body+2, i.e. the
   IE stream fwblk_get_ie (0x4cb90) walks
   */
extern void fwblk_table_payload(struct fwblk_table *table, const void **out_data, unsigned short *out_len);
/* Twenty-two bytes of which ten are not a relocated field, and those ten are
   newlib's _tzset_r exactly: push, move the argument aside, call, move it
   back, pop and tail-call. Every lock/operate/unlock wrapper and every errno
   stub has that shape, so the bytes name nothing and the branches do. These
   three go to 0x5b748, which takes the mutex at *0x0120e8f8 with
   xQueueSemaphoreTake and an infinite timeout, to 0x5b6ec, an SPI-flash
   operation that reaches spi_xfer_wait and is also called from
   flash_detect_fn, and to 0x5b6a4, which gives the same mutex back with
   xQueueGenericSend; the only caller is shell_cmd_spiflash. The argument
   travels through r0 untouched into the operation, which is all the wrapper
   does with it. Naming it here is what keeps the relink from replacing a
   flash lock with tzset, where only the shell would have run the difference.
   */
extern int spi_flash_locked_op(unsigned int arg);

/* globals */
extern void *spi_flash_handle;

/* tables */
extern struct flash_id flash_id_table[28];

#endif
