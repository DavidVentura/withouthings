/* HWA10 (ScanWatch 2) application firmware v3411: the ecg module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_ECG_H
#define WITHINGS_ECG_H

#include "withings/bat.h"

struct algo_sample_frame;

/* one object at 0x20011668, 0x3128 bytes, reached through three literals --
   itself, +0x2000 (0x20013668, which is how the ring cursors are written)
   and +0x3000 (0x20014668, the handles and the configuration) -- and nothing
   outside the ECG module loads any of the three. +0 is the `%d` of "[ECG]
   algo version %d" (0x4a538) and is set to 2 at 0x4a520. +1 is the state the
   7-way `tbh` at 0x4a094 switches on and the only thing 0x49bb8 writes,
   which also saves the old value into +2; nothing reads +2. +4 is cleared
   and tested around the ADC start and stop (0x49c3c, 0x4a1fc). +8 counts
   samples in steps of 0x32 (0x49f54) and is compared against +0x3118
   (0x49f4a). +0xc is the repeat counter, initialised to -1 (0x4a746) and
   decremented at 0x4a482. +0x10 is the `result = %d` of the stop line
   (0x49c90), +0x14 the "expected evt" counter and +0x18 the `%u` of "Nb
   dropped buffers" (0x49c9c). +0x1c and +0x20 are written once each
   (0x4a19c, 0x4a1ba, the latter from dblib id 0x11f) and read nowhere that
   names them. +0x28 is an embedded algorithm object: 0x4a52c initialises it
   as (base+0x28, 300, 1610) and every later use passes the same address; the
   loads at +0x6c, +0xcc8 and +0xd08 are inside it. +0x2478 is a 144-entry
   i16 ring with its write index at +0x2598 (wrapped at 0x90 at 0x49d24 and
   0x4a04e) and a wrapped flag at +0x259a, all three written through the
   +0x2000 literal. +0x30cc is the `%d` of the result line (0x4a400), +0x30d4
   the 14-word block the algorithm's own result accessor fills (`ldm`/`stm`
   at 0x4a342..0x4a358) and out of which four floats and one word are read at
   0x4a364..0x4a37a. +0x310c is the module mutex, created by
   xQueueCreateMutexStatic at 0x4a736 over the storage at 0x2001924c and
   taken with a 500-tick timeout by the task and by every entry point;
   +0x3110 is a one-slot semaphore created only for the duration of
   ecg_task_stop (0x4a620, deleted at 0x4a678). +0x3114..+0x3124 are the five
   configuration words ecg_acquisition_start copies in one block from its
   argument (0x4a54c..0x4a562): a 0x68-byte result buffer, a sample target
   that is -1 for unlimited, two words the image only ever reads a byte at a
   time, and a table of six function pointers (called at +0, +4, +8, +0xc,
   +0x10 and +0x14). Of those bytes +0x311c is only ever compared against 2,
   +0x311e gates every path that emits samples and +0x3120 selects the
   per-sample "%d" dump; +0x311d and +0x311f gate paths that leave more than
   one reading.
   */
struct ecg_session {
    unsigned char algo_version;
    unsigned char state;
    unsigned char prev_state;
    unsigned char pad_3;
    unsigned int adc_running;
    int sample_count;
    int repeats_remaining;
    int stop_result;
    unsigned int expected_evt_counter;
    unsigned int dropped_buffers;
    int field_1c;
    int field_20;
    unsigned char pad_24[0x4];
    unsigned char algo_ctx[0x2450];
    short sample_ring[0x90];
    unsigned short ring_wr;
    unsigned char ring_wrapped;
    unsigned char pad_259b;
    unsigned char pad_259c[0xb30];
    int result_code;
    unsigned char field_30d0;
    unsigned char pad_30d1[0x3];
    unsigned char algo_result[0x38];
    void *mutex;
    void *stop_done_sem;
    void *result_out;
    int target_sample_count;
    unsigned char cfg_mode;
    unsigned char field_311d;
    unsigned char cfg_stream;
    unsigned char field_311f;
    unsigned char cfg_raw_print;
    unsigned char pad_3121[0x3];
    const void *callbacks;
};

