/* HWA10 (ScanWatch 2) application firmware v3411: the hr module's ABI.
 *
 * What each of these is, not where it is: abi/symbols.yaml holds the
 * addresses. Written by abi/migrate.py from the old single manifest and
 * hand-maintained since. */
#ifndef WITHINGS_HR_H
#define WITHINGS_HR_H

/* hr_algo is built out of the sensor library's filters and its rate
   tracker, so the shapes come from there. */
#include "withings/sensors_sync.h"

struct algo_sample_frame;

/* the HR algorithm's own result block at 0x2000f480, which is its object's
   +0x2c84; 0x7aa60 is the only writer of the tail and 0xa2548 the only
   reset. The head is four filter states (three of 0xa8 for the three
   selected PPG channels, one of 0x38 that also takes the accelerometer,
   initialised and stepped in that order at 0x7aa7a..0x7aa9e) whose ready
   bytes are ANDed into +0x245 at 0x7aade. +0x234..+0x23c are three 0x98-byte
   float buffers, the size taken from the memsets at 0xa25b2..0xa25c0. +0x240
   is the beats per minute, a `vcvt.u32.f32` stored at 0x7ac6c, and the same
   byte is read through the object base as +0x2ec4 at 0xa24fa, which is what
   fixes the block's offset inside the object. +0x241 is the reference the
   plausibility test at 0x7ac84 measures against, +0x242 the ready mirror,
   +0x243 the flag the acceptance test at 0x7ac98..0x7acec produces, +0x244
   the quality 0x74494 returns and +0x248 the status: 0 on success, -3 for
   too few samples (0xa257c, 0x7acfa) and -6 for a failed sub-step (0x7aba4).
   The reader 0xa265c copies +0x240..+0x244 out and returns +0x248.
   */
struct hr_algo_result {
    unsigned char filters[0x230];
    unsigned int window_samples;
    void *buf_a;
    void *buf_b;
    void *buf_c;
    unsigned char bpm;
    unsigned char bpm_ref;
    unsigned char ready;
    unsigned char acceptable;
    unsigned char quality;
    unsigned char warm;
    unsigned char pad_246[0x2];
    int status;
};

/* the twelve bytes the algorithm manager builds on its stack and hands to
   the HR consumer, and the only call of that consumer is 0x64072, so the
   layout is forced from both sides. The publisher zeroes all twelve
   (0x63fc4, 0x63fc8), writes +0 to 1, +1 and +2 from the two beat rates the
   log lines call `hr_store: %dbpm` and `hr_live: %dbpm` (0x62624, 0x62622),
   +4 from the worn flag the `worn:%s` line prints (0x63fca, 0x62620) and
   +7..+0xb as one five-byte copy of hr_algo_result's +0x240..+0x244
   (0x63fe4). The consumer gates its whole burst block on +9 (0x626ba),
   prints +0xa as `Notifiable: %s` and stores +7 into the HR context's last
   result. +3, +5 and +6 are written only on the path taken when one
   algorithm is disabled (0x64124, 0x64130, 0x6406e), which is not enough to
   name them.
   */
struct hr_algo_report {
    unsigned char updated;
    unsigned char hr_store;
    unsigned char hr_live;
    unsigned char field_3;
    unsigned char worn;
    unsigned char field_5;
    unsigned char field_6;
    unsigned char bpm;
    unsigned char bpm_ref;
    unsigned char ready;
    unsigned char acceptable;
    unsigned char quality;
};

/* the HR_MEASURE module's own state at 0x20020004, every field of which is
   printed by one of its log lines or read by the WPP handler. +0 is the mode
   the `BURST`/`CONTINUOUS` selection at 0x62670 switches on (1 and 2 are
   burst, 3 and 4 continuous, from the range tests at 0x6269a and 0x62a66)
   and +1 and +2 are the two requests that set it. +8 is the uptime a
   measurement started at, subtracted at 0x626d0 to give its duration, and
   +0xc the counter the "Limit reached (non storable samples count = %u /
   %d)" line compares against 0x2c. +0x14, +0x18 and +0x1c are the `sdnn:
   %ld`, `rmssd: %ld` and quality the three HRV getters return (0x62686,
   0x6268e, 0x62698), and +0x24 and +0x28 the `rr:` and `is_censored:` the
   two RR getters return (0x628bc, 0x628d2). +0x30 is the byte the WPP
   handler answers CMD_HR_MEASURE with: written from hr_algo_report's bpm at
   0x62746, read at 0x62560 and refused when zero. +0x34 is the measurement
   timer 0x623a2 creates. 0x62040 clears +8, +0xc, sixteen bytes from +0x14,
   +0x24, +0x28 and +0x2c.
   */
