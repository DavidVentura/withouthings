/* HWA10 (ScanWatch 2) application firmware v3411: the wui module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_WUI_H
#define WITHINGS_WUI_H

/* the head of a WUI view descriptor, which is all of it that is fixed: the
   fifteen views wui_view_table names agree on these five words and then
   differ, some 24 bytes long and some 32, so the whole descriptor is not a
   table and is not declared as one. vtable is what wui_push_now calls
   through (+4 on entering, +0x10 on being covered), name is the string the
   "[WUI] %s %s" and "[WUI] Can't push %s cause %s is active" lines print,
   timeout_ms is 6000 in eleven of them and 60000, 21000 and 15000 in the
   rest, reserved is zero in all fifteen and flags is 0x14 in all fifteen.
   */
struct wui_view {
    const void *vtable;
    const char *name;
    unsigned int timeout_ms;
    unsigned int reserved;
    unsigned int flags;
};

/* bits points at the 1-bit-per-pixel rows in the image itself: 0xbc02c's
   word 0xecbac, which words.yaml had to declare by hand as a pointer before
   the table was known, holds 18 bytes that draw a cross for a 14 x 10 icon.
   */
struct asset_entry {
    unsigned char width;
    unsigned char height;
    unsigned short zero;
    const unsigned char *bits;
    unsigned int frames;
};

/* header is this row's own address plus 8, so it points at kind: the decoder
   is handed the five header fields alone and the row is the thing that owns
   them. blob, params and offsets are three points inside one body, blob
   first and offsets last, with params and offsets at a distance from blob
   that varies per row, so the body's own head is variable length and only
   offsets has a shape the image fixes -- climbing u16 scanline offsets,
   height of them. tag0 is 0x21 in every row and tag1 is 0x21 to 0x24, which
   is the encoding the decoder switches on. reserved is zero in all 74 rows.
   */
struct rre_asset {
    const unsigned char *header;
    const unsigned char *blob;
    unsigned short kind;
    unsigned short width;
    unsigned short height;
    unsigned char tag0;
    unsigned char tag1;
    const unsigned char *offsets;
    const unsigned char *params;
    unsigned int reserved;
};

/* offsets holds last - first + 2 halfwords, one per character plus the end,
   and bits is the packed glyph rows they index. Which of the two pointers is
   which is settled by the distances: every row's offsets pointer plus two
   times its character count is either the next row's offsets pointer or its
   own bits pointer. height is below advance in all six rows.
   */
struct font {
    unsigned short type;
    unsigned short height;
    unsigned short advance;
    unsigned char first;
    unsigned char last;
    const unsigned char *bits;
    const unsigned short *offsets;
    unsigned int flags;
};

/* kern holds last - first + 1 signed bytes, one per character in the range,
   and the ranges' arrays abut inside 0xcadaa..0xcadde. An all-zero row ends
   a list rather than describing a range.
   */
struct font_kern_range {
    unsigned int first;
    unsigned int last;
    const signed char *kern;
};

/* NULL, or the first font_kern_range of the list for this character. */
struct font_kern_ptr {
    const struct font_kern_range *ranges;
};

/* view is the descriptor wui_push takes as its second argument; the
   descriptors are variable length, so they are not a table and are not
   declared here. The last row is {0, NULL}.
   */
struct wui_view_entry {
    unsigned int id;
    const struct wui_view *view;
};

/* functions */
/* the whole of RRE_FONTS. The glyph is a run of big-endian-free 16-bit words
   taken from the font's data block between offset_table[code - first] and
   offset_table[code - first + 1] (0x34e5c), and each word is one filled
   rectangle: bits 0..3 are the x offset, bits 4..7 the y offset, bits 8..11
   the width minus one and bits 12..15 the height minus one
   (0x34e88..0x34ea8), clipped against the current window and handed to the
   blit callback as (x, y, w, h, colour). The switch on the font descriptor's
   first byte has seven arms, which is what the "unsupported font type" line
   it took its derived name from guards.
   */
extern unsigned int rre_font_draw_glyph(int x, int y, int code, int x_offset);
/* the pixel width of one glyph and nothing else: it reads the same
   descriptor rre_font_draw_glyph does, returns the fixed advance at +6
   (divided by three for the proportional arms) or walks the same offset
   table to measure the glyph, and calls nothing. rre_font_draw_glyph calls
   it once before it draws.
   */
extern unsigned int rre_font_glyph_width(int code);

/* globals */
/* one run holding every font's glyph bytes and every kerning range's deltas:
   font_table rows 2 to 5 point their bits at 0xcadda, 0xcd021, 0xcd3a2 and
   0xcdb10 and font_kern_table's rows point at 0xcadaa..0xcadde, all of them
   byte offsets and most of them odd. Declared as one byte array because that
   is what the run is -- left as a gap the analysis splits it at each of
   those pointers and the pieces lose the unaligned reference that proves the
   whole thing is byte indexed.
   */
extern unsigned char font_glyph_pool[20058];
/* the encoded bodies rre_asset_table's rows point into, the first of three
   pools. 203 of the table's 222 body pointers land in this one run, the
   lowest at 0xc43c8 and the highest at 0xc5f8c, and the run is one
   uninterrupted stretch the partition never typed. Declared as bytes because
   the pointers into it are byte offsets, not word slots: a word window over
   it reads two halves of two encoded fields.
   */
extern unsigned char rre_asset_bodies_a[9124];
/* the second body pool, holding the bodies of two rre_asset_table rows
   (0xee617 and 0xee76a); it starts where asset_table's own bitmaps and the
   strings after them end.
   */