/* enabled is one byte of a packed array descending from 0x20026bfa in row
   order. prereq is a second RAM byte the row shares with other rows -- the
   four HR rows (HR_BURST, HR_SPECTROTRACK, HR_TEMPO, PPG_HRV) all point at
   0x20026bfb and the rows with no prerequisite hold NULL. order is signed:
   0xff appears as -1 on HR_BURST, MOTION_DETECTION and SLEEP_WAKE. hook is
   set on the first row alone (0x4e9ad) and NULL on the other sixteen.
   */
struct algo_entry {
    unsigned char *enabled;
    unsigned int id;
    const char *name;
    unsigned char *prereq;
    unsigned char group;
    signed char order;
    unsigned short pad;
    void *hook;
};

/* a decision node: compare feature's value against threshold and take left
   or right, where a non-negative child is the index of the next node in the
   same tree and a negative one is the leaf's own value. Both children are
   strictly greater than the node's own index in all 77 rows, so each tree is
   stored in topological order and its last node is a pure leaf pair.
   */
struct algo_tree_node {
    unsigned int feature;
    unsigned int threshold;
    short left;
    short right;
};

/* functions */
/* nine copies and nothing else: channel 3's counts, scale and flag into the
   frame's first selection slot, channel 13's into the second and channel 6's
   into the third, in place. It is what fixes the frame head's three-slot
   shape and the three channels the algorithms actually use.
   */
extern int algo_frame_select_channels(struct algo_sample_frame *frame);
/* one byte read out of a global table indexed by the algorithm id, and every
   caller is a gate in algo_dispatch_sample.
   */
extern unsigned char algo_enabled(unsigned int algo);
/* hands one frame to each enabled algorithm: it folds the selected channels
   in (0x7d4fe), then for each of the ids 9, 8, 0xc, 7, 3, 4 and the rest
   asks algo_enabled and, if it answers, calls that algorithm with a fixed
   slice of the one context in r0 and the frame in r1 (0x7d506..0x7d5a4 and
   on). A per-id enable test in front of a per-id call is the whole body.
   */
extern void algo_dispatch_sample(void *ctx, struct algo_sample_frame *frame);
/* takes the ECG mutex at +0x10c, moves the state byte to 2, copies the five
   words (20 bytes, the loop at 0x4a54c..0x4a562; both callers build a
   20-byte stack struct) of the caller's configuration into the session
   (0x4a534..0x4a548), requests the high-frequency clock through the
   SoftDevice (svc 66 at 0x4a558) and posts state 1; its callers outside the
   module are the shell's ecg command and the WPP measure-start helper.
   */
extern int ecg_acquisition_start(const void *cfg);
/* the other half: releases the high-frequency clock (svc 67 at 0x49cba),
   clears the handle at +0x10 the start set and posts state 0. It is the only
   hfclk release in the module and pairs with the one request.
   */
extern void ecg_acquisition_stopped(void);
/* creates a one-slot semaphore, posts state 6 to the ECG task, blocks on
   that semaphore until the task acknowledges, then deletes it and drops the
   mutex; its caller outside the module is WPP_CMD_MEASURE_STOP.
   */
extern int ecg_task_stop(void);
/* the only writer of ecg_session.state: it saves the old value into
   prev_state and notifies the ECG task with 0x200 (0x49bc6). Every state
   transition in the module goes through it.
   */
extern void ecg_set_state(unsigned char state);
/* one load of ecg_session.algo_version, the `%d` of "[ECG] algo version %d". */
extern unsigned char ecg_get_algo_version(void);
/* returns ecg_session.state != 0 and touches nothing else. */
extern int ecg_is_active(void);
/* returns ecg_session.state > 1; its two callers use it as the "in progress"
   gate, one of them in front of "Cancel called while ECG is not in
   progress".
   */
