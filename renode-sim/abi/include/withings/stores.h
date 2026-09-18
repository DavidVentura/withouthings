/* HWA10 (ScanWatch 2) application firmware v3411: the storage layer's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses, written for this layer by abi/stores.py.
 *
 * The external SPI NOR part (8 MB) carries, from the bottom up: the driver
 * in flash.h, the flash translation layer WFTL, and the stores built on it.
 * dblib does not go through WFTL -- it has its own port and its own banks,
 * declared at the end of this file. */
#ifndef WITHINGS_STORES_H
#define WITHINGS_STORES_H

/* ------------------------------------------------------------------ WFTL */

/* The translation layer owns the external flash from 0x238000 to the end of
   the 8 MB part: 0x5c8 (1480) blocks of 4096 bytes. Both numbers are the
   image's own: `wftl print origin` prints 0x238000 (0x69376), `wftl print
   addr <index>` computes (index + 0x238) << 12 (0x693a6), every scan loop
   stops at 0x5c8, and wftl_post_update_cleanup erases twenty passes of
   0x4a000 bytes, which is 0x5c8000 exactly. */
#define WFTL_ORIGIN         0x238000
#define WFTL_BLOCK_COUNT    0x5c8
#define WFTL_BLOCK_SIZE     0x1000
/* wftl_block_payload_size (0x9d07e) returns this: the block less its header,
   and the bound wftl_read and wftl_write put on offset + len. */
#define WFTL_BLOCK_PAYLOAD  0xff8
/* wftl_type_count (0x9d0cc) returns 0x17. */
#define WFTL_TYPE_COUNT     0x17
/* The type field is six bits and an all-ones type is an erased block. */
#define WFTL_TYPE_ERASED    0x3f
/* No block: the value every block half of a cursor is reset to, and what
   wftl_find_block returns on a miss. It is the block count, so it can never
   be a block. */
#define WFTL_NO_BLOCK       0x5c8
/* No index / no next block: the 15-bit fields are all ones when unwritten. */
#define WFTL_NO_INDEX       0x7fff

/* The eight bytes at the front of every WFTL block, as wftl_block_header_read
   (0x9d0d0) decodes them and wftl_block_erase (0x9d15c) writes them back.

   NOR programming only clears bits, so the two flag bits are written after
   the erase and never cleared again: wftl_block_erase programs the word with
   byte 3 all ones (0x9d190), then programs 0xbfffffff over it (0x9d19e),
   which clears `initialised` alone. `released` is the bit a later write
   clears to retire a block.

   `next_block` is a block number, not an index: wftl_erase_index_locked
   stores it straight into the type's first_block (0x69f9a..0x69fac) and
   wftl_find_block walks the chain through it (0x698bc..0x698d4). The block
   whose next_block is WFTL_NO_INDEX is the tail, which is what
   "[WFTL] Type %d not requesting last block + 1" is about. */
struct wftl_block_header {
    unsigned int erase_count : 24;  /* +0, bits 0..23; +1 per erase */
    unsigned int type : 6;          /* bits 24..29; 0x3f = erased */
    unsigned int initialised : 1;   /* bit 30, cleared after the erase */
    unsigned int released : 1;      /* bit 31 */
    unsigned short index : 15;      /* +4, the logical index in the ring */
    unsigned short index_valid : 1;
    unsigned short next_block : 15; /* +6, or 0x7fff at the tail */
    unsigned short next_valid : 1;
};

/* wftl_block_header_read's verdict, which the `wftl list` branch at
   0x6933c..0x69358 prints by these names. */
enum wftl_block_state {
    WFTL_BLOCK_USED = 0,
    WFTL_BLOCK_CORRUPTED = 1,
    WFTL_BLOCK_EMPTY = 2,
};

/* What the entry points return. 3 is every argument check in wftl_read,
   wftl_write, wftl_erase_index and wftl_erase_type (0x695f4, 0x69672,
   0x696f8, 0x6973a); 5 is the two "should have found" refusals. */
