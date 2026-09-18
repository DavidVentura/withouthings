/* HWA10 (ScanWatch 2) application firmware v3411: the wpp module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_WPP_H
#define WITHINGS_WPP_H

struct wpp_ActivitySubcategory;
struct wpp_EndTime;
struct wpp_Id;
struct wpp_ImageMetadata;
struct wpp_MeasureCategory;
struct wpp_NotificationAppId;
struct wpp_StartTime;
struct wpp_StoredSignalMeta;

/* wpp_cmd_table's row +4. The master dispatcher 0x6c784 is called at 0x6c0c2
   as (command, objects, len) out of the frame the header parser 0x50460
   filled -- sp+0x14 is the pointer past the five-byte header and sp+0x10 the
   remaining length -- and at 0x6c7a8..0x6c7ae it moves those two into r0/r1
   and calls the row. It then returns 0 unconditionally, so a handler's
   return value is never read.
   */
typedef void (*wpp_cmd_handler)(const void *objects, unsigned short len);
/* wpp_cmd_table_slave's row +4. The slave dispatcher 0x6e788 takes (command,
   phase, objects, len) and shifts them down by one into the row at
   0x6e7c0..0x6e7cc, a `bx r3` tail call, so the handler's return value is
   the dispatcher's and its callers compare it against 1. phase is a literal
   at every call site: 1 at 0x6e856, 3 at 0x6e948, 4 at 0x6e9cc and a
   register at 0x6e8ec; objects and len are the same frame pair the master
   path uses and are 0/0 on the two sites that carry no payload.
   */
typedef int (*wpp_cmd_slave_handler)(unsigned int phase, const void *objects, unsigned short len);
/* what the frame appender 0x9d322 is handed in r3 and calls as (object,
   &cursor); every wpp_obj_*_encode body opens `mov r4, r1; mov r5, r0` and
   loads its fields off r5
   */
typedef void (*wpp_obj_encoder)(const void *obj, void **cursor);
/* the r2 of the same appender: the object's data length, a constant for a
   fixed-size type (0x98fe4 is `movs r0,#0xe; bx lr`, BatteryStatus's ten
   bytes plus the four-byte header) and a computation off the object for a
   variable one (0x98ff4 is `ldrb r0,[r0]; adds r0,#5`)
   */
typedef int (*wpp_obj_sizer)(const void *obj);
/* the callback wpp_obj_parse (0x504b4) stores at [sp] and calls at 0x504ea
   with r0 = &cursor (the stack slot it has already advanced past the TLV
   header), r1 = the object's data length and r2 = the caller's out pointer;
   a non-zero return is logged as a parse failure and turns into -1
   */
typedef int (*wpp_obj_parser)(void **cursor, unsigned short len, void *out);

/* handle pairs at +4/+8 (WPP_V2), +0xc/+0x10 (WPP_V3), +0x14/+0x18 (WPPS);
   subscribe bits at +0x4c, bit 3 selects the WPPS TLS record path. The tail
   is wpp_bytes_received's own: it memcpys the write into +0x4f and stores
   the length at +0x118 (0x3930c..0x39314), reads +0x4c and +0x4d as two
   bytes and tests bit 3 of their OR (0x39318..0x39324), and when that bit is
   set runs the TLS record path over +0x17c (bytes still owed for the record,
   `cmp r1,#0x54` at 0x39384), +0x180 (bytes of the five-byte record header
   already in hand, compared against 5 at 0x39364) and +0x184 (the record
   buffer, a fixed address stored at 0x39344). +0x11c is the TLS state
   symbols.txt gives: 0 idle, 1 handshaking, 2 established, and
   wpps_established (0x39124) is `ctx[0x11c] == 2`.
   */
