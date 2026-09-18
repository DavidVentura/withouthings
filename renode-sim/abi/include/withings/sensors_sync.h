/* HWA10 (ScanWatch 2) application firmware v3411: the sensors_sync module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_SENSORS_SYNC_H
#define WITHINGS_SENSORS_SYNC_H

/* the batch hook sensors_sync_drain calls at 0x59636 once per contiguous run
   of pending slots, with r0 the first slot and r1 the run length; the
   parameter is left void * because the typedef is emitted before struct
   sensors_sync_slot.
   */
typedef void (*sensors_sync_flush_fn)(const void *slots, unsigned int count);

/* the constant description of one of the two streams that share a
   sensor-sync slot; two of them, 0xb41cc {"PPG_CHANS", 0xc, 0x40, 0} and
   0xb41dc {"ACC", 0, 0xc, 5}, reached through the two-entry pointer table at
   0xb41c4 that sensors_sync_ring_push indexes by sensor id. +0 is the %s of
   "[SENSORS_SYNC][%s] Sample lost !!!" (loaded at 0x59766) and of "Nothing
   to pop" (0x596c8). +4 and +8 are the byte range the stream owns inside the
   slot: the push adds +4 to the slot base and passes +8 as the memcpy length
   (0x597a6, 0x597ae), and the consumer zeroes exactly that range for a
   stream that is not subscribed with the same pair loaded as one `ldrd`
   (0x595f0, 0x59606). +0xc is read once, only as `5 - x` into the ring's
   drop counter (0x598e2, 0x598f2), which is not enough to name it.
   */
struct sensors_sync_stream {
    const char *name;
    unsigned int offset;
    unsigned int size;
    unsigned char field_c;
    unsigned char pad_d[0x3];
};

/* the per-stream state of the shared ring, two of them at 0x20007124 (sensor
   id 0) and 0x20007118 (id 1), 0xc bytes apart. +0 is the stream description
   the log line and the memcpy length come from. +4 is the write index,
   bumped and wrapped at 0x80 after each push (0x597ba..0x597c6), and +6 the
   fill count bumped with it (0x597c8), decremented once per popped slot
   (0x5967c, 0x59688) and cleared together with +4 by the single `str` at
   0x598ac, which is what fixes them as two adjacent u16. +8 is a reference
   count: sensors_sync_ref_acquire increments it, sensors_sync_ref_release
   decrements it and refuses at zero, and both the push and the pop treat
   non-zero as "this stream is running". +9 is a drop countdown -- the push
   returns without storing while it is non-zero and decrements it (0x59746),
   and sensors_sync_restart is the only thing that loads it, from the
   stream's +0xc.
   */
struct sensors_sync_ring {
    const struct sensors_sync_stream *stream;
    unsigned short wr;
    unsigned short fill;
    unsigned char refs;
    unsigned char drop;
    unsigned char pad_a[0x2];
};

/* the 0xc bytes the "ACC" stream owns at offset 0 of a slot. The builder
   0x3bd10 walks the driver's 6-byte-per-entry sample array and writes the
   three axes to sp+0xc/0xe/0x10 (0x3bd6a, 0x3bd76, 0x3bd82), a literal zero
   to sp+0x12 (0x3bd92) and the sum of the three absolute values to sp+0x14
   (0x3bd86..0x3bd8e), then hands sp+0xc to sensors_sync_ring_push as sensor
   0, and the stream's size says 0xc bytes travel. Both readers take the axes
   as signed halfwords at +0/+2/+4 (0x5f5e2..0x5f5ea, 0x5f618..0x5f620) and
   both copy the 6 bytes as one word plus one halfword (0x5f636..0x5f642,
   0x3141e at 0x3143c..0x31440), which is a 3 x i16 struct assignment.
   Nothing reads +6 or +8.
   */
struct sensor_sample_accel {
    short x;
    short y;
    short z;
    unsigned short reserved_6;
    int abs_sum;
};

