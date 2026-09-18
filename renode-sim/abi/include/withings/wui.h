/* HWA10 (ScanWatch 2) application firmware v3411: the wui module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_WUI_H
#define WITHINGS_WUI_H

/* the head of a WUI view descriptor, which is all of it that is fixed. The
   fifteen wui_view_table names are a sixth of them: 126 descriptors run
   0xb91cc..0xb9fcc and a 127th is at 0xba048, each starting with a pointer to
   one of 105 vtables whose first three words are Thumb function starts, and
   125 of the 126 carry a name string. They agree on these five words and then
   differ -- 56 are 20 bytes and the rest run to 124 -- so the whole descriptor
   is not a table and is not declared as one. vtable is what wui_push_now calls
   through (+4 on entering, +0x10 on being covered), name is the string the
   "[WUI] %s %s" and "[WUI] Can't push %s cause %s is active" lines print.
   timeout_ms, reserved and flags are one call and not three fields: the
   default on_enter (0x82310) does `ldr r0,[r0,#8]` and `ldrd r1,r2,[r0,#12]`
   and hands all three to the timeout timer at 0x6f04c, which is why reserved
   is 0x3e8 on "Notif" and 0x384 on "Shortcut" and zero elsewhere, and why
   flags is 0x14 in all but the six carousel wrappers, where it is a RAM
   address.
   */
struct wui_view {
    const struct wui_view_vtable *vtable;
    const char *name;
    unsigned int timeout_ms;
    unsigned int reserved;
    unsigned int flags;
};

/* What wui_push_now, wui_replace, the pop path and the frame tick call through,
   named by the firmware's own words: the four lifecycle slots each log
   "[WUI] %s %s" with a role string and the view's name, and those strings
   (0xe6c09 "on_enter", 0xe6c4a "on_exit", 0xe6b94 "on_foreground", 0xe6b62
   "on_background") are what the slot is called, not a reading of it. The
   shared defaults are the same four lines and nothing else: 0x82310 and
   0x7ff44 print on_enter, 0x82658 and 0x8101c on_exit, 0x80b68 and 0x8105c
   on_foreground, 0x7fe30 and 0x80f88 on_background.

   The table is not this long in every view. All 105 distinct vtables the
   descriptors point at hold these six slots; 70 hold a seventh and 53 an
   eighth, and the longest holds sixteen. Only the six below have a
   caller that fixes what they are, so only the six are declared: +0x18 (the
   default 0xa5be8 is `return 0`, 37 of 70) and +0x1c (0xa5bec, 25 of 53) are
   dispatched from 0x80f7a, 0x8013c, 0x80cea, 0xa580e and 0xa59c6 without any
   of those sites saying what they are for, and above +0x20 the slot count
   itself is per-view.
   */