enum wftl_status {
    WFTL_OK = 0,
    WFTL_BAD_ARGUMENT = 3,
    WFTL_NOT_FOUND = 5,
};

/* One of a WFTL type's four (index, block) cursors. */
struct wftl_cursor {
    unsigned short index;
    unsigned short block;
};

/* The eighteen bytes wftl_type_state_reset (0x69934) writes, one per type:
   the index count, then four cursors it fills with (count, WFTL_NO_BLOCK).

   `nb_indices` is the ring modulus -- wftl_erase_index_locked advances
   first.index by one modulo it at 0x69f98 -- and a zero count is a type this
   build does not use, which is the first thing every entry point rejects.

   `first` and `last` are the ends of the block chain, rebuilt by wftl_mount:
   `last` is the block whose header has no next_block (0x69adc..0x69b1c),
   `first` is the one no other block points at. `write` and `read` are caches
   of the last index each path reached, and wftl_find_block (0x697ec) answers
   from whichever of the four holds the index before it walks. */
struct wftl_type_state {
    unsigned short nb_indices;
    struct wftl_cursor first;   /* +2 */
    struct wftl_cursor last;    /* +6 */
    struct wftl_cursor write;   /* +0xa, set by wftl_write_locked */
    struct wftl_cursor read;    /* +0xe, set by wftl_read_locked */
};

/* What wftl_reserve_blocks (0x6a000) leaves behind: the blocks it picked,
   lowest erase count first, and the type it picked them for. Ten is the
   ceiling the body refuses above (cmp r1,#0xa at 0x6a01a). */
struct wftl_reservation {
    unsigned short block[10];
    unsigned char type;         /* +0x14 */
    unsigned char count;        /* +0x15 */
    unsigned char fresh;        /* +0x16, blocks whose erase count is zero */
};

/* functions */
/* the scan wftl_init runs; see abi/symbols.yaml for the evidence of each */
extern void wftl_init(void);
extern enum wftl_status wftl_read(unsigned char type, unsigned short index,
                                  unsigned int offset, void *buf,
                                  unsigned int len);
extern enum wftl_status wftl_write(unsigned char type, unsigned short index,
                                   unsigned int offset, const void *buf,
                                   unsigned int len, unsigned int flags);
extern enum wftl_status wftl_erase_index(unsigned char type,
                                         unsigned short index);
extern enum wftl_status wftl_erase_type(unsigned char type);
/* the block, or WFTL_NO_BLOCK */
extern unsigned short wftl_find_block(unsigned char type,
                                      unsigned short index);
extern enum wftl_block_state wftl_block_header_read(unsigned short block,
                                                    unsigned short *out_index,
                                                    unsigned char *out_type,
                                                    unsigned int *out_erase_count);
extern void wftl_block_erase(unsigned short block, const unsigned int *keep);
extern void wftl_format(void);
extern int wftl_reserve_blocks(unsigned char type, unsigned char nb_blocks);
extern unsigned int wftl_block_payload_size(void);
extern unsigned char wftl_type_count(void);

/* globals */
/* the 23 states, 18 bytes apart */
extern struct wftl_type_state wftl_type_state[WFTL_TYPE_COUNT];
extern struct wftl_reservation wftl_reservation;
extern void *wftl_mutex;

/* ------------------------------------------------------- circularfs (CFS) */

/* An append-only log of variable-length elements over one WFTL type's ring of
   indices. A CFS block is a WFTL index, so a block holds WFTL_BLOCK_PAYLOAD
   bytes; the first four of them are the block's sequence number and the
   elements start at offset 4 (the strd of 0x40000 over both cursors at
   0x441d4 is {block 0, offset 4} twice).

   NOR programming only clears bits, which is the whole trick of the format:
   an element's head header is written open, the payload is appended, and the
   same twelve bytes are programmed again with the length and the CRC once the
   element is whole. The bytes that must not change are written back as 0xff.

   Log tag "[CFS]". flash_cache is what opens contexts over it. */

