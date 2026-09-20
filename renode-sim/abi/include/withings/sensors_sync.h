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

/* the state of a transposed direct-form II IIR section, the filter the whole
   sensor library is built out of: `n` coefficients in `b` and `a` and two
   state buffers of n-1 floats that iir_df2t_step swaps at the end of every
   call, so the one at +0xc is always the state the next call reads. Nothing
   in the object is a reflection coefficient, which is what rules the lattice
   readings out.
   */
struct iir_df2t {
    int n;
    const float *b;
    const float *a;
    float *state_out;
    float *state_in;
};

/* the direct-form I sibling iir_df1_step keeps its history in: two rings of
   `n` floats, the inputs in `x_hist` and the outputs in `y_hist`, shifted one
   place per call. `y` is both the accumulator the loop builds and the result
   the caller reads back. `warmup` picks how the rings are seeded before the
   first full window: 1 and 2 each hold one of the two rings at the incoming
   sample and anything else holds both.
   */
struct iir_df1 {
    int seeded;
    int n;
    int warmup;
    float y;
    float *x_hist;
    float *y_hist;
    const float *den;
    const float *num;
};

/* the deadband level tracker level_track_step owns: the last input, the level
   that follows it only in jumps of at least the threshold, and the byte that
   says the first sample has been seen.
   */
struct level_track {
    int last;
    int level;
    unsigned char seeded;
    unsigned char pad_9[0x3];
};

/* the FFT plan fft_real_split and fft_complex_transform work from. +0x10 is
   the real point count the caller set up (512 for the HR chain, from the
   `cmp.w r3, #0x200` in spectrum_fft_input) and +0 is the half of it
   fft_real_split writes there before handing the complex transform its own
   length; +0x14 is the twiddle table the split's vfma/vfms pairs index. The
   bytes between are the complex transform's own stage tables and are not
   declared, because only that one body reads them.
   */
struct fft_plan {
    unsigned short n_complex;
    unsigned char stages[0xe];
    unsigned short n_points;
    unsigned char pad_12[0x2];
    const float *twiddles;
};

/* what spectrum_fft_input and spectrum_power_bins share: the input length, the
   scale every output bin is multiplied by, the optional analysis window, the
   complex buffer the transform writes and the padded input it reads, and the
   plan itself at +0x20, which is the offset spectrum_fft_input adds before
   calling fft_real_split.
   */
struct spectrum_plan {
    int n_in;
    unsigned int field_4;
    float scale;
    unsigned char windowed;
    unsigned char pad_d[0x3];
    const float *window;
    float *fft_out;
    float *fft_in;
    unsigned int field_1c;
    struct fft_plan fft;
};

/* the constants a spectrotrack is configured by, reached only as the enclosing
   object's +0. +0x8 is the exponent spectrotrack_posterior_update tempers the
   likelihood with and +0xc its bin count as a float; +0x10 and +0x14 are the
   peak-weight floor and the spread ceiling spectrotrack_publish gates on and
   +0x18 the sample level spectrotrack_grade_quality compares against. The
   three (float, count) pairs at +0x8, +0x10 and +0x18 are what the refused
   0xa304e selects between with an immediate 0, 1 or 2.
   */
struct spectrotrack_cfg {
    float field_0;
    float field_4;
    float exponent;
    float bins_f;
    float peak_min;
    float spread_max;
    float quality_level;
};

/* a discrete distribution over one axis of values, which is what the HR chain
   estimates a rate with. `weight` is `bins` floats that sum to one:
   spectrotrack_posterior_update multiplies a smoothed, tempered likelihood
   into it and renormalises, and the two estimators read a value out of it --
   the peak's own axis value, or the weighted mean of the axis over a window of
   `window` bins around the peak. `estimate`, `spread`, `peak` and `mean` are
   what they leave behind, and spectrotrack_reset puts the whole array back to
   a uniform 1/bins.
   */
struct spectrotrack {
    unsigned char ready;
    unsigned char pad_1[0x3];
    int bins;
    const float *axis;
    float *weight;
    int window;
    float estimate;
    float spread;
    float peak;
    float mean;
    float *scratch_a;
    float *scratch_b;
    float *scratch_c;
    unsigned char state_30[0xc4];
    float published;
    unsigned char valid;
    unsigned char quality;
    unsigned char pad_fa[0x2];
};