struct wpp_service_ctx {
    unsigned int pad0;
    unsigned short wpp_v2_value;
    unsigned short pad1;
    unsigned short wpp_v2_cccd;
    unsigned short pad2;
    unsigned short wpp_v3_value;
    unsigned short pad3;
    unsigned short wpp_v3_cccd;
    unsigned short pad4;
    unsigned short wpps_value;
    unsigned short pad5;
    unsigned short wpps_cccd;
    unsigned char pad6[0x32];
    unsigned int subscribe_bits;
    unsigned char pad7[0xc4];
    unsigned short staged_len;
    unsigned char pad8[0x2];
    unsigned char tls_state;
    unsigned char pad9[0x5f];
    unsigned int record_expected;
    unsigned int record_filled;
    void *record_buf;
};

/* handler has the Thumb bit set and name points at the handler's source
   name; abi/autonames.py walks the table on exactly that shape. The
   dispatcher 0x6c784 loads the row's +0 as a u16 to compare and its +4 as
   the call target, so command is only ever read sixteen bits wide.
   */
struct wpp_cmd_entry {
    unsigned int command;
    wpp_cmd_handler handler;
    const char *name;
};

/* WPP object type 2351 (CalibrationType), 1 bytes; parser 0x4f7e4
   (+0x0@0x4f7ec).
   */
struct wpp_CalibrationType {
    unsigned char type;
};

/* WPP object type 2398 (ImageData), 65 bytes; parser 0x4fb0c (+0x0@0x4fb14). */
struct wpp_ImageData {
    unsigned char data[0x41];
};

/* WPP object type 2463 (MeasureLiveAppStatus), 1 bytes; parser 0x4ffdc
   (+0x0@0x4ffe4).
   */
struct wpp_MeasureLiveAppStatus {
    unsigned char app_live_screen_displayed;
};

/* WPP object type 2405 (NotificationAppDisplayInfo), 67 bytes; parser
   0x4fbc4 (+0x0@0x4fbd0, +0x1@0x4fbd8, +0x2@0x4fbe0).
   */
struct wpp_NotificationAppDisplayInfo {
    unsigned char mode;
    unsigned char display;
    unsigned char fields_order[0x41];
};

/* WPP object type 321 (WorkoutGpsStatus), 2 bytes; parser 0x4f19c
   (+0x0@0x4f1a4).
   */
struct wpp_WorkoutGpsStatus {
    unsigned short status;
};

/* the same three words as wpp_cmd_entry, which is why a walk by shape alone
   reads the two tables as one; the difference is the dispatcher, and so the
   handler's prototype.
   */
struct wpp_cmd_slave_entry {
    unsigned int command;
    wpp_cmd_slave_handler handler;
    const char *name;
};

/* functions */
/* entry the three GATT write demuxes at 0x93654 funnel into */
extern void wpp_bytes_received(const void *data, unsigned int len);
/* WPP command 1284 handler, entry 127 of wpp_cmd_table. Reads the battery
   level, then replies with command 0x504. Ignores its argument. Address
   carries the Thumb bit as it is stored in the table; gen.py masks it off
   and sets it again.
   */
extern void wpp_cmd_battery_status(const void *objects, unsigned short len);
extern void wpp_put_u8(void **cursor, unsigned char value);
extern void wpp_get_u8(unsigned char *out, void **cursor);
extern void wpp_put_be16(void **cursor, unsigned short value);
extern void wpp_get_be16(unsigned short *out, void **cursor);
extern void wpp_put_be32(void **cursor, unsigned int value);
extern void wpp_get_be32(unsigned int *out, void **cursor);
/* the source is length-prefixed and the length is clamped to 0x40 before
   both the count byte and the copy, so a string field on this wire is at
   most 64 bytes and the firmware truncates rather than failing
   */
extern void wpp_put_string(void **cursor, const unsigned char *pascal_string);
extern void wpp_put_array_u8(void **cursor, const unsigned char *data, unsigned char count);
extern void wpp_put_array_be32(void **cursor, const unsigned int *data, unsigned char count);
/* same 0x40 clamp as wpp_put_string, and it logs at level 3 when the wire
   length is above it rather than rejecting the object
   */
