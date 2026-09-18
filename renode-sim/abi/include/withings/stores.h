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

/* The six-bit type selects the payload, and nothing in the record says which
   union arm is live beyond that field: every reader switches on it. Three
   tables agree on the namespace and are the evidence for the arms below --
   vasistas_record_size's tbb at 0x66534 gives the byte count, the shell's
   `vasistas fake2` encoder jump table at 0x65eb8 gives the field packing, and
   the WPP reply builder's tbh at 0x33556 gives the wire objects a type is
   sent as. A type the size table gives 0 is not storable at all. */
enum vasistas_type {
    VASISTAS_TYPE_ACTIVITY_NONE = 0,   /* 8, no payload: head and duration */
    VASISTAS_TYPE_WALK = 1,            /* 28, WamVasistasWalk */
    VASISTAS_TYPE_RUN = 2,             /* 28, WamVasistasRun */
    VASISTAS_TYPE_SLEEP = 8,           /* 28, WamVasistasSleep */
    VASISTAS_TYPE_SWIM = 9,            /* 16, VasistasSwimV1 */
    VASISTAS_TYPE_BKP = 10,            /* 16, vasistas_write_bkp's own */
    VASISTAS_TYPE_HEARTRATE = 12,      /* 16, VasistasHeartrate */
    VASISTAS_TYPE_ACTIVITY_EVENT = 13, /* 12, ActivityLap/Pause/Subcategory */
    VASISTAS_TYPE_HEARTRATE_ALT = 14,  /* 16, the same emitter as 12 */
    VASISTAS_TYPE_UNKNOWN_15 = 15,     /* header size + 16, no emitter */
    VASISTAS_TYPE_UNKNOWN_16 = 16,     /* 32, no emitter */
    VASISTAS_TYPE_SPO2 = 18,           /* 12, VasistasSpo2 */
    VASISTAS_TYPE_UNKNOWN_19 = 19,     /* header size + 8, no emitter */
    VASISTAS_TYPE_AHI = 20,            /* 12, VasistasAhi */
    VASISTAS_TYPE_UNKNOWN_21 = 21,     /* 24, no emitter */
    VASISTAS_TYPE_CBT = 22,            /* 12, VasistasCbt */
    VASISTAS_TYPE_HRV = 23,            /* 16, VasistasHrv */
    VASISTAS_TYPE_RR = 24,             /* 12, VasistasRr */
    VASISTAS_TYPE_UNKNOWN_26 = 26,     /* 28, activity-shaped, no emitter */
    VASISTAS_TYPE_DBT = 27,            /* 20, VasistasDbt */
    VASISTAS_TYPE_SLEEP_SHORT = 37,    /* 12, WamVasistasSleep */
    VASISTAS_TYPE_MARKER = 62          /* 8, below the reply builder's table */
};

/* Every payload is bit-packed from bit 55 of the record, which is where the
   header's own fields stop, so a type struct repeats the header's bits rather
   than nesting it: a field that starts mid-byte cannot be expressed as a
   member after an eight-byte prefix. */
#define VASISTAS_HEADER_BITS \
    unsigned int timestamp;         /* +0, unix seconds */ \
    unsigned int type : 6;          /* bit 32 */ \
    unsigned int prev_size : 8;     /* bits 38..45 */ \
    unsigned int size : 8;          /* bits 46..53 */ \
    unsigned int size_valid : 1     /* bit 54 */

/* 28 bytes, the activity record types 1, 2, 8 and 26; 12-byte type 37 is the
   same packing truncated after `unknown_69`. Encoder cases 0x65f6e and
   0x65fb0; sent as WamVasistasHead, WamVasistasDuration, WamVasistasMetCal
   (or MetCalEarned), WamVasistasAwake, WamVasistasWalk/Run/Sleep and
   VasistasActiRecoV1V2 by the builders at 0x334bc, 0x335d4, 0x3366a and
   0x33634. */