/* the 0x40 bytes the "PPG_CHANS" stream owns at offset 0xc of a slot: one
   word per MAX86173 channel, indexed by channel id -- 0x9ab06 switches on
   the id (`cmp r0,#0xf`) and hands back the address of the word (0x52410),
   and the expander 0x3141c reads all sixteen. Each word is a bit field,
   written by 0x523cc and read by 0x3141c at the same positions: bits 0..19
   are the 20-bit FIFO datum, `bfi r2,r1,#0,#20` at 0x5242a against `sbfx
   r3,r3,#0,#0x14` at 0x31442; bits 20..28 and 29..30 are two values the
   producer packs into the upper halfword under the mask 0xffff800f
   (0x52466..0x5247e) and the consumer unpacks with `ubfx r0,r0,#4,#9` and
   `ubfx r1,r1,#5,#2` (0x3144a, 0x31446) before turning the pair into one
   float through 0x28758; bit 31 is a flag stored as a byte by the consumer
   (0x3145e). The producer takes bits 20..28 and 31 from the per-channel
   configuration at 0x2001af9c through 0x51bcc and 0x51bf4, which is not
   enough to name any of the three.
   */
struct sensor_sample_ppg {
    unsigned int chan[0x10];
};

/* one entry of the 128-slot ring at 0x2001d2d8; the stride 0x4c is the
   literal every walk of the buffer multiplies by (0x597a8 in the push,
   0x595ce and 0x59664 in the consumer, 0x30ee0 in the raw-data flush) and it
   is the sum of the two streams' offset and size.
   */
struct sensors_sync_slot {
    struct sensor_sample_accel acc;
    struct sensor_sample_ppg ppg;
};

/* one MAX86173 channel as the algorithms see it, unpacked from the channel
   word of sensor_sample_ppg by sensors_sync_slot_to_algo: the sign-extended
   20-bit datum (`sbfx #0,#0x14`), the float 0x28758 makes out of the word's
   9-bit and 2-bit fields, and bit 31 as a byte. Sixteen unrolled copies of
   the same three stores, 0xc apart, fix the record (0x3144e..0x31464 is the
   first, 0x3169a..0x316aa the last).
   */
struct algo_ppg_chan {
    int counts;
    float scale;
    unsigned char flag;
    unsigned char pad_9[0x3];
};

/* what one sensors_sync slot becomes on its way to the algorithms, 0xec
   bytes -- sensors_sync_slot_to_algo zeroes it from +6 for 0xe6 (0x31422)
   and the accelerometer driver's own feed builds the same frame the same way
   (0x5f62a). The head is the three accelerometer axes, copied as one word
   plus one halfword, and then a three-slot selection the sixteen channel
   records are folded into: 0xa4418 copies channel 3 to slot 0, channel 13 to
   slot 1 and channel 6 to slot 2, each as its counts, its scale and its flag
   (0xa4418..0xa444a), and that is what fixes the split between the i32 array
   at +8 and the three scale/flag pairs at +0x14, +0x1c and +0x24. +0x2c is
   the sixteen algo_ppg_chan records, which ends the frame exactly at 0xec.
   */
struct algo_sample_frame {
    short x;
    short y;
    short z;
    unsigned short pad_6;
    int sel_counts[0x3];
    float sel_scale_0;
    unsigned char sel_flag_0;
    unsigned char pad_19[0x3];
    float sel_scale_1;
    unsigned char sel_flag_1;
    unsigned char pad_21[0x3];
    float sel_scale_2;
    unsigned char sel_flag_2;
    unsigned char pad_29[0x3];
    struct algo_ppg_chan chan[0x10];
};

/* kind is 2 in all ten rows and group is 0, 1 or 2; suffix is the empty
   string at 0xe15d5 in all ten, which is why it reads as a number rather
   than as a string.
   */
struct ppg_mode {
    unsigned char group;
    unsigned char kind;
    unsigned short pad;
    const char *suffix;
    const char *name;
    unsigned int index;
};

/* functions */
/* one sample into one of the two sensor rings. The body indexes a two-entry
   table of ring descriptors by the first argument and refuses anything above
   one (0x5971e), memcpys the sample into slot (write index * 0x4c) of the
   ring's buffer (0x597a0), advances the write index modulo 128 (0x597ac) and
   increments the fill count; when the reader's index has caught up it logs
   an overrun and drops the oldest slot instead. A ring with a write index, a
   wrap and a fill count is a push and nothing else.
   */