extern void wpp_get_string(void **cursor, unsigned char *out);
/* the count byte is clamped to max_count, with a level-3 log when they
   differ; the caller's buffer bound is the only check
   */
extern void wpp_get_array_u8(void **cursor, unsigned char *out, unsigned char max_count);
extern void wpp_get_array_be32(void **cursor, unsigned int *out, unsigned char max_count);
/* the master-request walk: steps wpp_cmd_table by 12 between the pair
   (0x274b4, 0x27ba4) its literal pool holds, logs the row's name when +8 is
   non-null, calls +4 with (objects, len) and returns 0, or -1 when no row
   matches
   */
extern int wpp_cmd_dispatch(unsigned short command, const void *objects, unsigned short len);
/* the same walk over (0x27ba4, 0x27c04) with one more argument; the row is a
   tail call, so the handler's result is this one's
   */
extern int wpp_cmd_dispatch_slave(unsigned short command, unsigned int phase, const void *objects, unsigned short len);
/* the TLV walk every request handler reaches its objects through. Reads the
   {u16 type, u16 length} header at the cursor with 0x9a8fe, skips the object
   when the type does not match, and on a match calls parse with the advanced
   cursor, the object's length and out. -2 when the stream runs out before a
   match ("not found" log at 0x50510), -1 when parse fails.
   */
extern int wpp_obj_parse(void *out, const void *data, unsigned short len, unsigned short type, wpp_obj_parser parse);
/* the two big-endian u16 of an object header, read through the same cursor
   the field primitives use; its only caller is wpp_obj_parse
   */
extern void wpp_obj_header_get(void **cursor, unsigned short *out_type, unsigned short *out_len);
/* builds a 0xb0-byte frame on its own stack (0x9d30e), appends the one
   object through 0x9d322 and hands the frame to the link layer with 0x6bd0c.
   0x9d3d8 is a `b.w` to it and is the entry every reply site in the image
   actually calls.
   */
extern void wpp_send_object(const void *obj, unsigned short command, wpp_obj_sizer size, wpp_obj_encoder encode);
/* the `b.w 0x9d38a` thunk; declared because it is what the handlers call */
extern void wpp_send_object_alias(const void *obj, unsigned short command, wpp_obj_sizer size, wpp_obj_encoder encode);
/* the same frame with an empty object list, i.e. a command and nothing else */
extern void wpp_send_empty(unsigned short command);
/* appends one object to a frame the caller already has; the (sizer, encoder)
   pair in r2/r3 is what abi/protocol.py reads a reply's type from
   */
extern void wpp_frame_append_object(void *frame, const void *obj, wpp_obj_sizer size, wpp_obj_encoder encode);
/* row 257 of wpp_cmd_table, and also the branch the frame demux 0x6bfa8
   takes at 0x6c088 when the command word is 0x101, with the same (objects,
   len) pair it would have given the dispatcher
   */
extern void WPP_CMD_PROBE(const void *objects, unsigned short len);
/* row 341, called directly at 0x6c0aa on the 0x155 branch of the same demux */
extern void WPP_CMD_MTU_EXCH(const void *objects, unsigned short len);
/* row 321 of wpp_cmd_table_slave, so the three-argument prototype; the
   second caller is wpp_lite_client_cmd_dropped (0x6e7e8), which is inside
   the slave walk and passes the same three
   */
extern int WPP_CMD_SYNC_REQUEST(unsigned int phase, const void *objects, unsigned short len);
/* the object walk inlined into 0x6a864 (WPP_CMD_WORKOUT_START_0x6a864),
   0x6a9dc (WPP_CMD_WORKOUT_STOP_0x6a9dc) compares the TLV header's type
   against 2409 before calling it, and its 1 field read agree with
   wpp/src/objects.rs's ActivitySubcategory parse.
   */
extern int wpp_obj_ActivitySubcategory_parse(void **cursor, unsigned short len, struct wpp_ActivitySubcategory *out);
/* the object walk inlined into 0x94b52 (WPP_CMD_CALIBRATION_GET), 0x94bba
   (WPP_CMD_CALIBRATION_SET) compares the TLV header's type against 2351
   before calling it, and its 1 field read agree with wpp/src/objects.rs's
   CalibrationType parse.
   */