extern int ecg_is_measuring(void);
/* -1 unless ecg_is_measuring, else ecg_session.sample_count divided by 90
   and clamped to 0..100 (0x4a778..0x4a790). A sample count turned into a
   percentage is the whole body.
   */
extern int ecg_get_progress_percent(void);
/* reads and clears ecg_session.adc_running (0x49c3c, 0x49c42), drops the
   module mutex around the driver call at 0x49c4c and takes it again; it is
   the only clear of that field outside the reset.
   */
extern void ecg_adc_acquisition_stop(void);
/* creates the ECG task queue and the module mutex (0x4a736), zeroes the stop
   semaphore, state and prev_state, and sets repeats_remaining to -1. It runs
   once from INIT and writes no other field.
   */
extern void ecg_module_init(void);
/* takes the module mutex with a 500-tick timeout, loads ecg_session.state
   and dispatches on it through the seven-arm `tbh` at 0x4a094, every arm
   leaving through the single release at 0x4a0c8. A switch on the state field
   and nothing else.
   */
extern void ecg_task_state_machine(void);
/* the ADC block callback ecg_task_state_machine installs (its address is the
   pool word loaded at 0x4a1f0). It counts a dropped buffer when the mutex is
   not free (0x49db4, the "%u" of "Nb dropped buffers"), checks the block
   against ecg_session.expected_evt_counter and bumps it (0x49de0), walks the
   block's 0x32 samples, adds 0x32 to sample_count (0x49f54) and stops when
   that passes target_sample_count. The derived name it takes over came from
   the shell command that also calls it.
   */
extern void ecg_adc_block_handler(void);
/* the only reader of ecg_session.sample_ring that walks it whole: 0x90
   iterations from ring_wr, starting at zero or at the cursor depending on
   ring_wrapped (0x49ffe), emitting two samples at a time. Three ring fields
   and one emit call leave one reading.
   */
extern void ecg_sample_ring_flush(void);
/* the algorithm object's constructor, and the two arguments are read off its
   own bounds checks and off the protocol in one step: it refuses r1 outside
   200..2000 and r2 below 100 (0x775e0, 0x77612), then stores them at ctx+4
   and ctx+8. ecg_acquisition_start calls it once per measurement with
   (ecg_session+0x28, 300, 1610) at 0x4a52c, and 300 and 1610 are exactly the
   sampling_freq of the StoredSignalMeta and the gain of the
   UnitConversionParameters the watch sends the phone when the measurement
   starts, so r1 is the sample rate in hertz and r2 the microvolt scale.
   */
extern void ecg_algo_init(void *ctx, unsigned int sample_rate_hz, unsigned int gain);
/* `ldrd r1, r2, [r0,#4]; b.w ecg_algo_init` -- two instructions that re-run
   the constructor with the rate and gain the object already holds. Called
   from ecg_acquisition_start straight after the init and from
   ecg_task_state_machine's start arm.
   */
extern void ecg_algo_reset(void *ctx);
/* one call per ADC sample: ecg_adc_block_handler's 50-iteration loop calls
   it at 0x49ef6 with the raw halfword and the session's algorithm object,
   and the trace counts 9000 entries in a 9000-sample measurement. Its own
   first test is sample_count against 30 * sample_rate, the same window
   ecg_algo_finish asserts on, and it is the only path into the beat
   detector.
   */
extern void ecg_algo_process_sample(void *ctx, short sample);
/* one entry per measurement in the trace, from ecg_task_state_machine's
   processing arm at 0x4a334, before anything reads a result. Its own guard
   is the name: the first thing it does is compare sample_count against 30 *
   sample_rate and, when they differ, log its pool word 0x77a14 "WARNING:
   Finish function must be called only once after %li samples are processed"
   and trap (0x77940). An acquisition short of the whole thirty seconds
   reaches exactly that line and the undefined instruction after it. It then
   normalises four accumulated floats into ctx+0x10..0x1c, which are the four
   the log prints as ECGSW2 out=%d, %d, %d, %d once multiplied by 100.
   */