/* the object spectrotrack_feed, spectrotrack_publish and
   spectrotrack_grade_quality take: the configuration and the tracker it drives.
   The four-byte shift between the two is why those three name offsets four
   higher than the estimators do.
   */
struct spectrotrack_slot {
    const struct spectrotrack_cfg *cfg;
    struct spectrotrack track;
};

/* the running sum and count accum_i64_add fills and accum_i64_mean_reset
   drains: both are 64-bit and the drain is one __aeabi_ldivmod followed by
   zeroing all sixteen bytes (0x9edfc, 0x9ee00), so a mean can never be taken
   twice over the same samples.
   */
struct accum_i64 {
    long long sum;
    long long count;
};

/* motion detection's whole state, at +8 of its algorithm object. The three
   axis filters are ewma_fixed_step states seeded from the first sample, and
   `energy` is the distance between the raw sample and the smoothed one, summed
   into `window` once per sample by motion_energy_step and turned into `mean`
   and `moving` once per 25 samples by motion_window_decide.
   */
struct motion_detect {
    unsigned short seed;
    unsigned char pad_2[0x2];
    int ewma_x;
    int ewma_y;
    int ewma_z;
    int energy;
    unsigned char pad_14[0x4];
    struct accum_i64 window;
    int mean;
    unsigned char moving;
    unsigned char use_abs;
    unsigned char pad_2e[0x2];
};

/* the beat detector's state, the 0xb0 bytes beat_peak_detect owns.
   ppg_heart_beats_algo_step keeps two of them, at +0x84 and +0x134 of its own
   object, and the 0xb0 between those two is what fixes the size; the second is
   fed the same four values negated, so it is the trough detector.
   `ring` is 0x24 floats indexed by `head` through ring_index_advance, and the
   position is in quarter samples because the four values are four interleaved
   sub-samples.
   */
struct beat_detect {
    float ring[0x24];
    int head;
    int channel;
    int value;
    int samples;
    int position;
    int prev_position;
    int fill;
    float rate;
};

/* functions */
/* state * alpha + x * (1 - alpha) in 64-bit fixed point, the coefficient
   arriving as the two halves of one 64-bit value.
   */
extern int ewma_fixed_step(int state, int x, unsigned int alpha_lo, int alpha_hi);
/* one accelerometer sample into the three axis filters and the energy
   accumulator.
   */
extern int motion_energy_step(struct motion_detect *m, int x, int y, int z, unsigned int alpha_lo, int alpha_hi);
/* drains the window, stores its mean and decides the moving flag against the
   threshold.
   */
extern int motion_window_decide(struct motion_detect *m, int threshold);
/* (i + step + capacity) modulo capacity, kept non-negative so a negative step
   walks the ring backwards.
   */
extern int ring_index_advance(int i, int capacity, int step);
/* pushes four values into the ring and, once it has filled, declares a beat at
   the element sixteen back when it is above all sixteen before it and all
   fifteen after it.
   */
extern int beat_peak_detect(struct beat_detect *d, const void *values);
/* the constant at 0x4ff78 divided by the gap since the previous beat, left in
   `rate`.
   */
extern int beat_rate_from_interval(struct beat_detect *d);
/* the running sum and count, one sample at a time. */
extern int accum_i64_add(struct accum_i64 *a, int x);
/* the mean of what has been accumulated, and the accumulator back to zero. */
extern long long accum_i64_mean_reset(struct accum_i64 *a);

/* the complex FFT fft_real_split is built on: radix butterflies over the
   plan's twiddle table, reached from nothing else in the image.
   */
extern int fft_complex_transform(struct fft_plan *plan, const float *in, float *out);
/* the real-input transform: the complex transform over n/2 points plus the
   conjugate-symmetric split, in that order for the forward direction and the
   other order for the inverse. Its output is the packed real spectrum whose
   first word holds DC and Nyquist together.
   */
extern int fft_real_split(struct fft_plan *plan, const float *in, float *out, int inverse);
/* windows the n inputs, zeroes the rest of the 512-point buffer and runs
   fft_real_split over it.
   */
extern int spectrum_fft_input(struct spectrum_plan *plan, int n, const float *x);
/* the 257 scaled magnitude-squared bins of that packed spectrum, DC and
   Nyquist taken from the single word that carries both.
   */
extern int spectrum_power_bins(struct spectrum_plan *plan, int bins, float *out);
/* one sample through a transposed direct-form II section; returns y. */
extern float iir_df2t_step(struct iir_df2t *f, float x);
/* one sample through the direct-form I section; the result is also left in
   `y` and at the head of the output ring.
   */