struct vasistas_activity {
    VASISTAS_HEADER_BITS;
    unsigned int steps : 9;         /* 55..63,  WamVasistasAwake.steps */
    unsigned int level : 5;         /* 64..68,  WamVasistasWalk/Run/Sleep */
    unsigned int unknown_69 : 6;    /* 69..74,  encoder only, no reader */
    unsigned int distance : 16;     /* 75..90,  WamVasistasAwake.distance */
    unsigned int : 5;
    /* Two readers take bits 96..109 under complementary conditions: the awake
       emitter reads it as descent when the byte at *(0x20006448 + 5) is zero
       (0x333ae) and the acti-reco emitter reads it as reco_v1 when it is not
       (0x33460), so the name depends on that configuration byte. */
    unsigned int descent : 14;      /* 96..109, or VasistasActiRecoV1V2.v1 */
    unsigned int reco_v2 : 13;      /* 110..122, VasistasActiRecoV1V2.v2 */
    unsigned int : 5;
    unsigned int calories : 13;     /* 128..140, WamVasistasMetCal.calories */
    unsigned int met : 12;          /* 141..152, WamVasistasMetCal.met */
    unsigned int : 7;
    unsigned int ascent : 14;       /* 160..173, WamVasistasAwake.ascent */
    unsigned int merged : 1;        /* 174, read by vasistas_bank_for_record
                                       at 0x66bf4 beside steps and level */
    unsigned int : 17;
    unsigned int : 32;
};

/* 16 bytes, type 9. Encoder case 0x6604c; sent as Version, VasistasSwimType
   and VasistasSwimV1 by the builder at 0x336b4, whose head and duration both
   come from `duration`. */
struct vasistas_swim {
    VASISTAS_HEADER_BITS;
    unsigned int : 9;
    unsigned int duration;          /* 64..95 */
    unsigned int version : 4;       /* 96..99,  Version.value */
    unsigned int swim_type : 4;     /* 100..103, VasistasSwimType.value */
    unsigned int mvt : 16;          /* 104..119, VasistasSwimV1.mvt */
    unsigned int laps : 8;          /* 120..127, VasistasSwimV1.laps */
};

/* 16 bytes, type 10, the record vasistas_write_bkp (0x65c48) builds and
   vasistas_read_last_bkp reads. Encoder case 0x6607a; the reply builder's
   table has no row for it, so no wire object names these fields. */
struct vasistas_bkp {
    VASISTAS_HEADER_BITS;
    unsigned int : 9;
    unsigned int unknown_64 : 18;   /* 64..81 */
    unsigned int unknown_82 : 4;    /* 82..85 */
    unsigned int unknown_86 : 10;   /* 86..95 */
    unsigned int unknown_96 : 10;   /* 96..105 */
    unsigned int unknown_106 : 10;  /* 106..115, packed in two pieces at
                                       0x660e6 and 0x660fa */
    unsigned int unknown_116 : 1;   /* 116 */
    unsigned int unknown_117 : 1;   /* 117 */
    unsigned int : 10;
};

/* 16 bytes, types 12 and 14. Encoder case 0x6613a; sent as VasistasHeartrate
   and VasistasFlags by the builder at 0x3371e, which takes the head and the
   duration from `duration`. */
struct vasistas_heartrate {
    VASISTAS_HEADER_BITS;
    unsigned int heartrate : 8;     /* 55..62, VasistasHeartrate.heartrate */
    unsigned int unknown_63 : 1;    /* 63, encoder only, no reader */
    unsigned int quality : 8;       /* 64..71, VasistasHeartrate.quality */
    unsigned int unknown_72 : 16;   /* 72..87, encoder only, no reader */
    unsigned int : 8;
    unsigned int temperature : 16;  /* 96..111, VasistasHeartrate.temperature */
    unsigned int duration : 8;      /* 112..119 */
    /* VasistasFlags.enabled_flags, one bit each, against a constant
       supported_flags of 0xf; flag_8 is inverted (0x33798 takes the `pl`
       arm, so the wire bit is set when the stored bit is clear). */
    unsigned int flag_1 : 1;        /* 120 */
    unsigned int flag_2 : 1;        /* 121 */
    unsigned int flag_4 : 1;        /* 122 */
    unsigned int flag_8 : 1;        /* 123 */
    unsigned int : 4;
};

/* 12 bytes, type 13. Encoder case 0x6620c; the builder at 0x337b0 matches
   `event` against 1..5 and sends ActivitySubcategory, ActivityLap or
   ActivityPause, keeping the event's timestamp in the globals at 0x20017c50
   and 0x20017c4c so the next event can carry a duration. */
struct vasistas_activity_event {
    VASISTAS_HEADER_BITS;
    unsigned int event : 8;         /* 55..62, 1 start, 2 lap, 3 and 4 pause
                                       or resume, 5 stop */
    unsigned int : 1;
    signed int subcategory : 16;    /* 64..79, ActivitySubcategory.value;
                                       the reader is an ldrsh at 0x33832 */
    unsigned int : 16;
};