extern void ecg_algo_finish(void *ctx);
/* returns ctx+0x28 behind an assert on ctx+0xc, once per measurement. The
   caller is the reading: ecg_task_state_machine stores its result at 0x4a3be
   into ecg_session+0x30d0 and logs that byte as "[ECG DIAGNOSIS] ECGSW2 HR:
   %d". A run against a synthetic 62 bpm waveform logs 62.
   */
extern int ecg_algo_heart_rate(void *ctx);
/* `if (ctx->e78 <= 6 || ctx->e84 == 0.0f) return 0; return lroundf(60000.0f
   / ctx->e84);` -- sixty thousand over a float divided and rounded is beats
   per minute from an interval in milliseconds, the interval being the mean
   ecg_rr_interval_add accumulates into that same object, and the guard is
   seven intervals. This is the live one, not the diagnosis's:
   ecg_adc_block_handler calls it every 300 samples (0x49faa, one second at
   the measurement's own rate) and drops the answer above 255, and the trace
   counts 30 entries in a thirty-second measurement.
   */
extern int ecg_algo_heart_rate_from_rr(void *ctx);
/* returns ctx+0x44 behind the same assert, once per measurement.
   ecg_task_state_machine takes it at 0x4a35c, sign-extends it into
   ecg_session.result_code, and the log line beside it reads the same word as
   "[ECG DIAGNOSIS] ECGSW2 WS Diagnosis: %li"; afib_class_names is the table
   it indexes.
   */
extern int ecg_algo_diagnosis(void *ctx);
/* copies 0x38 bytes from ctx+0x10 with four `ldm`/`stm` pairs and a
   two-register tail, once per measurement. The caller settles what the block
   is: ecg_task_state_machine copies the result straight on into
   ecg_session+0x30d4 (0x4a342), which is the fourteen-word algorithm result
   the manifest already declares, and its first four words are the class
   probabilities ecg_algo_finish normalised.
   */
extern void ecg_algo_result_copy(void *out, const void *ctx);
/* the other per-sample call in ecg_adc_block_handler's loop (0x49eb6), on
   the second algorithm object at 0x20013c04 rather than the session's, and
   the only thing between a raw ADC sample and the filtered value
   ecg_filter_output returns. The trace counts 9144 entries in a 9000-sample
   measurement, the extra 144 being ecg_filter_flush_last_sample's. What it
   is a chain of is in the report and not here: the stages are shared
   primitives outside this module.
   */
extern void ecg_filter_process_sample(void *ctx, short sample);
/* `if (ecg_algo_output_ready(ctx)) return ctx->b28;` with an assert on the
   other arm, 9000 entries in the trace. ecg_adc_block_handler stores the
   return into the next slot of the 144-sample ring (0x49ed0), so the word at
   ctx+0xb28 is the filtered sample and this is how it leaves the filter.
   */
extern int ecg_filter_output(void *ctx);
/* `if (ecg_algo_output_ready(ctx)) return ecg_filter_process_sample(ctx,
   ctx->58);` -- ctx+0x58 is where ecg_filter_process_sample stores the
   sample it was last given (0x77e70), so this feeds the chain its own last
   sample again, which is what a filter with a group delay needs to push its
   tail out. The trace counts exactly 144 entries, one per slot of the ring.
   */
extern void ecg_filter_flush_last_sample(void *ctx);
/* `return ctx->0 >= ctx->8 && ctx->b2c <= 30 * ctx->5c;` -- enough samples
   in and not past the measurement's window. Every per-sample caller gates on
   it before reading an output, three times a sample between them.
   */
extern int ecg_algo_output_ready(const void *ctx);
/* walks the block's 0x32 samples writing each into ecg_session.sample_ring
   at ring_wr and reading the slot's old value out first; once ring_wr wraps
   at 0x90 it pairs each evicted raw sample with the filtered one and hands
   the pairs to the store call, which is the same two-samples-at-a-time shape
   ecg_sample_ring_flush emits. The trace counts 180 entries, one per block.
   */