extern int sensors_sync_ring_push(unsigned int sensor, const void *sample, unsigned int arg2, unsigned int arg3);
/* increments the byte at +8 of the same ring descriptor under the same lock
   sensors_sync_ref_release decrements it under, having refused a sensor
   index above one. The logline name it takes over is the panic path for a
   bad index.
   */
extern int sensors_sync_ref_acquire(unsigned int sensor);
/* decrements the byte at +8 sensors_sync_ref_acquire increments, refusing at
   zero with an error rather than wrapping (0x59840); an increment and a
   decrement over one byte with an underflow refusal is a reference count.
   Its caller outside the module is adxl367_continuous_measure_mode.
   */
extern int sensors_sync_ref_release(unsigned int sensor);
/* `xQueueSemaphoreTake(sensors_sync_mutex, 0xffffffff)` and nothing else;
   the handle is the one 0x59700 created and the only other user is
   sensors_sync_unlock.
   */
extern int sensors_sync_lock(void);
/* `xQueueGenericSend(sensors_sync_mutex, 0, 0, 0)`, the give that pairs with
   every sensors_sync_lock in the module.
   */
extern int sensors_sync_unlock(void);
/* the only writer of sensors_sync_mutex: xQueueCreateMutexStatic over the
   static storage at 0x2001d280, called from INIT at 0x2e4a2 just before the
   flush callback is installed.
   */
extern void sensors_sync_mutex_init(void);
/* a single store into sensors_sync_flush_cb; the word it writes is only ever
   read by the `blx` in sensors_sync_drain, so the argument is that callback
   and nothing else.
   */
extern void sensors_sync_set_flush_cb(sensors_sync_flush_fn cb);
/* the consumer side of the ring, and the reason the two streams are one. It
   takes the lock, reads each subscribed ring's fill count and keeps the
   smaller of the two (0x595a2), so the slower stream sets the pace; over
   that many slots from sensors_sync_rd it zeroes, in each slot, the byte
   range of any stream that is not subscribed or has nothing pending
   (0x595ec, 0x59602, using the stream's offset and size as one `ldrd`) and
   hands each contiguous run to sensors_sync_flush_cb (0x59636); it drains
   sensors_sync_exception_flags into 0x31410 (0x5964c); then it pops one slot
   at a time -- decrementing each subscribed ring's fill count, logging
   "Nothing to pop" if one is already empty, memcpying the 0x4c bytes to the
   stack and advancing sensors_sync_rd modulo 128 -- and hands each to
   sensors_sync_slot_to_algo (0x596b6). The derived name it takes over is one
   of its two error logs.
   */
extern void sensors_sync_drain(void);
/* drains what is pending (0x5989a) and then, under the lock, zeroes
   sensors_sync_rd and both rings' write index and fill count with one word
   store each (0x598a6..0x598b0), and sets bit 0 of
   sensors_sync_exception_flags. Clearing every cursor of both rings and the
   shared read index is a reset and leaves no other reading.
   */
extern void sensors_sync_reset(void);
/* the only writer of either ring's drop countdown: for each subscribed ring
   it stores `5 - ring->stream->field_c` into +9 (0x598e2, 0x598f2), so the
   accelerometer drops nothing and the PPG drops five samples, then restarts
   the sampling clock. It touches no other field.
   */
extern void sensors_sync_restart(void);
/* the only reader of a whole slot, and the only thing that gives the bit
   fields of sensor_sample_ppg a meaning. It zeroes a 0xec-byte frame, copies
   the three accelerometer axes into its head as one word plus one halfword
   (0x3143c..0x31440), and for each of the sixteen channel words emits a
   0xc-byte record -- the sign-extended 20-bit datum, the float 0x28758 makes
   out of the 9-bit and 2-bit fields, and bit 31 as a byte -- then hands the
   frame to 0x64214. Sixteen identical unrolled expansions of one struct
   field is what names it; the derived name it takes over came from the
   caller's log line.
   */
extern void sensors_sync_slot_to_algo(const struct sensors_sync_slot *slot);
/* builds a sensor_sample_accel and pushes it as sensor 0. It walks the
   driver's sample array six bytes at a time (0x3bdc0), writes the three axes
   with one of them negated, the literal zero and the sum of the three
   absolute values into a 0xc-byte stack struct, and calls
   sensors_sync_ring_push with r0 = 0 (0x3bda2). Three i16 reads filling the
   accel sample is the whole body.
   */