/* 12 bytes, type 18. Encoder case 0x66250; sent as VasistasSpo2 by the
   builder at 0x338ee. */
struct vasistas_spo2 {
    VASISTAS_HEADER_BITS;
    unsigned int error : 8;         /* 55..62, VasistasSpo2.error */
    unsigned int : 1;
    unsigned int spo2 : 10;         /* 64..73, VasistasSpo2.spo2 */
    unsigned int quality : 8;       /* 74..81, VasistasSpo2.quality */
    unsigned int duration : 8;      /* 82..89 */
    unsigned int : 6;
};

/* 12 bytes, type 20. Encoder case 0x662c8; sent as VasistasAhi by the builder
   at 0x33936, which copies the whole word at +8 into the object. */
struct vasistas_ahi {
    VASISTAS_HEADER_BITS;
    unsigned int : 9;
    signed short ahi;               /* +8,  VasistasAhi.ahi */
    signed short bd_proba;          /* +10, VasistasAhi.bd_proba */
};

/* 12 bytes, type 22. No fake2 encoder; the fields are the builder at 0x3395c,
   which also refuses to give a duration when attrib is 5. */
struct vasistas_cbt {
    VASISTAS_HEADER_BITS;
    unsigned int algo : 3;          /* 55..57, VasistasCbt.algo */
    unsigned int attrib : 4;        /* 58..61, VasistasCbt.attrib */
    unsigned int : 2;
    signed int temperature : 20;    /* 64..83, VasistasCbt.temperature; the
                                       reader is an sbfx at 0x3399e */
    unsigned int : 12;
};

/* 16 bytes, type 23. No fake2 encoder; the fields are the builder at 0x339ac,
   which takes the head and the duration from `duration`. */
struct vasistas_hrv {
    VASISTAS_HEADER_BITS;
    unsigned int : 1;
    unsigned int hr : 8;            /* 56..63, VasistasHrv.hr */
    unsigned short sdnn;            /* +8,  VasistasHrv.sdnn */
    unsigned short rmssd;           /* +10, VasistasHrv.rmssd */
    unsigned char quality;          /* +12, VasistasHrv.quality */
    unsigned char duration;         /* +13 */
    unsigned char pad[2];
};

/* 12 bytes, type 24. No fake2 encoder; the fields are the builder at 0x339e6,
   which takes the head and the duration from `duration`. */
struct vasistas_rr {
    VASISTAS_HEADER_BITS;
    unsigned int : 9;
    unsigned short rr;              /* +8, VasistasRr.rr */
    unsigned char duration;         /* +10 */
    unsigned char pad;
};

/* 20 bytes, type 27. No fake2 encoder; the fields are the builder at 0x33a0a,
   which takes the head and the duration from `duration`. */
struct vasistas_dbt {
    VASISTAS_HEADER_BITS;
    unsigned int : 9;
    unsigned short duration;        /* +8 */
    unsigned char hr_max_avg;       /* +10 */
    unsigned char hr_min_avg;       /* +11 */
    unsigned char in_period_ds;     /* +12 */
    unsigned char ex_period_ds;     /* +13 */
    unsigned char in_period_target_s; /* +14 */
    unsigned char ex_period_target_s; /* +15 */
    unsigned char duration_target;  /* +16 */
    unsigned char is_from_ecg;      /* +17 */
    unsigned char hr_quality;       /* +18 */
    unsigned char pad;
};

/* Types 15, 16, 19, 21 and 26 are storable (the size table gives them 32, 24,
   28 and two variable lengths) but no reply builder row reads them and only
   16 and 19 have a fake2 case, so their payloads stay undeclared: type 16
   carries a four-bit field at bits 55..58 (0x66236) and type 19 is a fixed
   34-byte blob copied from 0x3f4e4 behind a size the encoder writes itself
   (0x66296). */
union vasistas_record {
    struct vasistas_header header;
    struct vasistas_activity activity;
    struct vasistas_swim swim;
    struct vasistas_bkp bkp;
    struct vasistas_heartrate heartrate;
    struct vasistas_activity_event activity_event;
    struct vasistas_spo2 spo2;
    struct vasistas_ahi ahi;
    struct vasistas_cbt cbt;
    struct vasistas_hrv hrv;
    struct vasistas_rr rr;
    struct vasistas_dbt dbt;
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