/* the sequence number an erased block reads as, and what cfs_block_seq_read
   answers on a failed read */
#define CFS_SEQ_NONE 0xffffffffu
/* elements start after the block's sequence number */
#define CFS_DATA_START 4
/* a block with fewer than this many bytes left takes no new element:
   payload_size - 0x18, the comparison at 0x94cbc, 0x4425c and 0x94f2a */
#define CFS_BLOCK_TAIL 0x18

/* the two bits of the header's flags byte (byte 2), read at 0x94d8e and
   branched on identically at 0x4424c, 0x4433c and 0x951d0 */
enum cfs_chunk_state {
    CFS_CHUNK_HEAD = 0,   /* a twelve-byte header: this element begins here */
    CFS_CHUNK_CONT = 1,   /* a four-byte header: the element continues */
    CFS_CHUNK_FREE = 3,   /* unwritten; the scan of the block stops */
};

/* the four bytes every chunk of an element starts with */
struct cfs_chunk_header {
    unsigned short len;     /* +0, payload bytes in this chunk */
    unsigned char state;    /* +2, bits 0..1; the rest stay set */
    unsigned char pad;      /* +3, never programmed */
};

/* the twelve bytes a CFS_CHUNK_HEAD chunk starts with: the chunk header and
   the element's whole length and CRC, both programmed by cfs_element_commit
   after the payload is on flash

   The CRC is the image's own table-driven CRC-32 at 0xb2614: polynomial
   0xedb88320, initial and final 0xffffffff. cfs_element_load recomputes it
   over the payload and refuses the element on a difference, which is the only
   integrity check the format has. */
struct cfs_element_header {
    struct cfs_chunk_header chunk;
    unsigned int length;    /* +4 */
    unsigned int crc32;     /* +8 */
};

/* the caller's handle on one element, twelve bytes, moved around whole with
   three-word ldm/stm (0x4431c, 0x95274) */
struct cfs_element {
    unsigned short first_block;   /* +0, where the head header is */
    unsigned short first_offset;  /* +2 */
    unsigned short cur_block;     /* +4, where the next byte goes or comes */
    unsigned short cur_offset;    /* +6 */
    unsigned int length;          /* +8, the payload bytes so far */
};

/* the sixteen bytes of CFS state, which is also the front of a flash cache
   context because flash_cache_init writes the first two fields itself */
struct cfs_ctx {
    unsigned short wftl_type;     /* +0, taken as a u8 at every WFTL call */
    unsigned short nb_blocks;     /* +2, the ring length and the modulus */
    unsigned short read_block;    /* +4 */
    unsigned short read_offset;   /* +6 */
    unsigned short write_block;   /* +8 */
    unsigned short write_offset;  /* +0xa */
    unsigned int last_seq;        /* +0xc, the highest sequence number seen */
};

/* functions */
extern int cfs_init(struct cfs_ctx *ctx, int force_erase);
extern int cfs_first(struct cfs_ctx *ctx, struct cfs_element *out);
extern int cfs_next(struct cfs_ctx *ctx, struct cfs_element *out);
extern int cfs_element_begin(struct cfs_ctx *ctx, struct cfs_element *el);
extern int cfs_element_write(struct cfs_ctx *ctx, struct cfs_element *el,
                             unsigned int len, const void *buf, int at);
extern int cfs_element_commit(struct cfs_ctx *ctx, struct cfs_element *el);
extern int cfs_element_read(struct cfs_ctx *ctx, const struct cfs_element *el,
                            unsigned int len, void *buf, int at);
extern unsigned int cfs_element_size(struct cfs_ctx *ctx,
                                     const struct cfs_element *el);

/* ------------------------------------------------------- the flash cache */

/* "Flash Cache (over WFTL)", the shell's own words (0xd7827): a keyed store
   of opaque blobs over one circularfs. An element's payload is {u32 id; the
   blob}, so the id costs four bytes of every record and a lookup is a walk
   comparing the leading word -- flash_cache_read subtracts the four at
   0x4c52e. A context may carry a RAM table of recently found records so a
   repeat lookup skips the walk.

   Log tag "[FLASH CACHE]". */