extern unsigned char rre_asset_bodies_b[912];
/* the third body pool, seventeen rre_asset_table bodies from 0xeea07 to
   0xef784. Left undeclared the analysis follows the odd pointers into it and
   disassembles five of the bodies as functions.
   */
extern unsigned char rre_asset_bodies_c[4771];
/* the pool of NUL-terminated byte sequences the WUI view stack takes as the
   third argument of wui_push. Sixteen of them are named by fifty literal
   pool words at two-byte spacing, which is what a compiler's merge of byte
   array literals looks like and what made every one of them read as a
   number: the run is byte addressed, not a table of words. wui_push_now
   passes the sequence to the stack's own vtable entry at +0x24, and wui_push
   reads byte 1 of it (0x80000) to decide whether a transition runs, so a
   one-byte sequence is "no argument" and the longer ones (0xe6b4a is
   thirteen bytes) are lists. The run ends where the string "on_background"
   begins.
   */
extern unsigned char wui_push_args[40];

/* tables */
/* The image assets the UI draws. 70 rows of {u8 width, u8 height, u16 zero,
   const void *bits, u32 frames}, 0xbbf50..0xbc298, whose bits run
   0xeba84..0xee6xx in internal flash -- so the app does carry bitmaps, which
   is what the absence of a grid over this run hid. The extent is the run
   itself: below 0xbbf50 the bytes are an array of 0x64, at 0xbc298 a
   different object starts whose first word points back into itself, and 40
   literal-pool words name rows of the table and nothing between them. width
   is settled by the drawing routine 0x84878, whose first instruction is
   `ldrb r5,[r2,#0]` followed by `rsb r5,r5,#144`, halved and added to the x
   argument: the panel is 144 wide and the asset is centred on it. frames is
   settled by the offsets: where the asset is uncompressed the distance to
   the next row's offset is exactly width * height * frames / 8 (row 0 is 44
   x 58, four frames, 1276 bytes). Without the grid the pool words naming
   rows read as numbers and the 70 bits pointers read as numbers too, so a
   layout that moved the bitmaps would have left every one of them stale.
   */
extern struct asset_entry asset_table[70];
/* the second image family, the one asset_table is not: 74 rows of 28 bytes
   running 0xbc298 to 0xbcab0, starting at the byte asset_table ends on. The
   extent is the table's own invariant -- every row's +0 holds that row's
   address plus 8, so the row carries an interior pointer to its own header
   -- and it holds for exactly 74 rows and fails on the 75th word (0xefa5d),
   which is what fixes the count without a single literal-pool word naming
   the head. The bodies live in 0xc43c8..0xc5f8c, a region disjoint from
   asset_table's 0xeba84..0xee6xx bitmaps. width and height are read the same
   way asset_table's were: row 8 is 0x94 by 0x2e and the panel is 144 (0x90)
   wide plus its margins, and the +0x14 pointer is an array of strictly
   climbing u16 offsets with height entries, one per scanline, after which
   the encoded bytes begin. Twenty-four functions of the WUI drawing path
   load rows of it.
   */
extern struct rre_asset rre_asset_table[74];
/* the six fonts, named by twenty-two literal pool words that load a row each
   and by nothing that names the head. The count is the shape's own: the
   seventh row would have type 58480 and a first character of zero. The rows
   are checked against each other through their offset tables, which are
   consecutive -- row 2 covers 0x20..0xff so its table is 225 u16s at 0xbe00e
   and ends exactly on row 3's 0xbe1d0, row 3 covers 0x27..0x68 so its 67
   entries end on row 4's 0xbe256, row 4's 32 end on row 5's 0xbe296 and row
   5's 225 end at 0xbe458, which is where ble_conn_params_table begins. Rows
   0 and 1 carry their bitmaps immediately after their own offset table
   instead (0xbdba4 plus 182 is 0xbdc5a), which is the same arithmetic from
   the other side. type is the number "unsupported font type (%i)" at 0xcad90
   prints.
   */
extern struct font font_table[6];
/* the kerning lists font_kern_index points into: a run of character ranges
   each with a signed per-character delta array, with an all-zero row ending
   each list. The layout is proved by the arrays' own lengths -- the row
   {0x61, 0x6f} points at 0xcadc0 and the fifteen bytes there run out exactly
   where the row {0x25, 0x39}'s twenty-one bytes at 0xcadab begin, and that
   row's in turn end on 0xcadc0. The table stops at 0xb2064, where font_table
   starts.
   */
extern struct font_kern_range font_kern_table[17];
/* one pointer per character from 0x30 to 0x59, which is the pair of bounds
   the two words above the table hold (0xb1ee8 is 0x30 and 0xb1eec is 0x59),
   and 0x59 - 0x30 + 1 is 42, which lands the last slot exactly on
   font_kern_table's head. Thirty-three of the slots are NULL and the nine
   that are not all name a font_kern_table row. Half of it was already typed
   as ten pointers and the other half was the untyped run 0xb1f18, which is
   what split one object into two.
   */
extern struct font_kern_ptr font_kern_index[42];
/* the WUI view registry, {id, descriptor} by id, with a zero row last. Both
   the address and the count are the two words at 0xba068, which hold 0xba070
   and 16; the fifteen live rows point at the view descriptors in
   0xb9238..0xb9e68 that wui_push takes as its second argument.
   */
extern struct wui_view_entry wui_view_table[16];

#endif