extern void sensors_sync_push_accel(void);
/* builds a sensor_sample_ppg and pushes it as sensor 1. It memsets 0x40
   bytes (0x523e2), walks the measurement's channel list, asks 0x9ab06 for
   that channel's word inside the sample and packs the caller's 20-bit datum
   and the channel configuration's two upper fields into it, then calls
   sensors_sync_ring_push with r0 = 1 (0x523f6). The 0x40-byte buffer and the
   channel-indexed slot are the PPG stream's size and layout.
   */
extern void sensors_sync_push_ppg(void);
/* a `tbb` switch on the channel id, refusing above 0xf, whose every arm
   returns the address of that channel's word in the caller's PPG sample; it
   is what fixes sensor_sample_ppg as sixteen words indexed by channel id.
   */
extern int ppg_sample_channel_slot(unsigned int channel, struct sensor_sample_ppg *sample, unsigned int **slot);
/* the call algo_dispatch_sample makes under algo_enabled(9) (0x7d502,
   0x7d51a); id 9 is WORN in algo_registry. 1279 calls in the HR trace, one
   per popped slot.
   */
extern void worn_algo_step(void *algo, const struct algo_sample_frame *frame, unsigned int flag);
/* the call under algo_enabled(8) (0x7d51e, 0x7d546); id 8 is
   MOTION_DETECTION. Its argument is a four-byte copy of the frame's axes
   built on the caller's stack, not the frame itself.
   */
extern void motion_detection_algo_step(void *algo, const void *axes);
/* the call under algo_enabled(0xc) (0x7d54a, 0x7d55a); id 12 is
   PPG_HEART_BEATS.
   */
extern void ppg_heart_beats_algo_step(void *algo, const struct algo_sample_frame *frame);
/* the call under algo_enabled(7) (0x7d55e, 0x7d584); id 7 is PPG_ARRHYTHMIA.
   Five values are read out of the HR algorithm's object and passed alongside
   the frame (0x7d566..0x7d582).
   */
extern void ppg_arrhythmia_algo_step(void *algo, float a, float b, float c, float d, const struct algo_sample_frame *frame);
/* the call under algo_enabled(0x11) (0x7d5bc, 0x7d5cc); id 17 is PPG_HRV. It
   is the root of the beat detector: its only callee chain in the trace is
   0x7d778 and ppg_hrv_peak_detect, 2262 and 9016 calls against 1131 samples.
   */
extern void ppg_hrv_algo_step(void *algo, const struct algo_sample_frame *frame);
/* the call under the second algo_enabled(5) (0x7d5f6, 0x7d608); id 5 is
   SPO2_MULTI. The block it sits in is entered for id 5 or, when 5 is off,
   for id 6 (0x7d650), but this call is re-gated on 5 alone.
   */
extern void spo2_multi_algo_step(void *algo, const struct algo_sample_frame *frame, int state, unsigned int first);
/* the call under algo_enabled(6) (0x7d60c, 0x7d63e); id 6 is SPO2_MONO. */
extern void spo2_mono_algo_step(void *algo, const struct algo_sample_frame *frame, int state);
/* the call under algo_enabled(0xa) (0x7d65a, 0x7d672); id 10 is PPG_BR. */
extern void ppg_br_algo_step(void *algo, const struct algo_sample_frame *frame);
/* the call under algo_enabled(0xb) (0x7d676, 0x7d6b6); id 11 is PPG_APNEA. */
extern void ppg_apnea_algo_step(void *algo, const struct algo_sample_frame *frame, int beat, void *hr);
/* the call under algo_enabled(0xe) (0x7d6ba, 0x7d71e); id 14 is SLEEP_WAKE.
   The four HRV getters feed it on the stack (0x7d6ea..0x7d706). 1279 calls
   in the trace, one per popped slot.
   */