extern int ecg_sample_ring_push(const short *block, const short *filtered);
/* the classifier, and its failure message names it: ecg_algo_finish calls it
   once per measurement at 0x777b0 and the very next thing it does is test
   the output count at ctx+0x1f4c and, when it is zero, log "Neural network
   failed to provide an output" and trap (0x77964). The four floats it leaves
   at ctx+0x1f3c are what ecg_algo_finish divides by that count into the
   class probabilities, which are the "out=%d, %d, %d, %d" of the diagnosis
   line.
   */
extern void ecg_nn_classify(void *net, const void *beats, int count);
/* one entry per detected QRS complex -- 29 in a run whose own log says
   qrs_blocks_size=29 -- and the arithmetic is the definition of an RR
   interval: `t = beat[0] * 1000.0f / 300.0f;` (the sample index at the
   measurement's 300 Hz, in milliseconds) `d = t - prev; prev = t;` then d
   into the accumulator at rr+0x1f8 and, unless it is over 2000 ms or the
   hundred-and-twentieth, into the array at rr+0x14, which is then sorted.
   Its caller passes the algorithm object's ctx+0xe78, which is the same
   object ecg_algo_heart_rate_from_rr divides 60000 by.
   */
extern void ecg_rr_interval_add(void *rr, const short *beat);
/* `if (!n) return 0; best = v[0]; for (i = 1; i < n; i++) if (v[i] > best) {
   best = v[i]; r = i; } return r;` -- the index of the largest, with no
   other output. ecg_algo_finish calls it twice on its own accumulated
   floats, and the trace counts two entries per measurement.
   */
extern int argmax_f32(const float *v, int n);
/* `vcmpe.f32 s0,#0; it mi; vnegmi.f32 s0,s0` and nothing else. The compiler
   would have inlined `vabs`, so this is a called-through absolute value the
   algorithm library builds its chains out of.
   */
extern float fabsf_vfp(float x);
/* zeroes the two words accum_f32_add writes, which are the sum and the
   count.
   */
extern void accum_f32_reset(void *a);
/* one frame in, one round of the algorithms out: it keeps one sample in
   every rate/25 (the `udiv` by 0x19 at 0x64242 against the configured rate),
   calls algo_dispatch_sample and then tail-calls the publisher 0x63ddc with
   the same context. Its only caller is sensors_sync_slot_to_algo.
   */
extern void algo_manager_feed_sample(void *ctx, struct algo_sample_frame *frame);
/* the other half of algo_manager_feed_sample: a chain of algo_enabled gates,
   each followed by a read of that algorithm's own result object and a call
   to that algorithm's consumer; the heart-rate arm builds a hr_algo_report
   on its stack (0x63fc4..0x64068) and hands it to hr_measure_on_algo_result.
   */
extern void algo_manager_publish_results(void *ctx);
/* one load whose result is hr_measure_ctx.sdnn and the `%ld` of "sdnn:". */
extern int hrv_get_sdnn(void *hrv);
/* one load whose result is hr_measure_ctx.rr_interval and the `rr:` line's
   value.
   */
extern float rr_get_interval(void *rr);
/* a float sum at +0 and an i32 count at +4, both incremented. */
extern int accum_f32_add(void *acc, float x);
/* the sum divided by the count as a float, then both back to zero. */
extern float accum_f32_mean_reset(void *acc);

/* globals */
extern struct ecg_session ecg_session;
/* a third pool of encoded bitmap bodies, paired the way rre_asset_table
   pairs a blob with its offsets: FUN_0007c260 zeroes a 0x198-byte object and
   installs fourteen addresses inside this run into it as seven pairs, the
   two of a pair eight bytes apart, and the three 0x30-byte descriptors at
   0xb8e28 pair six more with sizes 12 by 14, 12 by 12 and 10 by 12. Declared
   as bytes because two of the twenty-one addresses the image holds into it
   are 2 mod 4 (0xc29fa and 0xc2d6a), so a word window over it reads halves
   of two fields.
   */