/* one row of the RAM lookup table; a zero id is an empty slot */
struct flash_cache_entry {
    unsigned int id;
    struct cfs_element element;
};

/* the 0x20 bytes flash_cache_deinit memsets */
struct flash_cache_ctx {
    struct cfs_ctx cfs;                 /* +0 */
    struct flash_cache_entry *table;    /* +0x10, or NULL */
    unsigned short table_next;          /* +0x14, the ring insert point */
    unsigned short table_capacity;      /* +0x16, rows */
    /* +0x18 is a flag flash_cache_preload_ids raises and every table clear
       drops, read only by a getter this map does not name: whether it means
       "every requested id is resident" or "the table is authoritative" is two
       readings and no code in the layer branches on it. */
    unsigned short preloaded;
    unsigned short pad;
    void *mutex;                        /* +0x1c */
};

/* functions */
extern void flash_cache_init(struct flash_cache_ctx *ctx,
                             unsigned short wftl_type,
                             unsigned short nb_blocks,
                             struct flash_cache_entry *table,
                             unsigned short table_capacity,
                             unsigned int force_format);
extern void flash_cache_deinit(struct flash_cache_ctx *ctx);
extern int flash_cache_insert(struct flash_cache_ctx *ctx, unsigned int id,
                              unsigned int nseg, const unsigned int *lens,
                              const void *const *bufs);
extern int flash_cache_read(struct flash_cache_ctx *ctx, unsigned int id,
                            unsigned int *out_len, void *buf,
                            unsigned int buf_size);
extern int flash_cache_list_ids(struct flash_cache_ctx *ctx, unsigned int *out,
                                unsigned int max);
extern int flash_cache_format(struct flash_cache_ctx *ctx);

/* The glyph cache stores {u8 width; u8 height; u8 bitmap[width *
   ((height + 7) >> 3)]} under the glyph id, over two flash caches: WFTL type
   6 with two blocks and WFTL type 7 with three and a twelve-row RAM table.
   glyph_cache_insert refuses either dimension above 0x22. */
struct glyph_cache_record {
    unsigned char width;
    unsigned char height;
    unsigned char bitmap[1];
};

/* The workout cache is one flash cache over WFTL type 0xa with a single
   block. Its key is not a plain id: the top nibble is which of the three
   records this is and the rest is the workout id, which is why one workout
   costs three elements.

   The info record is 36 opaque bytes; the name and its length are the only
   fields the WPP handler's own log lines fix ("name too long" against a
   ceiling of 0x20 at 0x6b2ec), so the two bytes after it are left unnamed. */
enum workout_cache_kind {
    WORKOUT_CACHE_INFO = 0,
    WORKOUT_CACHE_ICON_SMALL = 1,   /* bounded at 20 x 20 */
    WORKOUT_CACHE_ICON_BIG = 2,     /* bounded at 34 x 34 */
};

struct workout_cache_info {
    char name[32];
    unsigned char unknown_20;
    unsigned char pad;
    unsigned short unknown_22;
};

/* -------------------------------------------------- the vasistas store */

/* A vasistas is one measurement record. The records live in fourteen banks,
   each bank one WFTL type and a fixed ring of indices, and inside a block
   they are packed with no table of contents at all: the walk is the format.

   What makes a walk possible in both directions is the header, which carries
   this record's size in bits 14..21 and the previous record's size in bits
   6..13 -- vasistas_append writes the second out of the bank's last-record
   size (0x665bc) and vasistas_walk_prev subtracts it (0x9c8de). The size
   field and the per-type size table must agree or vasistas_read_record
   refuses the record (-4 at 0x66788), which is the store's only check. */

/* the six-bit type field's all-ones value: no record */
#define VASISTAS_TYPE_NONE 0x3f
/* vasistas_bank_count */
#define VASISTAS_BANK_COUNT 14