extern int wpp_obj_CalibrationType_parse(void **cursor, unsigned short len, struct wpp_CalibrationType *out);
/* the object walk inlined into 0x3d088 (WPP_CMD_SET_MULTI_ALARM), 0x6a9dc
   (WPP_CMD_WORKOUT_STOP_0x6a9dc) compares the TLV header's type against 2419
   before calling it, and its 1 field read agree with wpp/src/objects.rs's
   EndTime parse.
   */
extern int wpp_obj_EndTime_parse(void **cursor, unsigned short len, struct wpp_EndTime *out);
/* the object walk inlined into 0x4c1a4
   (WPP_CMD_FEATURE_TAGS_SET_DEPRECATED_V2), 0x5eaac (WPP_CMD_THRESHOLDS_SET)
   compares the TLV header's type against 325 before calling it, and its 1
   field read agree with wpp/src/objects.rs's Id parse.
   */
extern int wpp_obj_Id_parse(void **cursor, unsigned short len, struct wpp_Id *out);
/* the object walk inlined into 0x560c8 (WPP_CMD_GLYPH_GET), 0x6b224
   (WPP_CMD_WORKOUT_SCREEN_SET), 0x6b6d8 (WPP_CMD_CUSTO_SCREEN_SET) compares
   the TLV header's type against 2398 before calling it, and its 1 field read
   agree with wpp/src/objects.rs's ImageData parse.
   */
extern int wpp_obj_ImageData_parse(void **cursor, unsigned short len, struct wpp_ImageData *out);
/* the object walk inlined into 0x6b224 (WPP_CMD_WORKOUT_SCREEN_SET), 0x6b6d8
   (WPP_CMD_CUSTO_SCREEN_SET) compares the TLV header's type against 2397
   before calling it, and its 3 field reads agree with wpp/src/objects.rs's
   ImageMetadata parse.
   */
extern int wpp_obj_ImageMetadata_parse(void **cursor, unsigned short len, struct wpp_ImageMetadata *out);
/* the object walk inlined into 0x54238 (WPP_CMD_MEASURE_START_0x54238),
   0x54380 (WPP_CMD_MEASURE_STOP_0x54380), 0x54418
   (WPP_CMD_MEASURE_START_0x54418) compares the TLV header's type against
   2427 before calling it, and its 1 field read agree with
   wpp/src/objects.rs's MeasureCategory parse.
   */
extern int wpp_obj_MeasureCategory_parse(void **cursor, unsigned short len, struct wpp_MeasureCategory *out);
/* the object walk inlined into 0x54238 (WPP_CMD_MEASURE_START_0x54238),
   0x54418 (WPP_CMD_MEASURE_START_0x54418) compares the TLV header's type
   against 2463 before calling it, and its 1 field read agree with
   wpp/src/objects.rs's MeasureLiveAppStatus parse.
   */
extern int wpp_obj_MeasureLiveAppStatus_parse(void **cursor, unsigned short len, struct wpp_MeasureLiveAppStatus *out);
/* the object walk inlined into 0x55b8c (NOTIFICATION_APP_ENABLED_SET)
   compares the TLV header's type against 2405 before calling it, and its 3
   field reads agree with wpp/src/objects.rs's NotificationAppDisplayInfo
   parse.
   */
extern int wpp_obj_NotificationAppDisplayInfo_parse(void **cursor, unsigned short len, struct wpp_NotificationAppDisplayInfo *out);
/* the object walk inlined into 0x55b8c (NOTIFICATION_APP_ENABLED_SET)
   compares the TLV header's type against 2404 before calling it, and its 1
   field read agree with wpp/src/objects.rs's NotificationAppId parse.
   */