extern void sleep_wake_algo_step(void *algo, int beats, unsigned int a, unsigned int b);
/* the call under algo_enabled(0x12) (0x7d722, 0x7d73e); id 18 is PPG_RR. */
extern void ppg_rr_algo_step(void *algo, const struct algo_sample_frame *frame, int beat);
/* one halfword load out of a global and nothing else; its only caller is
   algo_manager_feed_sample, which divides the result by 25 to decide which
   samples reach algo_dispatch_sample. 1279 calls in the trace, one per
   popped slot, immediately before each algo_dispatch_sample.
   */
extern unsigned short sensors_sync_get_rate_hz(void);
/* a peak detector by shape, reached only from ppg_hrv_algo_step through
   0x7d778 (9016 calls against 1131 samples): it pushes the sample into a
   30-slot delay line (0x7d870), decays the running threshold by a constant
   and clamps it at 2.0 (0x7d884..0x7d89a), and once the line is full
   compares the sample fourteen back (0x7d8b4) against the fourteen before it
   and the fourteen after it (the two loops at 0x7d8cc and 0x7d8e6),
   declaring a peak only when it is strictly the largest and above the
   threshold.
   */
extern void ppg_hrv_peak_detect(void *det, float sample);
/* zero, then `vldmia`/`vadd.f32` over n words and return the accumulator. */
extern float vec_f32_sum(unsigned int n, const float *x);
/* vec_f32_sum divided by n as a float, zero for n == 0. */
extern float vec_f32_mean(unsigned int n, const float *x);
/* vec_f32_mean, then `vfma` of (x[i] - mean) with itself over the array
   (0x743ee), divided by n and passed through the double sqrt; the population
   deviation, zero for n == 0.
   */
extern float vec_f32_stddev(unsigned int n, const float *x);
/* vec_f32_sum of the weights, refused when its magnitude is below the
   constant at 0x4d48c, then `vfma` of x[i] with w[i] (0x7445e) divided by
   that sum.
   */
extern int vec_f32_weighted_mean(unsigned int n, const float *x, const float *w, float *out);
/* the first element, then `vmovmi` on each `vcmp` that runs lower. */
extern float vec_f32_min(unsigned int n, const float *x);
/* the first element, then `vmovgt` on each `vcmp` that runs higher. */
extern float vec_f32_max(unsigned int n, const float *x);
/* out[i] = x[i] + s over n words. */
extern int vec_f32_add_scalar(unsigned int n, const float *x, float *out, float s);
/* out[i] = a[i] * b[i] over n words, three cursors advancing together. */
extern int vec_f32_mul(unsigned int n, const float *a, const float *b, float *out);
/* the running maximum's index, kept in a counter the `movgt` copies out
   (0x9f57c); -1 for n == 0. The value itself is never returned, which is
   what separates it from vec_f32_max.
   */
extern int vec_f32_argmax(unsigned int n, const float *x);
/* refuses with -2 on the first negative element (0x9f5dc), then divides the
   array by its own vec_f32_sum through vec_f32_div_scalar, so the result
   sums to one.
   */
extern int vec_f32_normalise_sum(unsigned int n, const float *x, float *out);
/* out[i] = powf(x[i], e) over n words, the exponent held in s16 across the
   loop, refused on the first NaN the `vcmp` of the result with itself finds.
   */
extern int vec_f32_powf(unsigned int n, const float *x, float *out, float e);
/* moves state one step towards target and clamps at target: the two arms add
   or subtract step and take the result only while it has not overshot
   (0x9feba, 0x9fece).
   */
extern float slew_limit_step(float target, float state, float step);
/* two delay stores and a subtract: the object keeps x[n-1] at +0 and x[n-2]
   at +4 behind a three-valued warm-up counter at +8, emits x[n] - x[n-1] on
   the first full step and (x[n] - x[n-2]) * 0.5 afterwards.
   */
extern float central_diff_step(void *state, float x);
/* the maximum of the last three samples, kept with two delay slots and three
   fmaxf calls: the departing sample is compared against the maximum of the
   two that stay (0xa336e), so the window's maximum is recomputed only when
   the one leaving held it.
   */
extern float running_max3_step(void *state, float x);
/* the window object is {full u8 @0, capacity i32 @4, head i32 @8, float *
   @0xc}: advance head modulo capacity, store, and set full the first time
   head reaches capacity - 1.
   */