extern float iir_df1_step(struct iir_df1 *f, float x);
/* a second copy of central_diff_step whose warm-up counter is a word. */
extern float central_diff_step_i32(void *state, float x);
/* moves the level only by jumps of at least `threshold` and returns what is
   left of the input once the level is taken out.
   */
extern int level_track_step(struct level_track *t, int x, int threshold);
/* stores `cfg[0] > x` as the byte at +4 and returns zero. */
extern int threshold_flag_step(void *state, float x);
/* the sum of the squared k-th difference of the array, k being the third
   argument halved: 0 gives the energy, 1 the first-difference energy and 2 the
   second, which are the spectral moments m0, m1 and m2.
   */
extern float vec_f32_diff_energy(int n, const float *x, int order2);
/* m1 * m1 / (m0 * m2) from three vec_f32_diff_energy calls, refused to a
   constant when the denominator underflows.
   */
extern float spectral_purity_index(int n, const float *x);
/* out[i] = sum of kernel[j] * x[i+j] over a centred kernel, with the boundary
   mode 0 dropping the tap, 1 repeating the first element and 2 mirroring.
   */
extern int vec_f32_convolve(int n, const float *x, int lo, const float *kernel, float *out, int klen, unsigned char mode);
/* x[i] multiplied by powf(tab[2i], e) inside [lo, hi] and copied through
   outside it.
   */
extern int vec_f32_band_weight_powf(int n, const float *tab, int lo, int hi, float e, float *out);
/* sqrt of the weighted mean of the squared deviations from the weighted mean;
   four of the vector primitives and a sqrtf, one per line.
   */
extern float vec_f32_weighted_stddev(int n, const float *x, const float *w, float *scratch);
/* puts the weights back to a uniform 1/bins and clears every estimate. */
extern int spectrotrack_reset(struct spectrotrack *t);
/* stores the axis, the weights, the three scratch arrays and the estimator
   window, then resets; refuses a window wider than the bin count.
   */
extern int spectrotrack_init(struct spectrotrack *t, int bins, const float *axis, float *weight, float *sa, float *sb, float *sc, int window);
/* one multiplicative update: a smoothed likelihood raised to `e`, multiplied
   into the weights, renormalised to sum one and clamped away from zero.
   Refuses an exponent outside [0, 1].
   */
extern int spectrotrack_posterior_update(struct spectrotrack *t, int bins, float *scratch, int m, float e);
/* the estimate as the axis value at the peak weight. */
extern int spectrotrack_estimate_peak(struct spectrotrack *t);
/* the estimate as the weighted mean of the axis over a window of bins around
   the peak of the smoothed weights.
   */
extern int spectrotrack_estimate_centroid(struct spectrotrack *t);
/* spectrotrack_posterior_update and then one of the two estimators, chosen by
   the mode argument; -2 for any other mode.
   */
extern int spectrotrack_step(struct spectrotrack *t, int bins, float *scratch, int m, float e, unsigned char mode);
/* publishes the estimate and sets the valid flag when the peak weight clears
   the config's floor and the spread is under its ceiling; zeroes both
   otherwise.
   */
extern int spectrotrack_publish(struct spectrotrack_slot *s);
/* the quality byte: 2 - valid below the config's level, 0 above it. */
extern int spectrotrack_grade_quality(struct spectrotrack_slot *s, float x);
/* one sample into the slot: the step, then the publish, then the quality. */
extern int spectrotrack_feed(struct spectrotrack_slot *s, float x);

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