extern int wpp_obj_NotificationAppId_parse(void **cursor, unsigned short len, struct wpp_NotificationAppId *out);
/* the object walk inlined into 0x3d088 (WPP_CMD_SET_MULTI_ALARM), 0x6a864
   (WPP_CMD_WORKOUT_START_0x6a864), 0x6a9dc (WPP_CMD_WORKOUT_STOP_0x6a9dc)
   compares the TLV header's type against 2418 before calling it, and its 1
   field read agree with wpp/src/objects.rs's StartTime parse.
   */
extern int wpp_obj_StartTime_parse(void **cursor, unsigned short len, struct wpp_StartTime *out);
/* the object walk inlined into 0x6e21c (WPP_CMD_STORED_MEASURE_SIGNAL_GET),
   0x6e3e8 (WPP_CMD_STORED_MEASURE_SIGNAL_DEL) compares the TLV header's type
   against 323 before calling it, and its 6 field reads agree with
   wpp/src/objects.rs's StoredSignalMeta parse.
   */
extern int wpp_obj_StoredSignalMeta_parse(void **cursor, unsigned short len, struct wpp_StoredSignalMeta *out);
/* the object walk inlined into 0x6a730 (WPP_CMD_WORKOUT_GPS_STATUS) compares
   the TLV header's type against 321 before calling it, and its 1 field read
   agree with wpp/src/objects.rs's WorkoutGpsStatus parse.
   */
extern int wpp_obj_WorkoutGpsStatus_parse(void **cursor, unsigned short len, struct wpp_WorkoutGpsStatus *out);

/* globals */
extern struct wpp_service_ctx wpp_service_ctx;
/* the 200-byte reassembly buffer installed at 0x6c16c together with the
   capacity 200 at ctx+64; FIRMWARE.md's frame-length section is the
   evidence, including that the next global begins exactly 200 bytes later
   */
extern unsigned char wpp_rx_frame_buffer[0xc8];

/* tables */
/* master-request dispatch: the commands the phone sends. Runs 0x274b4 to
   0x27ba4, not to 0x27c04: the dispatcher at 0x6c784 and the send path's
   name lookup at 0x6bdbe both load the pair (0x274b4, 0x27ba4) and step by
   12 between them, and the eight rows above 0x27ba4 are walked by a
   different dispatcher (0x6e788) that loads (0x27ba4, 0x27c04) and calls its
   handler with three arguments instead of two. They are declared separately
   as wpp_cmd_table_slave. The rows themselves have the same shape either
   way, which is why a walk by shape alone reads the two as one table of 156;
   an earlier reading started it at 0x27994, which is row 104, not the head.
   abi/autonames.py walks it in both directions from there and stops where
   the row stops being {command id, odd handler in the image, pointer to a
   printable name}; the row above the head belongs to the shell's own table
   (0x27100, 79 rows, a different shape: the command word first and two entry
   points after it). Each row's name string is the handler's source name,
   which is where abi/autonames.yaml's wppcmd class comes from. No
   literal-pool reference to the table exists in the image, so the walker
   reaches it some other way; replacing a handler word in place works
   regardless (proven by diverting the battery handler to a logging hook).
   */
extern struct wpp_cmd_entry wpp_cmd_table[148];
/* The eight rows above wpp_cmd_table, dispatched by 0x6e788 rather than
   0x6c784. Both bounds are its own literal pool pair (0x6e7d4 and 0x6e7d8,
   repeated at 0x6e830/0x6e834), it logs "[WPP][<-]"-style lines with the
   row's name from +8 like the master walk, and it tail-calls the handler at
   +4 as `bx r3` after loading three arguments, so these handlers have a
   different prototype from the master-request ones. In address order the
   commands are SYNC_REQUEST 321, LOCAL_EVENT_NOTIFY 2459, MEASURE_STOP 2420,
   MEASURE_START 2419, NOTIFICATION_GET 2404, GLYPH_GET 2403, WORKOUT_STOP
   318 and WORKOUT_START 317.
   */
extern struct wpp_cmd_slave_entry wpp_cmd_table_slave[8];

#endif