extern int window_f32_push(void *win, float x);
/* window_f32_push over the same object with a byte element (`strb`). */
extern int window_u8_push(void *win, unsigned char x);
/* the window in chronological order: it starts at head + 1 and wraps, so the
   oldest sample comes first. Refuses unless n is exactly the capacity (-1),
   the window is full (-3) and the destination is not the window's own buffer
   (-4).
   */
extern int window_f32_copy_out(void *win, int n, float *out);
/* a second window layout, {capacity i32 @0, float * @4, head i32 @8, full u8
   @0xc}: it returns the value it overwrites, so one call is both the push
   and the sample leaving the line.
   */
extern float delay_f32_push_pop(void *line, float x);
/* the element k after the oldest, (head + 1 + k) modulo capacity, over the
   same object delay_f32_push_pop writes. ppg_hrv_peak_detect is its only
   caller and walks the whole line with it.
   */
extern float delay_f32_tap(void *line, int k);

/* globals */
/* indexed by sensor id after the `cmp r0,#1` that refuses anything else
   (0x59718, 0x59804, 0x59838); entry 0 is the accelerometer, entry 1 the
   PPG, which the copy lengths fix -- the accel builder hands over a 0xc-byte
   stack struct as sensor 0 and the PPG builder a 0x40-byte one as sensor 1.
   */
extern const struct sensors_sync_stream *sensors_sync_streams[0x2];
extern const struct sensors_sync_stream sensors_sync_stream_ppg;
extern const struct sensors_sync_stream sensors_sync_stream_acc;
extern struct sensors_sync_ring sensors_sync_ring_acc;
extern struct sensors_sync_ring sensors_sync_ring_ppg;
/* the handle xQueueCreateMutexStatic leaves at 0x59700, taken with
   portMAX_DELAY by sensors_sync_lock and given by sensors_sync_unlock; its
   static queue storage is the 0x54 bytes at 0x2001d280.
   */
extern void *sensors_sync_mutex;
/* the one buffer both streams write into, 0x2600 bytes; the wrap is the `cmp
   #0x80` in the push (0x597c0), in the consumer's read cursor (0x596a4) and
   in the `and #0x7f` at 0x5963a.
   */
extern struct sensors_sync_slot sensors_sync_slots[0x80];
/* written only by sensors_sync_set_flush_cb and called only at 0x59636;
   abi/out/ghidra shows INIT installing raw_data_save's 0x30ea0 there (pool
   word at 0x2e568).
   */
extern sensors_sync_flush_fn sensors_sync_flush_cb;
/* the read cursor both streams share, which is what makes the two rings one
   ring: the push compares it against a stream's own write index to detect
   the overrun (0x5975c) and advances it past the lost slot (0x5978c), the
   consumer reads it to place the batch callback (0x595c0) and advances it
   once per popped slot (0x596aa), and sensors_sync_reset clears it
   (0x598a6).
   */
extern unsigned short sensors_sync_rd;
/* a bitmask, set and never cleared here: bit 0 by sensors_sync_reset
   (0x598ba) and bit 1 by the push's overrun path (0x59776). The consumer
   hands a non-zero value to 0x31410 and zeroes it (0x59646..0x59650), which
   is the "[TRACKER_ALGO] Exception detected (0x2)" line.
   */
extern unsigned int sensors_sync_exception_flags;

/* tables */
/* the ten MAX86173 measurement configurations by name --
   MULTIPPG_LONG_MEASURE__ACC, MULTIPPG__ACC, MULTIPPG_MONO_LONG_MEASURE__
   ACC, MULTIPPG_MONO__ACC, PPG_IR940_NORTH__ACC, PPG_IR__ACC,
   MULTIPPG_GREEN_LONG_MEASURE__ACC, SINGLEPPG_GREEN_MEASURE_ECG__ACC,
   SINGLEPPG_GREEN__ACC and MULTIPPG_GREEN__ACC. It sits in the zeros above
   algo_table and the eleventh row is not one: its name pointer lands on a
   lone "e". index is a permutation of 0..9 and is what the rest of the image
   indexes the set by, since the rows are not stored in that order.
   */
extern struct ppg_mode ppg_mode_table[10];

#endif