extern unsigned char ui_bitmap_pool[4695];
/* the frame bitmaps of the three animations described at 0xbbd80, 0xbbe38
   and 0xbbe74, whose descriptors are {u32 frames, u16 width, u16 height,
   const u8 *bits, ..., u32 bytes, const u32 *frame_ms} followed by their own
   frames-long duration array. The first descriptor says 16 frames of 22 by
   20, which is 3 bytes a row and 60 bytes a frame, and 60 is exactly the
   period of the run: the mask at 0xe8354 repeats at 0xe8390. Declared as
   bytes because 0xbbe40 holds 0xe8de6, an odd address inside the run, so it
   is indexed by the byte and a word window over it reads two halves of two
   rows.
   */
extern unsigned char anim_frame_bits[4090];
/* the DER value of OID 1.2.840.113549.1.1.1 (rsaEncryption), NUL terminated,
   with "rsaEncryption" and "RSA" as the next two strings: one
   mbedtls_oid_descriptor_t's three fields laid out in source order. Nothing
   printable starts it, so the string rule could not name it and the fifteen
   pool words that load its last two bytes (0xe53f2, the one-byte literal
   "\x01" the compiler merged onto this tail) read as numbers.
   */
extern unsigned char oid_pkcs1_rsa[10];
/* the DER value of OID 1.2.840.113549.1.1.10 (RSASSA-PSS), NUL terminated,
   followed by its "RSASSA-PSS" name; nine pool words name its tail 0xe56c4
   for the same suffix-merge reason as oid_pkcs1_rsa.
   */
extern unsigned char oid_rsassa_pss[10];

/* tables */
/* UNDEFINED, NSR, AFIB, OTHER, NOISE -- the AFib classifier's verdicts,
   loaded at 0x63570 and 0x63608
   */
extern struct name_ptr afib_class_names[5];
/* the algorithm registry DEVELOPMENT.md names from the strings it logs,
   declared so its rows stop reading as numbers. Seventeen live rows and a
   terminator whose id is 0 and whose name is NULL; the terminator still
   carries an enable-flag pointer, so it is declared as an eighteenth row
   rather than cut off. In address order the ids are 1 HR_LONG_MEASURE, 2
   HR_BURST, 3 HR_SPECTROTRACK, 4 HR_TEMPO, 5 SPO2_MULTI, 6 SPO2_MONO, 7
   PPG_ARRHYTHMIA, 17 PPG_HRV, 8 MOTION_DETECTION, 9 WORN, 10 PPG_BR, 11
   PPG_APNEA, 12 PPG_HEART_BEATS, 14 SLEEP_WAKE, 16 RAW_DATA_MODE_ACC_PPG, 18
   PPG_RR and 19 LIBDBT, which are the names the algorithm manager prints and
   the ids algo_dispatch_sample gates each call on. enabled walks down one
   byte per row from 0x20026bfa, so the flags are one packed array in
   registry order.
   */
extern struct algo_entry algo_table[18];
/* four binary decision trees end to end, 18, 20, 17 and 22 nodes at 0xb443c,
   0xb4514, 0xb4604 and 0xb46d0, which is where the four pointers at
   0xf08e4..0xf08f0 point -- inside the RAM initialiser image, so the trees
   are reached through RAM and no instruction in the image names them. The
   layout is proved by the trees themselves: for all 77 rows left and right
   are either negative, which is a leaf value, or a node index strictly
   greater than the row's own and inside that tree's node count, so every
   segment closes on itself exactly at the next segment's head. feature is
   0..4 in all four trees, so the ensemble reads five features. Which
   algorithm scores it is not settled: the descriptor that holds the four
   roots also holds 0.025, 0.5, 1.0 and 1.0 as floats and a RAM pointer, and
   it is only ever addressed after the startup copy.
   */
extern struct algo_tree_node algo_tree_table[77];

#endif
