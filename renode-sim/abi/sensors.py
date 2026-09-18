#!/usr/bin/env python3
"""The sensor algorithms: the DSP primitives, the HR chain and the trackers.

    python3 abi/sensors.py           # writes the sensor class of abi/symbols.yaml

SENSORS_SYNC is the block the log tags cannot name: it carries no `__func__`,
no per-function log line, and its callees are memcpy and soft float. What names
it is arithmetic and state. A body that multiplies a coefficient array by the
input and a second array by its own output, over a pair of buffers it swaps at
the end, is a transposed direct-form II filter whatever it is called; a body
that renormalises an array to sum one and then takes its argmax over a value
axis is an estimator over a distribution.

So every row carries the reading and the reading's checks: the callees the body
must still reach and the literal-pool words it must still load, re-measured
against the analysis this run reads, exactly as abi/stores.py does. Re-running
the analysis and re-running this is what says the reading survived. Where the
arithmetic admits more than one reading the address is left out and the module
keeps its unnamed byte, which is the point of the refusals listed at the end.

The counts in the evidence come from two Renode runs over the hooks
renode-sim/calltrace.py --module SENSORS_SYNC --count emits: a 700 s run with
`spi1.max86173 Worn true` and `HeartRateBpm 72`, and a run that drives
`spi2.adxl367 Motion 2000 30` and toggles `Worn` across four transitions. A
count is evidence about rate -- once per sample, once per window -- and never
about role, so a `rate` line says the ratio and stops.

Neither run started MOTION_DETECTION: its entry point is hooked in both and
counted zero, and the motion run's transitions land after the burst
measurement has ended, when the algorithm manager has stopped every algorithm
and only the ring push and drain are still running. So the three motion names
below rest on their bodies alone and carry no rate, and what would settle them
is a run that starts that algorithm rather than a longer one.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbols as symmap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GHIDRA = os.path.join(HERE, "out", "ghidra")
CLASS = "sensor"
# The log-tag partition's module where it has one. A handful of these bodies
# are shared primitives the partition never reached, and claiming a module for
# them would be inventing evidence, so they take the header they are declared
# in instead.
HEADER_MODULE = "sensors_sync"

# address, name, kind, evidence; `calls` are callee addresses the body must
# still reach and `reads` are literal-pool values it must still load. `rate` is
# what the counting run measured, which says how often a body runs and never
# what it does -- a ratio against the popped-slot count is the evidence that a
# stage is per sample and not per window, and the place a reading that says
# "once per window" would break.
ROWS = [
    # --- the transforms ------------------------------------------------------
    dict(address=0xA17F8, name="fft_complex_transform", kind="function",
         rate="130 calls in the 700 s HR run against 65 of hr_algo_step, 2 per window",
         calls=[0x7A12C],
         evidence="the complex FFT the real transform is built on: 1738 bytes"
                  " of radix butterflies over the twiddle table the plan's"
                  " +0x14 points at, reached only from fft_real_split and"
                  " calling only its own stage bodies. No CMSIS-DSP or KissFFT"
                  " body matches it (matches.yaml), so it is Withings' own"),
    dict(address=0xA1EC2, name="fft_real_split", kind="function",
         rate="130 calls in the 700 s HR run against 65 of hr_algo_step, 2 per window",
         calls=[0xA17F8],
         evidence="the real-input wrapper: it halves the plan's point count at"
                  " +0x10 into +0, and either runs fft_complex_transform over"
                  " N/2 complex points and then splits the result (the r3 == 0"
                  " arm at 0xa1f82) or splits first and then transforms (the"
                  " forward arm). The split is the conjugate-symmetric"
                  " recombination: for each k it forms (a+b)/2 and (a-b)/2"
                  " against the mirror element at 0xa1f38..0xa1f7c with the"
                  " twiddle pair from +0x14 applied by vfma/vfms, and the DC"
                  " term is the lone (x0+x1)/2, (x0-x1)/2 pair at"
                  " 0xa1ee8..0xa1f06, which is the packed real spectrum's"
                  " DC-and-Nyquist word"),
    dict(address=0x7A6E0, name="spectrum_fft_input", kind="function",
         rate="130 calls in the 700 s HR run against 65 of hr_algo_step, 2 per window",
         calls=[0xA1EC2],
         evidence="windows and zero-pads: it refuses unless the length equals"
                  " the plan's +0 (0x7a6e6), multiplies each of the n inputs by"
                  " the window at +0x10 when the flag at +0xc is set"
                  " (0x7a700..0x7a708), writes zero for every index past n"
                  " (0x7a72a) up to the 0x200 the `cmp.w r3,#0x200` fixes, and"
                  " hands the 512-point buffer to fft_real_split"),
    dict(address=0x7A73C, name="spectrum_power_bins", kind="function",
         rate="130 calls in the 700 s HR run against 65 of hr_algo_step, 2 per window",
         evidence="the 0x101 = 257 magnitude-squared bins of a 512-point packed"
                  " real spectrum, which is what fixes the transform's length:"
                  " bin 0 from the DC word alone (0x7a74c..0x7a760), the 255"
                  " interior bins as re*re + im*im doubled (the `vadd.f32 s15,"
                  " s15, s15` at 0x7a788) over the pairs from +8 to +0x7f8, and"
                  " the last bin from the second word of the packed pair, which"
                  " is the Nyquist term (0x7a792). Every bin is scaled by the"
                  " plan's +8. It refuses any length but 257 (0x7a742)"),

    # --- the filters ---------------------------------------------------------
    dict(address=0xA3298, name="iir_df2t_step", kind="function",
         rate="8924 calls in the 700 s HR run against 1786 of hr_algo_process_sample, 5 per sample",
         evidence="a transposed direct-form II IIR step, not a lattice: with"
                  " b = +4, a = +8, and the two state buffers at +0xc and"
                  " +0x10, it forms y = b[0]*x + s[0] (the `vfma.f32 s0, s15,"
                  " s14` at 0xa32b6), then s'[i] = s[i+1] + b[i+1]*x -"
                  " a[i+1]*y for i below n-1 (the vfma/vfms pair at"
                  " 0xa32f8..0xa3302) and s'[n-2] = b[n-1]*x - a[n-1]*y with no"
                  " carried state, which is the last tap of the transposed form"
                  " (0xa32cc..0xa32e4). The `strd r12, r6, [r0, #12]` at"
                  " 0xa32e8 swaps the two buffers, so the ping-pong is where"
                  " the new state is written and nothing else, and the returned"
                  " s0 is y. Levinson-Durbin, a lattice and a Goertzel bank are"
                  " all refuted by it: there is no reflection coefficient, no"
                  " forward/backward error pair and no per-bin recurrence"),
    dict(address=0x742C0, name="iir_df1_step", kind="function",
         evidence="the direct-form I sibling, with the history kept in two"
                  " rings: the loop at 0x74366 accumulates num[i] * x[n-i] from"
                  " the array at +0x1c and subtracts den[i] * y[n-i] from the"
                  " one at +0x18, the tail adds num[0]*x and divides by den[0]"
                  " (0x742f6..0x74306), and the result is stored both to +0xc"
                  " and to the head of the output ring. 0x7434a is the"
                  " one-element shift of both rings and +8 is the warm-up mode"
                  " that decides how they are seeded"),
    dict(address=0x7AEE0, name="central_diff_step_i32", kind="function",
         rate="1786 calls in the 700 s HR run against 1786 of hr_algo_process_sample, one per sample",
         evidence="the same body as central_diff_step 0xa3256 -- x[n-1] at +0,"
                  " x[n-2] at +4, the first step emitting x[n] - x[n-1] and"
                  " every later one (x[n] - x[n-2]) * 0.5 (the `vmov.f32 s12,"
                  " #5.000000e-01` at 0x7af18) -- with the warm-up counter at"
                  " +8 held as a word rather than a halfword, so it is a second"
                  " copy of the routine in another translation unit and not"
                  " another operation"),
    dict(address=0xA2684, name="level_track_step", kind="function",
         rate="1786 calls in the 700 s HR run against 1786 of hr_algo_process_sample, one per sample",
         evidence="a deadband level tracker over {last input @0, level @4, seeded"
                  " @8}: d = x - last, and the level moves by d only when |d|"
                  " reaches the threshold argument (the `cmp r5, r2` and"
                  " `addge r0, r0, r4` at 0xa269e), both fields are stored back"
                  " with one `strd` (0xa26a4) and the return is x - level. An"
                  " accumulator that takes only jumps above a threshold and"
                  " returns the residue is what takes the LED-current steps out"
                  " of a raw PPG count, and it is the first thing"
                  " hr_chan_filter_step does to one"),
    dict(address=0x7AF28, name="window_zscore_step", kind="function",
         rate="1785 calls in the 700 s HR run against 1786 of hr_algo_process_sample, one per sample",
         calls=[0xA30FE, 0x743D8],
         evidence="window_f32_push, and once the window is full a"
                  " vec_f32_stddev over the whole of it (0x7af6c): the newest"
                  " sample divided by that deviation goes to +0x14 and the"
                  " whole window divided by it goes to the buffer at +0x1c"
                  " (0x7af96..0x7afb6), with the divisor forced to 1.0 when its"
                  " magnitude falls under the literal at 0x53fbc. Dividing a"
                  " window by its own standard deviation is one operation and"
                  " the byte at +0x10 is the only thing that gates it"),
    dict(address=0xA30B2, name="threshold_flag_step", kind="function",
         evidence="`cfg[0] > x` as a byte at +4 and nothing else (0xa30b8,"
                  " 0xa30c6); three instructions of comparison over one"
                  " configured constant"),

    # --- the vector primitives the chain adds to the ones already named ------
    dict(address=0x7AFC0, name="vec_f32_diff_energy", kind="function",
         rate="195 calls in the 700 s HR run against 65 of hr_algo_step, 3 per window",
         evidence="the sum of the squared k-th difference over the array, with"
                  " k taken as the third argument shifted right by one"
                  " (`asrs r2, r2, #1` at 0x7afc4): k = 0 accumulates x[i]^2,"
                  " k = 1 accumulates (x[i+1] - x[i])^2 (the `vsubeq` at"
                  " 0x7afec) and k = 2 accumulates (x[i+2] - 2*x[i+1] +"
                  " x[i])^2 (the `vfmsne` against 2.0 at 0x7afe8 and the"
                  " `vaddne` at 0x7aff0). Those three sums are the spectral"
                  " moments m0, m1 and m2"),
    dict(address=0x7B004, name="spectral_purity_index", kind="function",
         rate="65 calls in the 700 s HR run against 65 of hr_algo_step, once per window",
         calls=[0x7AFC0],
         evidence="m1^2 / (m0 * m2) from three vec_f32_diff_energy calls with"
                  " the orders 0, 4 and 2 (0x7b00c, 0x7b01a, 0x7b03a), refused"
                  " to a constant when |m0 * m2| falls under the literal at"
                  " 0x54050. The ratio of the squared first moment to the"
                  " product of the zeroth and second is one number with one"
                  " name, and hr_spectrum_step stores it beside the window's"
                  " standard deviation"),
    dict(address=0x7B3DC, name="vec_f32_convolve", kind="function",
         rate="195 calls in the 700 s HR run against 65 of hr_algo_step, 3 per window",
         evidence="a 1-D convolution with a centred kernel and a boundary mode:"
                  " the kernel half-width is the kernel length halved and"
                  " negated (0x7b3f4..0x7b3fe), each output accumulates"
                  " kernel[j] * x[i+j] by `vfma` (0x7b45c), and the mode byte"
                  " off the stack picks what happens past the edge -- 0 drops"
                  " the tap, 1 takes the first element (`vldr s15, [r1]` at"
                  " 0x7b450) and 2 takes the mirror (`vldr s15, [r8, #-4]` at"
                  " 0x7b46c) -- with anything above 2 refused at 0x7b3f2"),
    dict(address=0xA31C6, name="vec_f32_band_weight_powf", kind="function",
         rate="122 calls in the 700 s HR run against 1971 popped slots",
         calls=[0xA7CC6],
         evidence="out[i] = x[i] for every i outside [lo, hi] and x[i] *"
                  " powf(tab[2i], e) inside it (0xa3212..0xa3222), the table"
                  " strided by eight bytes (`lsl #3`); it refuses a negative"
                  " exponent (0xa31ee), a band wider than the array or a"
                  " length below twice the upper bound (0xa31dc, 0xa31e2)"),
    dict(address=0xA2B0A, name="vec_f32_weighted_stddev", kind="function",
         rate="130 calls in the 700 s HR run against 65 of hr_algo_step, 2 per window",
         calls=[0x74434, 0x9F594, 0x9F606, 0x8CA48],
         evidence="vec_f32_weighted_mean, then vec_f32_add_scalar of its"
                  " negation, then vec_f32_powf with the exponent 2, then"
                  " vec_f32_weighted_mean again and sqrtf -- the weighted"
                  " standard deviation written out one primitive per step"
                  " (0xa2b16..0xa2b50)"),

    # --- the integer and ring primitives the other algorithms share ---------
    dict(address=0x9EA3E, name="vec_i32_mean", kind="function",
         evidence="the sum of n words walked backwards from the end"
                  " (`ldr r4, [r1, #-4]!` at 0x9ea4c) divided by n with `sdiv`;"
                  " zero for n == 0. vec_f32_mean over integers"),
    dict(address=0x9EA5C, name="vec_i32_max", kind="function",
         rate="83 calls in the 700 s HR run against 1971 popped slots",
         evidence="x[0], then `movlt` on each element that runs higher"
                  " (0x9ea6e..0x9ea72); the integer vec_f32_max, and like it"
                  " it returns the value and not the index"),
    dict(address=0x9ECF2, name="delay_i32_push_pop", kind="function",
         evidence="the integer delay_f32_push_pop over {wrapped @0, head @4,"
                  " capacity @8, int * @0xc}: the head advances and, on"
                  " reaching the capacity, resets to zero with the wrapped flag"
                  " set by the one `strdeq` at 0x9ed02; then the slot's old"
                  " value is returned and the new one stored over it (0x9ed0a,"
                  " 0x9ed0e), so one call is both the push and the sample"
                  " leaving the line"),
    dict(address=0xA0312, name="window_pair_push", kind="function",
         evidence="window_f32_push with an eight-byte element and the fields in"
                  " delay_f32_push_pop's order: {capacity @0, head @4, void *"
                  " @8, full @0xc}. The head wraps to zero at capacity - 1"
                  " (0xa0328..0xa032c), the pair arrives in two registers and"
                  " is stored with one `stm` (0xa0336), and full is set the"
                  " first time the head lands on the last slot (0xa033c)"),
    dict(address=0xA02DC, name="ring3_prev_index", kind="function",
         evidence="the index at +8 less one, and 2 when it is already zero"
                  " (0xa02de..0xa02e6); the predecessor in a three-slot ring"
                  " and nothing else"),
    dict(address=0x9EE58, name="mat_u16_drop_first_row", kind="function",
         evidence="a row-major halfword matrix shifted up one row: for each of"
                  " the first rows - 1 rows it copies every column from the row"
                  " below (`ldrh.w r7, [r5, r1, lsl #1]` against"
                  " `strh r7, [r5], #2` at 0x9ee6c), with r1 the column count"
                  " and r2 the row count. Dropping the oldest row of a history"
                  " matrix is the whole body"),

    # --- motion detection, whose two rates the motion run separates ---------
    dict(address=0x9EC72, name="ewma_fixed_step", kind="function",
         evidence="the fixed-point sibling of ewma_step, which sits directly"
                  " after it at 0x9ecae: the 64-bit coefficient in r2:r3 is"
                  " negated into its own complement at 0x9ec74..0x9ec7a, and"
                  " the two 64-bit products state * alpha and x * (1 - alpha)"
                  " are summed with `umull`/`mla`/`adc` and rounded"
                  " (0x9ec86..0x9eca8). One coefficient, one state, one"
                  " sample"),
    dict(address=0xA073A, name="motion_energy_step", kind="function",
         calls=[0x9EC72, 0x9EDC8],
         evidence="the per-sample half of motion detection. Each axis is"
                  " smoothed by its own ewma_fixed_step at +4, +8 and +0xc"
                  " (0xa0762, 0xa0770, 0xa077e), and the distance between the"
                  " raw sample and the smoothed one is stored at +0x10 and"
                  " added to the 64-bit accumulator at +0x18 -- as the sum of"
                  " the three squared differences (0xa0796..0xa07a2) or, when"
                  " the byte at +0x2d is set, as the sum of their magnitudes"
                  " (0xa07b2 onwards). The halfword at +0 seeds the three"
                  " filters with the first sample instead of smoothing it"),
    dict(address=0xA071C, name="motion_window_decide", kind="function",
         calls=[0x9EDE8],
         evidence="the window half, and the only writer of the moving flag:"
                  " accum_i64_mean_reset drains the accumulator"
                  " motion_energy_step filled, the mean goes to +0x28 and +0x2c"
                  " becomes 1 when it is above the threshold argument and 0"
                  " when it is not (0xa0724..0xa0734)."
                  " motion_detection_algo_step calls it with 300 once every 25"
                  " samples, off the counter that wraps at 0x18 (0x2a00e,"
                  " 0x2a022), so the two rates are one per sample and one per"
                  " second of accelerometer at 25 Hz"),

    # --- the beat detector under ppg_heart_beats_algo_step ------------------
    dict(address=0x9EE42, name="ring_index_advance", kind="function",
         evidence="(i + step + capacity) modulo capacity, with the `blt` at"
                  " 0x9ee50 adding the capacity back until the remainder is"
                  " non-negative, which is what makes a negative step a step"
                  " backwards round the ring rather than an out-of-range index"),
    dict(address=0xA05AA, name="beat_peak_detect", kind="function",
         calls=[0x9EE42, 0xA86C2],
         evidence="the beat test, and the only writer of the beat position."
                  " Each call pushes the caller's four floats at +0x74..+0x80"
                  " into a 0x24-slot ring through ring_index_advance"
                  " (0xa05b8..0xa05d6), and once the fill at +0xa8 passes 8 it"
                  " copies the ring out in order and, for each of the four,"
                  " tests the middle element at index 16 against the 16 before"
                  " it and the 15 after it (the two counting loops at 0xa0622"
                  " and 0xa063a), declaring a beat only when more than 15 and"
                  " more than 14 of them run lower. The winner's channel goes"
                  " to +0x94 and its value to +0x98, and +0xa0 takes the"
                  " position as (channel + 0x10) + (samples - 9) * 4"
                  " (0xa0674..0xa067e), so the position counts in quarters of a"
                  " sample and the channel is its fractional part, which is"
                  " only consistent with the four values being four points in"
                  " time rather than four sensors; that much the arithmetic"
                  " forces and no more."
                  " ppg_heart_beats_algo_step calls it twice, negating those"
                  " four floats in between (0xa0554..0xa0584), so the second"
                  " call is the same detector finding troughs"),
    dict(address=0x76F4C, name="beat_rate_from_interval", kind="function",
         evidence="the gap between the beat position at +0xa0 and the previous"
                  " one at +0xa4, converted to a float and divided into the"
                  " constant at 0x4ff78, stored at +0xac (0x76f60..0x76f6e);"
                  " a constant over an interval is a rate, and it does nothing"
                  " when no beat has been seen"),

    # --- the distribution tracker the HR chain estimates a rate with ---------
    # Its object is one array of weights over one axis of values, multiplied by
    # a likelihood and renormalised every step, which is a distribution and not
    # a filter: +0 ready, +4 bin count, +8 the value axis, +0xc the weights,
    # +0x10 the estimator's window width, +0x14 the estimate, +0x18 the
    # weighted spread, +0x1c the peak weight, +0x20 the mean weight,
    # +0x24/+0x28/+0x2c three scratch arrays, +0xf4 the published estimate,
    # +0xf8 its valid flag and +0xf9 its quality.
    # spectrotrack_feed, spectrotrack_publish and spectrotrack_grade_quality
    # take the enclosing object instead, whose +0 is the config pointer and
    # whose +4 is this one, so their offsets run four higher throughout.
    dict(address=0xA2B54, name="spectrotrack_reset", kind="function",
         evidence="clears the ready byte at +0, fills the scratch array at +0x24"
                  " with 1/n (the `vdiv.f32 s15, s13, s14` at 0x7b78 region"
                  " 0xa2b78 and the store loop at 0xa2b82), copies it over the"
                  " weights at +0xc while zeroing +0x28 and +0x2c"
                  " (0xa2b98..0xa2bb4), and zeroes +0x14, +0x18, +0x1c and"
                  " +0x20. A uniform distribution and no estimate is the"
                  " tracker's initial state"),
    dict(address=0xA2BBE, name="spectrotrack_init", kind="function",
         calls=[0xA2B54],
         evidence="stores the bin count at +4, the axis and weight pointers at"
                  " +0xc, the three scratch arrays and the window width"
                  " (0xa2bd2..0xa2be2) and tail-calls spectrotrack_reset;"
                  " refuses when the window width exceeds the bin count"),
    dict(address=0x7B178, name="spectrotrack_posterior_update", kind="function",
         rate="130 calls in the 700 s HR run against 65 of hr_algo_step, 2 per window",
         calls=[0x7B3DC, 0x9F5AE, 0x9F5CC, 0x9F606],
         evidence="one multiplicative update of the weights: the likelihood is"
                  " built uniform over the first m bins and zero after"
                  " (0x7b1b8..0x7b1dc), smoothed by vec_f32_convolve, raised to"
                  " the exponent argument by vec_f32_powf after a"
                  " vec_f32_normalise_sum, multiplied into the weights by"
                  " vec_f32_mul and renormalised again (0x7b1f6..0x7b222), then"
                  " every weight is clamped into [the literal at 0x54284, 1.0]"
                  " (0x7b246..0x7b270). It refuses an exponent outside [0, 1]"
                  " (0x7b19a, 0x7b1a8), which is what makes it a temperature"),
    dict(address=0xA2C9E, name="spectrotrack_estimate_peak", kind="label",
         evidence="vec_f32_argmax over the weights, the axis value at that"
                  " index into +0x14 (0xa2cbc), vec_f32_max into +0x1c, the"
                  " weight at the peak into +0x20 and vec_f32_weighted_stddev"
                  " into +0x18. The mode-0 arm of spectrotrack_step; the"
                  " partition gave it no function of its own"),
    dict(address=0xA2BF4, name="spectrotrack_estimate_centroid", kind="label",
         evidence="the mode-1 arm: vec_f32_convolve smooths the weights,"
                  " vec_f32_argmax finds the peak, a window of +0x10 bins is"
                  " clipped around it (0xa2c20..0xa2c40) and"
                  " vec_f32_weighted_mean over that window's axis values gives"
                  " +0x14, with vec_f32_max and vec_f32_mean over the same"
                  " window into +0x1c and +0x20 and"
                  " vec_f32_weighted_stddev into +0x18. Interpolating the peak"
                  " against its neighbours is what separates it from the"
                  " mode-0 arm, which takes the bin itself"),
    dict(address=0xA2CE6, name="spectrotrack_step", kind="function",
         rate="130 calls in the 700 s HR run against 65 of hr_algo_step, 2 per window",
         calls=[0x7B178, 0xA2BF4, 0xA2C9E],
         evidence="spectrotrack_posterior_update and then one of the two"
                  " estimators on the mode byte off the stack -- 1 tail-calls"
                  " spectrotrack_estimate_centroid, 0 tail-calls"
                  " spectrotrack_estimate_peak, anything else is -2"
                  " (0xa2cf4..0xa2d0e)"),
    dict(address=0xA2D14, name="spectrotrack_publish", kind="function",
         rate="65 calls in the 700 s HR run against 65 of hr_algo_step, once per window",
         evidence="the only writer of the published pair, over the enclosing"
                  " object: +0xfc is 1 and +0xf8 takes the estimate at +0x18"
                  " when the peak weight at +0x20 is above the config's +0x10"
                  " and the spread at +0x1c is below its +0x14, and both are"
                  " zero otherwise (0xa2d26..0xa2d4c). A strong enough peak and"
                  " a tight enough distribution is what lets the estimate out,"
                  " and the two thresholds are the only reason either field"
                  " ever moves"),
    dict(address=0xA2D54, name="spectrotrack_grade_quality", kind="function",
         rate="65 calls in the 700 s HR run against 65 of hr_algo_step, once per window",
         evidence="the only writer of the enclosing object's +0xfd: `2 - valid`"
                  " when the sample is at or below the config's +0x18 and 0"
                  " when it is above (0xa2d62..0xa2d70). One byte, one"
                  " comparison against one configured constant"),
    dict(address=0xA2DDC, name="spectrotrack_feed", kind="function",
         rate="65 calls in the 700 s HR run against 65 of hr_algo_step, once per window",
         calls=[0xA2CE6, 0xA2D14, 0xA2D54],
         evidence="one sample through the enclosing object: spectrotrack_step"
                  " over the tracker at +4 with the config's exponent at +8 and"
                  " its bin count at +0xc converted to an integer (0xa2dee),"
                  " then spectrotrack_publish and spectrotrack_grade_quality,"
                  " both skipped when the step failed. It is named for the"
                  " published estimate, flag and quality it leaves behind"
                  " because it is three operations and no one of them"),

    # --- the HR chain, in the order the trace fixes -------------------------
    dict(address=0xA2848, name="hr_chan_filter_step", kind="function",
         rate="1786 calls in the 700 s HR run against 1786 of hr_algo_process_sample, one per sample",
         calls=[0xA2684, 0x7AEE0, 0x742C0, 0x7AF28],
         evidence="one PPG channel's front end and the only writer of that"
                  " channel's output pair: level_track_step over the raw count,"
                  " `vcvt.f32.s32` to a float, central_diff_step_i32, and once"
                  " that differentiator's warm-up counter at +0x14 has"
                  " saturated at 1 an iir_df1_step at +0x18 and a"
                  " window_zscore_step at +0x58 (0xa2852..0xa287c); when the"
                  " z-score's full byte at +0x68 is set it copies the scaled"
                  " sample at +0x6c into +0x20c and marks +0x210 ready"
                  " (0xa2880..0xa288c)"),
    dict(address=0xA2896, name="hr_channel_bank_step", kind="function",
         rate="1786 calls in the 700 s HR run against 1786 of hr_algo_process_sample, one per sample",
         calls=[0xA2848, 0x742C0],
         evidence="the channel bank and the only writer of the two signals the"
                  " rest of the chain runs on. On the mode at +4 it drives"
                  " either one hr_chan_filter_step with the frame's +8 or four"
                  " at the stride 0x214 with the frame's +0x44, +0x50, +0x2c"
                  " and +0x38 (0xa295c..0xa2978), and writes their mean --"
                  " the four outputs at +0x214, +0x428, +0x63c and +0x850 added"
                  " and multiplied by 0.25 (0xa2980..0xa29b2) -- to +0x8a8."
                  " +0x8ac takes the accelerometer arm: sqrt of the sum of the"
                  " three squared axes (0xa28f0..0xa290e) through an"
                  " iir_df1_step at +0x85c. The ready bytes of the four"
                  " channels are ANDed at 0xa29ba..0xa29d0"),
    dict(address=0xA2AE8, name="hr_spectrum_push_ppg", kind="function",
         rate="1785 calls in the 700 s HR run against 1786 of hr_algo_process_sample, one per sample",
         calls=[0xA30FE],
         evidence="window_f32_push of the pair's first word into the window at"
                  " +4 (0xa2aea, 0xa2aee); the pair hr_spectrum_step is handed"
                  " is hr_channel_bank_step's +0x8a8 and +0x8ac, so the first"
                  " word is the PPG channel mean"),
    dict(address=0xA2AF8, name="hr_spectrum_push_accel", kind="function",
         rate="1785 calls in the 700 s HR run against 1786 of hr_algo_process_sample, one per sample",
         calls=[0xA30FE],
         evidence="window_f32_push of the same pair's second word into the"
                  " window at +0x738 (0xa2afa, 0xa2afe), which is"
                  " hr_channel_bank_step's +0x8ac, the filtered accelerometer"
                  " magnitude"),
    dict(address=0x7B058, name="hr_spectrum_step", kind="function",
         rate="1686 calls in the 700 s HR run against 1971 popped slots",
         calls=[0xA2AE8, 0xA2AF8, 0xA3128, 0x7B004, 0x7A6E0, 0x7A73C,
                0x743D8, 0x9F5CC, 0xA31C6],
         evidence="pushes both windows every sample and, once the counter at"
                  " +0xc is a multiple of the plan's stride (0x7b08e..0x7b09e),"
                  " builds the two spectra: 200 samples out of the PPG window"
                  " (`movs r1, #0xc8`), spectral_purity_index into +0xea8,"
                  " spectrum_fft_input and spectrum_power_bins into the 257"
                  " bins at +0x334 normalised by vec_f32_normalise_sum, and"
                  " vec_f32_stddev of the raw window into +0xeac"
                  " (0x7b0a8..0x7b0f8); then the same transform over the"
                  " accelerometer window into +0xa68, floored at the literal at"
                  " 0x54174 and weighted by vec_f32_band_weight_powf"
                  " (0x7b10a..0x7b154). Two windows in, two normalised spectra"
                  " and two scalars out"),
    dict(address=0xA20C8, name="hr_algo_report_state", kind="function",
         rate="65 calls in the 700 s HR run against 65 of hr_algo_step, once per window",
         calls=[0x9B7A8, 0x9BF40],
         evidence="gathers a 0x34-byte record on its own stack out of fourteen"
                  " fields spread across the whole object -- the status at"
                  " +0x2c80, the tracker's published value and quality at"
                  " +0x1910 and +0x1915, the channel bank's outputs, the result"
                  " block's bpm and quality at +0x2ec4..+0x2ec7"
                  " (0xa20d4..0xa2156) -- and hands it to 0x9b7a8 under the"
                  " record id 0xba. hr_algo_process_sample calls it on every"
                  " error exit and once when the mode byte at +0x2c78 turns"
                  " over, and it writes nothing back"),
]

# What this run refuses, and why. A body whose arithmetic reads two ways is
# left unnamed on purpose: the module keeping an honest unnamed byte is worth
# more than a name that has to be un-learned. Listed here rather than in prose
# so that the next run over a better trace has the question in front of it.
REFUSED = [
    (0x7B320, "0xa2e38 builds 63 floats out of the tracker's published value"
              " and the body multiplies the accelerometer spectrum by them"
              " (vec_f32_mul over 0x3f at 0x7b396). A 63-entry table keyed by"
              " one frequency is a harmonic comb, a resampling of the axis or"
              " a per-bin gain curve, and the three are not separated by"
              " anything the body or the trace shows"),
    (0xA304E, "one spectrotrack_step over the tracker at +4 with the config"
              " chosen by an immediate 0, 1 or 2, then +0x18, +0x1c and +0x24"
              " copied to +0x244, +0x248 and +0x24c. It writes three fields"
              " and owns none of them: whether the three configs are three"
              " bands, three time scales or three candidate rates is not in"
              " the body"),
    (0xA2E38, "426 bytes of table build with `round` and a double convert"
              " reached only from 0x7b320; it cannot be read without reading"
              " that caller first"),
]


def load_analysis():
    with open(os.path.join(GHIDRA, "items.json")) as fh:
        items = json.load(fh)
    with open(os.path.join(GHIDRA, "references.json")) as fh:
        refs = json.load(fh)
    with open(os.path.join(GHIDRA, "modules.json")) as fh:
        modules = json.load(fh)["functions"]
    starts = dict((f["start"], f) for f in items["functions"])
    calls, pool = {}, {}
    for call in refs["calls"]:
        if call.get("function") is None:
            continue
        calls.setdefault(call["function"], set()).add(call["to"] & ~1)
    for word in refs["words"]:
        for reader in word["readers"]:
            pool.setdefault(reader, set()).add(word["value"])
    return starts, calls, pool, modules


def checked(rows):
    """Every row, with the claims it makes about the image still true.

    A name is worth no more than the reading behind it, so the callees and the
    pool words the evidence cites are re-measured against the analysis this run
    reads. A body that no longer calls what the reading says it calls is a
    different body, and naming it would be the stale claim winning.
    """
    starts, calls, pool, modules = load_analysis()
    partition = dict((a, v["module"]) for a, v in modules.items())
    out, complaints = [], []
    for row in rows:
        at = row["address"]
        fn = starts.get(at)
        if fn is None and row["kind"] != "label":
            complaints.append("0x%x is not a function start" % at)
            continue
        # An orphan label is code the partition gave no function of its own, so
        # the analysis attributes none of its calls to it; the evidence line
        # carries the reading and there is nothing here to re-measure.
        made = calls.get(at, set()) if fn else set(row.get("calls", ()))
        missing = [c for c in row.get("calls", ()) if c not in made]
        if missing:
            complaints.append("%s at 0x%x no longer calls %s"
                              % (row["name"], at,
                                 ", ".join("0x%x" % c for c in missing)))
            continue
        read = set()
        for site in range(at, fn["end"] if fn else at):
            read |= pool.get(site, set())
        absent = [v for v in row.get("reads", ()) if v not in read]
        if absent:
            complaints.append("%s at 0x%x no longer loads %s"
                              % (row["name"], at,
                                 ", ".join("0x%x" % v for v in absent)))
            continue
        evidence = row["evidence"]
        if row.get("rate"):
            evidence += ". " + row["rate"]
        out.append(dict(address=at, name=row["name"], kind=row["kind"],
                        **{"class": CLASS,
                           "module": partition.get("0x%x" % at, HEADER_MODULE),
                           "evidence": evidence}))
    return out, complaints


def main():
    rows, complaints = checked(ROWS)
    for line in complaints:
        print("abi/sensors.py: %s" % line, file=sys.stderr)
    if complaints:
        sys.exit("abi/sensors.py: %d reading(s) no longer hold" % len(complaints))
    try:
        added = symmap.load().rewrite(rows, {CLASS},
                                      verified=set(r["address"] for r in rows))
    except symmap.Refusal as err:
        sys.exit("abi/sensors.py: abi/symbols.yaml: %s" % err)
    print("%d sensor-algorithm names checked against the analysis;"
          " %d entries added" % (len(rows), added))
    print("%d addresses refused:" % len(REFUSED))
    for at, why in REFUSED:
        print("  0x%x: %s" % (at, why.split(".")[0]))


if __name__ == "__main__":
    main()