struct hr_measure_ctx {
    unsigned char mode;
    unsigned char mode_1_requested;
    unsigned char mode_2_requested;
    unsigned char field_3;
    unsigned char field_4;
    unsigned char field_5;
    unsigned char auto_burst_armed;
    unsigned char pad_7;
    unsigned int measure_start_uptime;
    unsigned int non_storable_count;
    unsigned char vasistas_stored;
    unsigned char pad_11[0x3];
    int sdnn;
    int rmssd;
    signed char hrv_quality;
    unsigned char pad_1d[0x3];
    unsigned int window_start_uptime;
    int rr_interval;
    signed char rr_censored;
    unsigned char pad_29[0x3];
    unsigned int rr_uptime;
    unsigned char last_bpm;
    unsigned char pad_31[0x3];
    void *timer;
};

/* sixteen bytes at 0x2001fff4 that the watch face reads and nothing else
   writes: the live rate and its validity as one halfword (cleared together
   by the `strh` at 0x62064, set at 0x621ae and 0x621b2) with the uptime it
   was taken at, and the same pair for the last burst result (0x62984,
   0x6298a). 0x624b0 hands the whole block out under the module lock.
   */
struct hr_screen_data {
    unsigned char live_valid;
    unsigned char live_bpm;
    unsigned char pad_2[0x2];
    unsigned int live_uptime;
    unsigned char pad_8;
    unsigned char burst_bpm;
    unsigned char pad_a[0x2];
    unsigned int burst_uptime;
};

/* the window_zscore_step object: the push window at +0, the byte that says it
   has filled, the newest sample divided by the window's own standard
   deviation, and the buffer the whole divided window is written to.
   */
struct window_zscore {
    unsigned char full;
    unsigned char pad_1[0x3];
    int capacity;
    int head;
    float *buf;
    unsigned char ready;
    unsigned char pad_11[0x3];
    float scaled;
    unsigned int field_18;
    float *normalised;
};

/* one PPG channel's front end, the 0x214 bytes hr_chan_filter_step owns and
   the stride hr_channel_bank_step repeats four times: the four channels sit at
   +0x8, +0x21c, +0x430 and +0x644 of the bank and their outputs at +0x214,
   +0x428, +0x63c and +0x850, which is that stride four times over. The order
   of the fields is the order the body steps them in -- the LED-step remover,
   the differentiator, the band filter, the z-score -- and `out` and `ready`
   are the pair the bank reads back.
   */
struct hr_chan_filter {
    struct level_track level;
    float diff_x1;
    float diff_x2;
    int diff_warm;
    struct iir_df1 band;
    unsigned char pad_38[0x20];
    struct window_zscore zscore;
    unsigned char pad_78[0x194];
    float out;
    unsigned char ready;
    unsigned char pad_211[0x3];
};

/* the four channel front ends and the accelerometer arm, at +0xa8 of the
   algorithm object. `mode` at +4 picks one channel or four. `mean` is the
   average of the four filtered outputs -- the four adds and the multiply by
   0.25 at 0xa2980..0xa29b2 -- and `accel` the magnitude of the three axes put
   through `accel_filter`. Those two floats are the whole input to everything
   downstream: hr_spectrum_step is handed exactly this pair.
   */
struct hr_channel_bank {
    unsigned char pad_0[0x4];
    int mode;
    struct hr_chan_filter chan[0x4];
    unsigned char pad_858[0x4];
    struct iir_df1 accel_filter;
    unsigned char pad_87c[0x20];
    float accel_prev;
    unsigned char primed;
    unsigned char pad_8a1[0x3];
    int replay_count;
    float mean;
    float accel;
};

/* the spectral half, at +0x964 of the algorithm object. Two 200-sample windows
   -- the channel mean and the accelerometer magnitude -- each with its own
   0x320-byte buffer right behind it, and the two normalised 257-bin spectra
   they become. The trigger is the two windows' heads agreeing and the next one
   being a multiple of the stride the config's first word carries
   (0x7b086..0x7b09e), which is why both windows are pushed every sample and
   the transform runs on one in every stride of them. `purity` is
   spectral_purity_index over the PPG window and `stddev` its vec_f32_stddev.
   The plan is embedded at +0xe6c rather than pointed at, which is the offset
   hr_spectrum_step adds before calling spectrum_fft_input.
   */
struct hr_spectrum {
    const unsigned int *cfg;
    unsigned char ppg_full;
    unsigned char pad_5[0x3];
    int ppg_capacity;
    int ppg_head;
    float *ppg_buf;
    float ppg_window[0xc8];
    float ppg_spectrum[0x101];
    unsigned char acc_full;
    unsigned char pad_739[0x3];
    int acc_capacity;
    int acc_head;
    float *acc_buf;
    float acc_window[0xc8];
    float acc_spectrum[0x101];
    struct spectrum_plan plan;
    unsigned char started;
    unsigned char pad_ea5[0x3];
    float purity;
    float stddev;
    float *scratch;
};

/* the whole HR algorithm object at 0x2000c7fc, reached only as the pointer
   algo_dispatch_sample hands hr_algo_process_sample and never by its own
   literal, which is why abi/protocol.py --struct-uses reports nothing for it
   and the layout had to be read off the argument instead.
   What fixes it is that the sub-objects abut exactly: the channel bank ends at
   +0x958 where the primed byte sits, the pair it publishes is +0x95c, the
   spectral half runs +0x964 to +0x1818, the rate tracker's slot is the 0x100
   bytes to +0x1918, the refused 0x7b320's object the 0x104 to +0x1a1c and the
   refused 0xa304e's the 0x250 to +0x1c6c -- whose published triple at +0x244,
   +0x248 and +0x24c is the +0x1c60, +0x1c64 and +0x1c68 the body reads -- and
   hr_algo_result at +0x2c84 puts its bpm at +0x2ec4, the offset
   hr_algo_get_result reads it through.
   */