struct wui_view_vtable {
    /* The view is offered an event and says whether it took it. 0xa55de walks
       the stack with this: it calls +0 on the top view, returns 1 if the call
       returned non-zero, and otherwise pops and offers event 2 to whatever
       came up. 0x80fc8 offers the event to a container's child before the
       container looks at it itself. A 60 s display run sees the codes 0xd,
       0x10 and 1 arrive here on "Menu carousel", and the 1 is followed
       immediately by on_exit. */
    int (*on_event)(struct wui_view *view, int event);
    /* The view has become the top of the stack. wui_push_now tail-calls it at
       0x7ffe0 after storing the view, wui_replace at 0x80252 after taking the
       old one off, and 0xa5712 when the root is set. The default (0x82310)
       starts the view's own timeout from timeout_ms, reserved and flags --
       `ldrd r1,r2,[r0,#12]` with `ldr r0,[r0,#8]` into 0x6f04c -- so those
       three words are one timer call and not three unrelated fields. */
    void (*on_enter)(struct wui_view *view);
    /* The view is coming off the stack. 0xa55b2 calls it from the pop before
       the count is decremented, 0x8023a from the replace before the new view
       is stored, 0xa5708 when the root is replaced. The default (0x82658)
       cancels the timeout with 0x6f04c(0, 0, 0). */
    void (*on_exit)(struct wui_view *view);
    /* The view above this one went away and this one is on top again: the tail
       call of the pop at 0xa55da, taken only when the pop was not told to stay
       quiet. A container forwards it to its child (0x81082). */
    void (*on_foreground)(struct wui_view *view);
    /* A view is about to be pushed on top of this one: wui_push_now calls it
       at 0x7ffae on the old top before the new one is stored. A container
       forwards it to its child (0x80fa4). */
    void (*on_background)(struct wui_view *view);
    /* The frame. 0x806d4 calls it on the top of the stack once per display
       frame -- 52 of them a second, 19.2 ms apart, through the whole of a 60 s
       run -- with both arguments zero while nothing is moving, and during a
       transition it is called twice, once on the incoming view and once on the
       view at 0x20021ba8+0x28 that is sliding out (0x80736 and 0x80742), with
       the pixel offset each is to be drawn at. The second argument is zero at
       every one of those three sites. It is not `draw` as a leaf view would
       mean it: 0x81002 calls the same slot on a carousel with a view pointer,
       which is that class's way of saying which child to show, so the slot is
       named for when it is called and not for one class's reading of the
       argument. */
    void (*refresh)(struct wui_view *view, int offset, int zero);
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

/* The WUI view descriptors, named by abi/wui_views.py out of the name string
   each one carries. Only the twenty-byte head is declared: the tail is
   per-view -- 56 of the 126 descriptors are twenty bytes and the rest run to
   124 -- and no two views agree on it, so a declaration past the head would
   be a claim about one view written as if it held for all of them. */
extern const struct wui_view wui_view_factory_test_screen;
extern const struct wui_view wui_view_factory_test_temperature;
extern const struct wui_view wui_view_factory_test;
extern const struct wui_view wui_view_cycletrackingsymptomsmenu;
extern const struct wui_view wui_view_cycletrackingmenu;
extern const struct wui_view wui_view_cycle;
extern const struct wui_view wui_view_hands_calibration;
extern const struct wui_view wui_view_spo2_error;
extern const struct wui_view wui_view_spo2_meas;
extern const struct wui_view wui_view_spo2_meas_2;
extern const struct wui_view wui_view_spo2;
extern const struct wui_view wui_view_demo_exit;
extern const struct wui_view wui_view_demo_menu;
extern const struct wui_view wui_view_fake_notif_3;
extern const struct wui_view wui_view_fake_notif_1;
extern const struct wui_view wui_view_ecg_fake;
extern const struct wui_view wui_view_hr_demo;
extern const struct wui_view wui_view_notif;
extern const struct wui_view wui_view_timer;
extern const struct wui_view wui_view_timer_menu;
extern const struct wui_view wui_view_stopwatch;
extern const struct wui_view wui_view_stopwatch_menu;
extern const struct wui_view wui_view_alarm;
extern const struct wui_view wui_view_clock_menu;
extern const struct wui_view wui_view_missing_medical_permissions;
extern const struct wui_view wui_view_missing_medical_permissions_2;
extern const struct wui_view wui_view_hold_the_watch;
extern const struct wui_view wui_view_ecg_selection;
extern const struct wui_view wui_view_ecg_result;
extern const struct wui_view wui_view_ecg_live_app;
extern const struct wui_view wui_view_ecg_meas;
extern const struct wui_view wui_view_screen_update;
extern const struct wui_view wui_view_hands_calibration_install;
extern const struct wui_view wui_view_install_nok;
extern const struct wui_view wui_view_install_ok;
extern const struct wui_view wui_view_install_connected;
extern const struct wui_view wui_view_install_bt_key;
extern const struct wui_view wui_view_install_go;
extern const struct wui_view wui_view_tighten_the_watch;
extern const struct wui_view wui_view_workout_low_batt;
extern const struct wui_view wui_view_sport_notif;
extern const struct wui_view wui_view_pause_selection;
extern const struct wui_view wui_view_sport_pause;
extern const struct wui_view wui_view_workout_temperature;
extern const struct wui_view wui_view_sport_congrats;
extern const struct wui_view wui_view_sport_elevation;
extern const struct wui_view wui_view_sport_gps_speed;
extern const struct wui_view wui_view_sport_gps_pace_summary;
extern const struct wui_view wui_view_sport_gps_pace;
extern const struct wui_view wui_view_sport_gps_distance;
extern const struct wui_view wui_view_sport_time;
extern const struct wui_view wui_view_sport_chrono_summary;
extern const struct wui_view wui_view_sport_chrono;
extern const struct wui_view wui_view_sport_heart_rate;
extern const struct wui_view wui_view_sport_calories;
extern const struct wui_view wui_view_workout_2;
extern const struct wui_view wui_view_workout;
extern const struct wui_view wui_view_charge_station;
extern const struct wui_view wui_view_battery_charging;
extern const struct wui_view wui_view_low_battery_screen;
extern const struct wui_view wui_view_power_reserve_screen;
extern const struct wui_view wui_view_factory_charging;
extern const struct wui_view wui_view_lapping_test;
extern const struct wui_view wui_view_temperature_sensors;
extern const struct wui_view wui_view_erase_cache;
extern const struct wui_view wui_view_factory_reset_coutdown;
extern const struct wui_view wui_view_factory_reset;
extern const struct wui_view wui_view_factory_check;
extern const struct wui_view wui_view_info;
extern const struct wui_view wui_view_certif_japan;
extern const struct wui_view wui_view_certif;
extern const struct wui_view wui_view_version;
extern const struct wui_view wui_view_goal;
extern const struct wui_view wui_view_alarm_popup;
extern const struct wui_view wui_view_alarm_ringing;
extern const struct wui_view wui_view_activity_reminder;
extern const struct wui_view wui_view_ppg_afib;
extern const struct wui_view wui_view_high_hr_popup;
extern const struct wui_view wui_view_low_hr_popup;
extern const struct wui_view wui_view_nok;
extern const struct wui_view wui_view_ok;
extern const struct wui_view wui_view_back;
extern const struct wui_view wui_view_shortcut_pause;
extern const struct wui_view wui_view_shortcut;
extern const struct wui_view wui_view_hands_calibration_settings;
extern const struct wui_view wui_view_hands_calibration_tuto;
extern const struct wui_view wui_view_hands_calib_menu;
extern const struct wui_view wui_view_clock_mode;
extern const struct wui_view wui_view_quicklook_selection;
extern const struct wui_view wui_view_quick_look;
extern const struct wui_view wui_view_home_face_settings;
extern const struct wui_view wui_view_dnd_selection;
extern const struct wui_view wui_view_dnd_settings;
extern const struct wui_view wui_view_battery;
extern const struct wui_view wui_view_settings;
extern const struct wui_view wui_view_body_temperature;
extern const struct wui_view wui_view_ecg_hr;
extern const struct wui_view wui_view_spo2_hr;
extern const struct wui_view wui_view_dbt_result;
extern const struct wui_view wui_view_dbt_meas;
extern const struct wui_view wui_view_dbt_duration;
extern const struct wui_view wui_view_dbt_mode;
extern const struct wui_view wui_view_dbt;
extern const struct wui_view wui_view_sleep_duration;
extern const struct wui_view wui_view_custo_2;
extern const struct wui_view wui_view_custo_1;
extern const struct wui_view wui_view_hr;
extern const struct wui_view wui_view_elevation;
extern const struct wui_view wui_view_ecg;
extern const struct wui_view wui_view_distance;
extern const struct wui_view wui_view_steps;
extern const struct wui_view wui_view_calories;
extern const struct wui_view wui_view_home;
extern const struct wui_view wui_view_carousel_clocks;
extern const struct wui_view wui_view_carousel_clocks_2;
extern const struct wui_view wui_view_demo_carousel;
extern const struct wui_view wui_view_demo_carousel_2;
extern const struct wui_view wui_view_hidden_screens;
extern const struct wui_view wui_view_hidden_screens_2;
extern const struct wui_view wui_view_sport_summary;
extern const struct wui_view wui_view_sport_summary_2;
extern const struct wui_view wui_view_sport;
extern const struct wui_view wui_view_sport_2;
extern const struct wui_view wui_view_setup_flow;
extern const struct wui_view wui_view_setup_flow_2;
extern const struct wui_view wui_view_menu_carousel;

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