/* the eight bytes every record starts with */
struct vasistas_header {
    unsigned int timestamp;         /* +0, unix seconds */
    unsigned int type : 6;          /* +4, bits 0..5 */
    unsigned int prev_size : 8;     /* bits 6..13, the record before this one */
    unsigned int size : 8;          /* bits 14..21 */
    unsigned int size_valid : 1;    /* bit 22, set by every writer */
    unsigned int payload : 9;       /* bits 23..31, where the record's own
                                       fields begin */
};

/* one bank: the WFTL type it lives in, the ring length, and where the writer
   and the oldest record are. vasistas_bank_reset (0x65864) keeps the first
   and the last field and zeroes the rest, which is what says those two are
   configuration and the four between them are state. */
struct vasistas_bank {
    unsigned char wftl_type;      /* +0 */
    unsigned char pad[3];
    unsigned int tail_index;      /* +4, the oldest block */
    unsigned int head_index;      /* +8, where the next record goes */
    unsigned int write_offset;    /* +0xc, inside that block */
    unsigned int last_size;       /* +0x10, what the next header back-links */
    unsigned int nb_indices;      /* +0x14, the ring length */
};

/* functions */
extern void vasistas_banks_init(void);
extern void vasistas_init(void);
extern void vasistas_erase_all(void);
/* the bank a WFTL type names, or NULL with "vas: can't get coords for bank" */
extern struct vasistas_bank *vasistas_bank_for_type(unsigned char wftl_type);
/* the WFTL type a record type belongs in, or 0xff for one that is not stored */
extern unsigned char vasistas_bank_for_record(unsigned char record_type);
extern unsigned int vasistas_record_size(const struct vasistas_header *rec);
extern unsigned int vasistas_size_from_header(const struct vasistas_header *r);
/* (wftl_type, index, offset) -- the sim's trace shows the walk calling it as
   vasistas_read_record(1, 4, 0x45c) and wftl_read(1, 4, 0x45c) straight
   after, so the three are the WFTL coordinates and not a bank pointer */
extern int vasistas_read_record(unsigned char wftl_type, unsigned short index,
                                unsigned int offset, void *out);
extern int vasistas_store(const void *rec, unsigned char wftl_type);
extern int vasistas_walk_next(struct vasistas_bank *cursor, void *out);
extern int vasistas_walk_prev(struct vasistas_bank *cursor, void *out);

/* ---------------------------------------------------------- dblib's port */

/* dblib stores its information elements in banks of raw external flash, not
   in WFTL, and reaches them through a cursor rather than an address: every
   read and write is at base + cursor and advances it.

   The fields are read off dblib_bank_write (0x4896c), dblib_bank_read
   (0x489ac) and dblib_bank_erase (0x48940). `medium` at +0x14 is what lets
   the same struct serve the in-RAM copy: dblib_bank_read memcpy()s for 1
   (0x489d4) and goes to the flash for 0. */
struct dblib_bank {
    unsigned int id;            /* +0, the number the log lines print */
    unsigned int base;          /* +4, the byte offset in external flash */
    unsigned int size;          /* +8, the limit both "addr oor" lines check */
    unsigned int erase_len;     /* +0xc, what dblib_bank_erase erases */
    unsigned int cursor;        /* +0x10 */
    unsigned char medium;       /* +0x14, 0 = external flash, 1 = memory */
    unsigned char dirty;        /* +0x15, set by every write and erase */
};

/* functions */
extern void dblib_port_open(void);
extern void dblib_port_close(void);
extern void dblib_bank_erase(struct dblib_bank *bank);
/* len, or 0 when the cursor would pass the bank's size */
extern unsigned int dblib_bank_write(struct dblib_bank *bank, const void *buf,
                                     unsigned int len);
extern unsigned int dblib_bank_read(struct dblib_bank *bank, void *buf,
                                    unsigned int len);
extern void dblib_bank_copy(struct dblib_bank *dst, struct dblib_bank *src);

#endif