struct hr_algo {
    unsigned char pad_0[0xa8];
    struct hr_channel_bank bank;
    unsigned char primed;
    unsigned char pad_959[0x3];
    float pair[0x2];
    struct hr_spectrum spectrum;
    struct spectrotrack_slot rate;
    unsigned char harmonics[0x104];
    unsigned char bands[0x250];
    const float *threshold_cfg;
    unsigned char threshold_flag;
    unsigned char pad_1c71[0x3];
    unsigned int samples;
    unsigned char pad_1c78[0x1000];
    unsigned char mode;
    unsigned char pad_2c79;
    unsigned char published;
    unsigned char clipped;
    float last_value;
    int status;
    struct hr_algo_result result;
};

/* functions */
/* paired with hr_measure_unlock around every access to hr_screen_data. */
extern int hr_measure_lock(void);
/* the give that closes every hr_measure_lock. */
extern int hr_measure_unlock(void);
/* under the lock, zeroes hr_measure_ctx's measure_start_uptime,
   non_storable_count, the sixteen bytes from sdnn, rr_interval, rr_censored
   and rr_uptime, and clears hr_screen_data's live halfword (0x62064). It
   writes no field it does not zero.
   */
extern void hr_measure_reset_context(void);
/* under the lock, stores its two arguments into hr_screen_data.live_valid
   and live_bpm and the current uptime into live_uptime (0x621ae..0x621ba).
   */
extern void hr_measure_set_live_hr(unsigned char bpm, int valid);
/* takes the lock and copies the sixteen bytes of hr_screen_data out with one
   `ldm`/`stm` pair; its derived name is the error line on the lock failure,
   not the role.
   */
extern int hr_measure_get_screen_data(struct hr_screen_data *out);
/* the sole consumer of hr_algo_report, reached only from 0x64072. It prints
   every field of the report under "[HR_MEASURE]", gates its burst block on
   the report's ready byte and stores the report's bpm into
   hr_measure_ctx.last_bpm, which is what the WPP handler answers with.
   */
extern void hr_measure_on_algo_result(const struct hr_algo_report *report);
/* refuses with -2 while the HR algorithm's mode byte is zero, otherwise
   copies hr_algo_result's bpm, bpm_ref, ready and acceptable as one word and
   quality as one byte into the caller's report and returns the status field.
   It is the only reader of that five-byte group.
   */
extern int hr_algo_get_result(struct hr_algo_report *out);
/* the only writer of hr_algo_result's bpm, bpm_ref, ready, acceptable,
   quality, warm and status. It steps the three channel filters and the
   motion filter, sets warm once all four report ready (0x7aade), and on a
   full window converts the rate and the quality to bytes (0x7ac6c, 0x7ac88)
   and decides acceptability from the sample count and the distance to
   bpm_ref.
   */
extern int hr_algo_step(void);
/* resets the four filters of hr_algo_result, memsets its three 0x98-byte
   buffers and puts status back to -3 with bpm, quality and warm at zero
   (0xa257c..0xa2592). The mirror of hr_algo_step and nothing else.
   */
extern int hr_algo_reset(void);
/* one load whose result is hr_measure_ctx.rmssd and the `%ld` of "rmssd:". */
extern int hrv_get_rmssd(void *hrv);
/* one load whose result is hr_measure_ctx.hrv_quality, the `%d` the "Poor
   quality %d < %d" line compares against 0x14.
   */
extern signed char hrv_get_quality(void *hrv);
/* one load whose result is hr_measure_ctx.rr_censored, the value the
   "is_censored: %s" line prints.
   */
extern signed char rr_get_is_censored(void *rr);
/* the per-sample half of the heart-rate algorithm: algo_dispatch_sample
   reaches it under algo_enabled(3) and, when 3 is off, under algo_enabled(2)
   through the same block (0x7d588, 0x7d5a4, 0x7d644), which are
   HR_SPECTROTRACK and HR_BURST, and it is the only caller of hr_algo_step.
   The trace has 1131 calls of this against 38 of hr_algo_step, so the
   per-sample chain runs here and the result is written once per window.
   */
extern void hr_algo_process_sample(void *algo, const struct algo_sample_frame *frame);
/* the call under algo_enabled(4) (0x7d5a8, 0x7d5b8); id 4 is HR_TEMPO. */
extern void hr_tempo_algo_step(void *algo, const struct algo_sample_frame *frame);

/* globals */
/* reached only as the HR algorithm object's +0x2c84, never by its own
   literal
   */
extern struct hr_algo_result hr_algo_result_block;
extern struct hr_measure_ctx hr_measure_ctx;
extern struct hr_screen_data hr_screen_data;

#endif