/* The tracker's live counters, 14 words the whole module indexes off one
   base, and 0x38 is the length tracker_live_counters_reset memsets (0x5f044),
   so the object's own extent is the struct's.

   Four quantities are counted, and the four "[TRACKER_LIVE][STEPS|DIST|CCALO|
   STAIRS] ..." lines at 0x5f3ea, 0x5f568, 0x5f4b0 and 0x5f72c name the words
   of each group by printing them in order. Which quantity a group is is fixed
   twice over: tracker_live_counters_add_record (0x5f970) accumulates exactly
   four bit-fields of a `struct vasistas_activity` into the four groups --
   steps (bits 55..63, `[+6]>>7`), distance (bits 75..90), calories (bits
   128..140) and ascent (bits 160..173), at 0x5f9e8..0x5fa16 and again at
   0x5fabe..0x5faf0 -- and CMD_WAM_DISPLAYED_INFO_GET's reply (0x6cbf8) puts
   the three total getters into WamDailyActivities' `steps`, `distance` and
   `calories` fields in that order.

   The two quadruples are the two arms of that body: the certain one is what
   the awake activity records (types 0, 1, 2 and 37) add, and nothing clears
   it; the unknown one is what the sleep record (type 8) adds, and the
   discontinuity path zeroes all four of it at once (0x5f9d4..0x5f9dc), which
   is what makes them one group and not four unrelated words.

   The current minute is not in here: the STEPS, DIST, CCALO and STAIRS lines
   each print a `min` that comes from outside the struct (0x2001fcc0 for
   steps, 0x2001fcb8 for calories, 0x2001fca8 for ascent, and for distance the
   minute's own step count at 0x2001fcbc put through 0x5d808), and every total
   getter adds that word to the three fields below.
   */
struct tracker_live_counters {
    unsigned int steps_certain;      /* +0,  "[STEPS] confirmed" */
    unsigned int steps_unknown;      /* +4,  "[STEPS] unknown", and the
                                        UnknownSteps object CMD_UNKNOWN_DATA_
                                        GET sends (0x6cc50) */
    unsigned int steps_delta;        /* +8,  the argument of 0x5f3c8 */
    unsigned int distance_certain;   /* +12, "[DIST] current" */
    unsigned int distance_unknown;   /* +16, "[DIST] unknown" */
    unsigned int distance_delta;     /* +20, the argument of 0x5f52c */
    /* +24, the timestamp of the last record the accumulator consumed: the
       one word 0x5f070 saves and puts back across a reset, the bound the
       next record's timestamp is tested against as `anchor + 59..61`
       (0x5f98c..0x5f99a), and what the gap path overwrites before it clears
       the four unknown counters. */
    unsigned int anchor;
    unsigned int calories_certain;   /* +28, "[CCALO] certain" */
    unsigned int calories_unknown;   /* +32, "[CCALO] unknown" */
    unsigned int calories_delta;     /* +36, the argument of 0x5f480, which
                                        divides it by ten first */
    /* +40, "[CCALO] current": the only counter no record feeds. It takes
       0x678b0(60) once per minute of record the accumulator walks past
       (0x5fa1c..0x5fa28), so it is the resting calories of elapsed time and
       not of measured activity. */
    unsigned int calories_current;
    unsigned int ascent_certain;     /* +44, "[STAIRS] confirmed" */
    unsigned int ascent_unknown;     /* +48, the sleep arm's fourth field;
                                        the STAIRS line never prints it */
    unsigned int ascent_delta;       /* +52, the argument of 0x5f710 */
};

extern struct tracker_live_counters tracker_live_counters;
/* the minute the counters have not folded in yet: the `min` term of the
   [TRACKER_LIVE][STEPS] and [CCALO] lines and of the totals below. */
extern unsigned int tracker_live_steps_minute;
extern unsigned int tracker_live_calories_minute;

/* The three totals CMD_WAM_DISPLAYED_INFO_GET's handler (0x6cbf8) reads into
   WamDailyActivities' first, second and fifth words, in that order; its
   `ascent` and `descent` words are written as zero, so the ascent the watch
   does count never leaves it over that command. */
extern unsigned int tracker_live_steps_total_get(void);
extern unsigned int tracker_live_distance_total_get(void);
extern unsigned int tracker_live_calories_total_get(void);
extern unsigned int tracker_live_ascent_total_get(void);
/* the one counter a command of its own carries: CMD_UNKNOWN_DATA_GET sends it
   as the single word of the UnknownSteps object. */
extern unsigned int tracker_live_steps_unknown_get(void);
/* Each adder stores its argument as its group's delta and logs the group; the
   calories one divides by ten on the way in. */
extern void tracker_live_steps_add(unsigned int delta);
extern void tracker_live_distance_add(unsigned int delta);
extern void tracker_live_calories_add(unsigned int delta);
extern void tracker_live_ascent_add(unsigned int delta);
/* the step goal, off tracker_live_steps_total_get against 0x6089c's goal; the
   percentage it has already reported lives in the retained block at
   0x20002800+0xd7, so a reboot does not re-announce the goal. */
extern int tracker_live_goal_check(void);
extern void tracker_live_counters_reset(void);
extern void tracker_live_counters_add_record(const void *record);

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
