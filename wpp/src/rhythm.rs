use crate::analysis::detect_r_peaks;
use crate::units::{Bpm, Millis};

/// The watch logs the rate of a background measurement within this of the
/// start of the reading it keeps from it.
pub const WATCH_RATE_WITHIN_MS: i64 = 120_000;

/// Under this many intervals, a breathing wave is one or two cycles and cannot
/// be told from chance.
const MIN_INTERVALS: usize = 12;

/// The pulse sensor sometimes counts a beat twice, a fixed 350-400 ms after
/// the real one, which splits one interval into a short piece and the rest.
/// A real early beat this premature is rare enough to read as the split.
const SPLIT_PIECE_OF_BEAT: f64 = 0.65;

/// How close two pieces must add up to one beat at the watch's own rate to be
/// taken back together. Breathing alone moves an interval about this much.
const SPLIT_SUM_OF_BEAT: std::ops::RangeInclusive<f64> = 0.7..=1.3;

/// How far a reading's beat count may stray from the watch's rate for the same
/// measurement before neither is trusted.
const AGREEMENT: std::ops::RangeInclusive<f64> = 0.8..=1.25;

/// An early beat lands suddenly, unlike breathing, which shortens intervals
/// over several beats.
const EARLY_DROP_FROM_PREVIOUS: f64 = 0.85;
const EARLY_OF_LOCAL: f64 = 0.8;
const PAUSE_OF_LOCAL: f64 = 1.1;

/// The heart's own pacemaker keeps time through an early beat, so the early
/// interval and the pause after it together come close to two beats.
const EARLY_PAIR_OF_LOCAL: f64 = 1.6;

/// Beats either side of an interval it is compared against.
const LOCAL_BEATS: usize = 5;

/// Below this the variation is what a pulse sensor's own timing adds to a
/// steady heart.
const STEADY_VARIATION: f64 = 0.08;

/// Up to here, variation with no pattern to it is still within what the pulse
/// sensor's timing jitter produces.
const JITTER_VARIATION: f64 = 0.15;

/// Variation an irregular rhythm reaches, well over the jitter.
const IRREGULAR_VARIATION: f64 = 0.25;

/// The breathing rates a wave is looked for at.
const SLOWEST_BREATHS_PER_MINUTE: f64 = 6.0;
const FASTEST_BREATHS_PER_MINUTE: f64 = 30.0;

/// The heart samples the breathing wave once a beat, so a wave shorter than
/// this many beats cannot be told from beat-to-beat noise.
const FEWEST_BEATS_PER_BREATH: f64 = 2.5;

const BREATHING_RATES_TRIED: usize = 60;

/// Breathing quickens and slows from breath to breath, so a wave is fitted
/// with rates this far either side of its own as well, which lets it keep in
/// step over half a minute.
const BREATHING_DRIFT_PER_MINUTE: f64 = 1.5;

/// The best of many sine waves fits any intervals somewhat. The same intervals
/// shuffled out of order set the bar a real wave has to clear.
const SHUFFLES: usize = 200;
const BREATHING_CHANCE: f64 = 0.05;

/// A rhythm reads as breathing only when the wave accounts for most of how the
/// beats vary. Over many beats a weak wave beats chance while the rest of the
/// variation is something else.
const BREATHING_SHARE: f64 = 0.5;

const WAVE_CURVE_POINTS: usize = 120;

/// Six beats span more than a breath at resting rates, so a breathing wave
/// cannot keep six beats in a row apart from the next six.
const SHIFT_BEATS: usize = 6;

/// How far apart the two runs of beats must stay, as a share of the median
/// interval, for the rate to have moved rather than wandered.
const SHIFT_GAP: f64 = 0.1;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IntervalKind {
    Beat,
    Merged,
    Early,
    Pause,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Interval {
    pub length: Millis,
    pub kind: IntervalKind,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Wave {
    pub per_minute: f64,
    pub share: f64,
    pub chance: f64,
    pub curve: Vec<(Millis, Millis)>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pattern {
    Steady,
    Breathing,
    Variable,
    Irregular,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Shift {
    pub from: Bpm,
    pub to: Bpm,
    pub at: usize,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Rhythm {
    pub pattern: Pattern,
    pub wave: Option<Wave>,
    pub rate: Bpm,
    /// RMSSD over the median interval, with early beats and their pauses left
    /// out so a few of them do not read as an irregular rhythm.
    pub variation: f64,
    pub early_beats: usize,
}

#[derive(Debug, Clone, PartialEq)]
pub enum PulseVerdict {
    TooFew { intervals: usize },
    Disagrees { counted: Bpm, watch: Bpm },
    Shifted(Shift),
    Assessed(Rhythm),
}

#[derive(Debug, Clone, PartialEq)]
pub struct PulseReading {
    pub intervals: Vec<Interval>,
    pub verdict: PulseVerdict,
}

pub fn read_pulse(raw: &[Millis], watch: Bpm) -> PulseReading {
    let lengths: Vec<f64> = raw.iter().map(|m| m.0).filter(|ms| *ms > 0.0).collect();
    let merged = merge_split_beats(&lengths, 60_000.0 / watch.0.max(1) as f64);
    let values: Vec<f64> = merged.iter().map(|i| i.length.0).collect();
    if values.len() < MIN_INTERVALS {
        return PulseReading {
            intervals: merged,
            verdict: PulseVerdict::TooFew {
                intervals: values.len(),
            },
        };
    }

    let span: f64 = values.iter().sum();
    let counted = 60_000.0 * values.len() as f64 / span;
    if !AGREEMENT.contains(&(counted / watch.0.max(1) as f64)) {
        return PulseReading {
            intervals: merged,
            verdict: PulseVerdict::Disagrees {
                counted: Bpm(counted.round() as u16),
                watch,
            },
        };
    }

    let (rhythm, kinds) = match assess(&values) {
        Assessment::Shifted(shift) => {
            return PulseReading {
                intervals: merged,
                verdict: PulseVerdict::Shifted(shift),
            }
        }
        Assessment::Assessed(rhythm, kinds) => (rhythm, kinds),
    };
    let intervals = merged
        .into_iter()
        .zip(kinds)
        .map(|(interval, kind)| Interval {
            length: interval.length,
            kind: if interval.kind == IntervalKind::Merged {
                IntervalKind::Merged
            } else {
                kind
            },
        })
        .collect();
    PulseReading {
        intervals,
        verdict: PulseVerdict::Assessed(rhythm),
    }
}

/// Puts each short piece back with whichever neighbour makes the sum closest to
/// `beat_ms`, and leaves it alone when neither makes a plausible beat.
fn merge_split_beats(lengths: &[f64], beat_ms: f64) -> Vec<Interval> {
    let plausible = |sum: f64| SPLIT_SUM_OF_BEAT.contains(&(sum / beat_ms));
    let mut out: Vec<Interval> = Vec::with_capacity(lengths.len());
    let mut i = 0;
    while i < lengths.len() {
        let piece = lengths[i];
        if piece >= SPLIT_PIECE_OF_BEAT * beat_ms {
            out.push(Interval {
                length: Millis(piece),
                kind: IntervalKind::Beat,
            });
            i += 1;
            continue;
        }
        // The recording can stop partway into a beat, and a short last piece
        // reads the same as a split one; neither is a whole beat to keep.
        if i + 1 == lengths.len() {
            break;
        }
        let with_next = lengths
            .get(i + 1)
            .map(|next| piece + next)
            .filter(|s| plausible(*s));
        let with_previous = out
            .last()
            .filter(|last| last.kind == IntervalKind::Beat)
            .map(|last| last.length.0 + piece)
            .filter(|s| plausible(*s));
        let off = |sum: f64| (sum - beat_ms).abs();
        match (with_previous, with_next) {
            (Some(previous), next) if next.is_none_or(|next| off(previous) <= off(next)) => {
                *out.last_mut().unwrap() = Interval {
                    length: Millis(previous),
                    kind: IntervalKind::Merged,
                };
                i += 1;
            }
            (_, Some(next)) => {
                out.push(Interval {
                    length: Millis(next),
                    kind: IntervalKind::Merged,
                });
                i += 2;
            }
            (_, None) => {
                out.push(Interval {
                    length: Millis(piece),
                    kind: IntervalKind::Beat,
                });
                i += 1;
            }
        }
    }
    out
}

enum Assessment {
    Shifted(Shift),
    Assessed(Rhythm, Vec<IntervalKind>),
}

/// Classifies intervals already trusted to be one beat each, and marks the
/// early beats and their pauses. `lengths` holds at least [`MIN_INTERVALS`].
fn assess(lengths: &[f64]) -> Assessment {
    if let Some(shift) = shift(lengths) {
        return Assessment::Shifted(shift);
    }
    let kinds = early_beats(lengths);
    let ends: Vec<f64> = lengths
        .iter()
        .scan(0.0, |at, length| {
            *at += length;
            Some(*at)
        })
        .collect();
    let (times, underlying): (Vec<f64>, Vec<f64>) = ends
        .iter()
        .zip(lengths)
        .zip(&kinds)
        .filter(|(_, kind)| **kind == IntervalKind::Beat)
        .map(|((end, length), _)| (*end, *length))
        .unzip();
    let typical = median(&underlying);
    let rmssd = (underlying
        .windows(2)
        .map(|w| (w[1] - w[0]).powi(2))
        .sum::<f64>()
        / (underlying.len() - 1) as f64)
        .sqrt();
    let variation = rmssd / typical;

    let wave = breathing_wave(&times, &underlying, typical);
    let breathing = wave.as_ref().is_some_and(|w| w.share >= BREATHING_SHARE);
    let pattern = match () {
        _ if variation < STEADY_VARIATION => Pattern::Steady,
        _ if breathing => Pattern::Breathing,
        _ if variation < JITTER_VARIATION => Pattern::Steady,
        _ if variation >= IRREGULAR_VARIATION => Pattern::Irregular,
        _ => Pattern::Variable,
    };
    // Among intervals that vary at random, some pairs fall into the early-beat
    // shape by chance.
    let kinds = match pattern {
        Pattern::Irregular => vec![IntervalKind::Beat; lengths.len()],
        _ => kinds,
    };
    let early_beats = kinds.iter().filter(|k| **k == IntervalKind::Early).count();
    Assessment::Assessed(
        Rhythm {
            pattern,
            wave,
            rate: Bpm((60_000.0 / typical).round() as u16),
            variation,
            early_beats,
        },
        kinds,
    )
}

/// The widest split where every one of [`SHIFT_BEATS`] intervals before it
/// sits clear of every one of as many after it.
fn shift(lengths: &[f64]) -> Option<Shift> {
    let typical = median(lengths);
    let low = |run: &[f64]| run.iter().copied().fold(f64::MAX, f64::min);
    let high = |run: &[f64]| run.iter().copied().fold(f64::MIN, f64::max);
    (SHIFT_BEATS..=lengths.len().saturating_sub(SHIFT_BEATS))
        .map(|at| {
            let before = &lengths[at - SHIFT_BEATS..at];
            let after = &lengths[at..at + SHIFT_BEATS];
            (
                at,
                (low(after) - high(before)).max(low(before) - high(after)),
            )
        })
        .filter(|(_, gap)| *gap >= SHIFT_GAP * typical)
        .max_by(|a, b| a.1.partial_cmp(&b.1).unwrap())
        .map(|(at, _)| Shift {
            from: Bpm((60_000.0 / median(&lengths[at - SHIFT_BEATS..at])).round() as u16),
            to: Bpm((60_000.0 / median(&lengths[at..at + SHIFT_BEATS])).round() as u16),
            at,
        })
}

/// Looks for a breathing wave over the intervals, after a straight-line drift
/// is taken out, around each breathing rate the heart rate can carry, and keeps
/// it when it fits better than the same intervals shuffled do. `times` are when
/// each interval ended, in ms from the start of the reading.
fn breathing_wave(times: &[f64], lengths: &[f64], typical: f64) -> Option<Wave> {
    let seconds: Vec<f64> = times.iter().map(|t| t / 1000.0).collect();
    let (intercept, slope) = line(&seconds, lengths);
    let residual: Vec<f64> = seconds
        .iter()
        .zip(lengths)
        .map(|(t, y)| y - intercept - slope * t)
        .collect();

    let slowest = SLOWEST_BREATHS_PER_MINUTE + BREATHING_DRIFT_PER_MINUTE;
    let fastest = FASTEST_BREATHS_PER_MINUTE.min(60_000.0 / typical / FEWEST_BEATS_PER_BREATH)
        - BREATHING_DRIFT_PER_MINUTE;
    debug_assert!(fastest > slowest);
    let fits: Vec<BandFit> = (0..BREATHING_RATES_TRIED)
        .map(|k| {
            let per_minute =
                slowest + (fastest - slowest) * k as f64 / (BREATHING_RATES_TRIED - 1) as f64;
            BandFit::around(&seconds, per_minute)
        })
        .collect();
    let best_share = |values: &[f64]| {
        fits.iter()
            .map(|fit| fit.explained_share(values))
            .fold(0.0, f64::max)
    };
    let best = fits
        .iter()
        .max_by(|a, b| {
            a.explained_share(&residual)
                .partial_cmp(&b.explained_share(&residual))
                .unwrap()
        })
        .unwrap();
    let share = best.explained_share(&residual);

    let mut shuffled = residual.clone();
    let mut random = Lcg(0x5eed);
    let as_good = (0..SHUFFLES)
        .filter(|_| {
            random.shuffle(&mut shuffled);
            best_share(&shuffled) >= share
        })
        .count();
    let chance = (1 + as_good) as f64 / (1 + SHUFFLES) as f64;
    if chance > BREATHING_CHANCE {
        return None;
    }

    let coefficients = best.coefficients(&residual);
    let end = *times.last().unwrap();
    let curve = (0..WAVE_CURVE_POINTS)
        .map(|k| {
            let at = end * k as f64 / (WAVE_CURVE_POINTS - 1) as f64;
            let t = at / 1000.0;
            let wave: f64 = BandFit::columns_at(best.per_minute, t)
                .iter()
                .zip(&coefficients)
                .map(|(column, weight)| column * weight)
                .sum();
            (Millis(at), Millis(intercept + slope * t + wave))
        })
        .collect();
    // Six wave terms and the drift line fit some of any intervals; the share
    // is corrected for them, so it is what a wave explains beyond that.
    let n = lengths.len() as f64;
    let parameters = (BAND_COLUMNS + 2) as f64;
    Some(Wave {
        per_minute: best.per_minute,
        share: 1.0 - (1.0 - share) * (n - 1.0) / (n - parameters - 1.0),
        chance,
        curve,
    })
}

fn line(xs: &[f64], ys: &[f64]) -> (f64, f64) {
    let n = xs.len() as f64;
    let (mean_x, mean_y) = (xs.iter().sum::<f64>() / n, ys.iter().sum::<f64>() / n);
    let spread: f64 = xs.iter().map(|x| (x - mean_x).powi(2)).sum();
    let slope = xs
        .iter()
        .zip(ys)
        .map(|(x, y)| (x - mean_x) * (y - mean_y))
        .sum::<f64>()
        / spread;
    (mean_y - slope * mean_x, slope)
}

const BAND_COLUMNS: usize = 6;

/// Sines at a breathing rate and either side of it, at fixed times. Shuffling
/// only reorders the values, so the system they are solved through is built
/// once.
struct BandFit {
    per_minute: f64,
    columns: Vec<[f64; BAND_COLUMNS]>,
    inverse: [[f64; BAND_COLUMNS]; BAND_COLUMNS],
}

impl BandFit {
    fn columns_at(per_minute: f64, t: f64) -> [f64; BAND_COLUMNS] {
        let mut out = [0.0; BAND_COLUMNS];
        for (k, rate) in [
            per_minute - BREATHING_DRIFT_PER_MINUTE,
            per_minute,
            per_minute + BREATHING_DRIFT_PER_MINUTE,
        ]
        .iter()
        .enumerate()
        {
            let angle = 2.0 * std::f64::consts::PI * rate / 60.0 * t;
            out[2 * k] = angle.sin();
            out[2 * k + 1] = angle.cos();
        }
        out
    }

    fn around(seconds: &[f64], per_minute: f64) -> BandFit {
        let columns: Vec<[f64; BAND_COLUMNS]> = seconds
            .iter()
            .map(|t| BandFit::columns_at(per_minute, *t))
            .collect();
        let mut normal = [[0.0; BAND_COLUMNS]; BAND_COLUMNS];
        for row in &columns {
            for i in 0..BAND_COLUMNS {
                for j in 0..BAND_COLUMNS {
                    normal[i][j] += row[i] * row[j];
                }
            }
        }
        BandFit {
            per_minute,
            columns,
            inverse: invert(normal),
        }
    }

    fn coefficients(&self, values: &[f64]) -> [f64; BAND_COLUMNS] {
        let mut projected = [0.0; BAND_COLUMNS];
        for (row, value) in self.columns.iter().zip(values) {
            for i in 0..BAND_COLUMNS {
                projected[i] += row[i] * value;
            }
        }
        let mut out = [0.0; BAND_COLUMNS];
        for i in 0..BAND_COLUMNS {
            out[i] = (0..BAND_COLUMNS)
                .map(|j| self.inverse[i][j] * projected[j])
                .sum();
        }
        out
    }

    fn explained_share(&self, values: &[f64]) -> f64 {
        let total: f64 = values.iter().map(|v| v * v).sum();
        if total < f64::EPSILON {
            return 0.0;
        }
        let weights = self.coefficients(values);
        let residual: f64 = values
            .iter()
            .zip(&self.columns)
            .map(|(v, row)| {
                let fitted: f64 = row.iter().zip(&weights).map(|(c, w)| c * w).sum();
                (v - fitted).powi(2)
            })
            .sum();
        1.0 - residual / total
    }
}

/// Gauss-Jordan with partial pivoting. The sines at three distinct rates over
/// a dozen beats or more are never collinear.
fn invert(matrix: [[f64; BAND_COLUMNS]; BAND_COLUMNS]) -> [[f64; BAND_COLUMNS]; BAND_COLUMNS] {
    let mut a = matrix;
    let mut out = [[0.0; BAND_COLUMNS]; BAND_COLUMNS];
    for (i, row) in out.iter_mut().enumerate() {
        row[i] = 1.0;
    }
    for column in 0..BAND_COLUMNS {
        let pivot = (column..BAND_COLUMNS)
            .max_by(|x, y| {
                a[*x][column]
                    .abs()
                    .partial_cmp(&a[*y][column].abs())
                    .unwrap()
            })
            .unwrap();
        a.swap(column, pivot);
        out.swap(column, pivot);
        let lead = a[column][column];
        for k in 0..BAND_COLUMNS {
            a[column][k] /= lead;
            out[column][k] /= lead;
        }
        for row in 0..BAND_COLUMNS {
            if row == column {
                continue;
            }
            let factor = a[row][column];
            for k in 0..BAND_COLUMNS {
                a[row][k] -= factor * a[column][k];
                out[row][k] -= factor * out[column][k];
            }
        }
    }
    out
}

/// Fixed seed: the same reading must always read the same way.
struct Lcg(u64);

impl Lcg {
    fn below(&mut self, bound: usize) -> usize {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        ((self.0 >> 33) % bound as u64) as usize
    }

    fn shuffle(&mut self, values: &mut [f64]) {
        for i in (1..values.len()).rev() {
            values.swap(i, self.below(i + 1));
        }
    }
}

fn early_beats(lengths: &[f64]) -> Vec<IntervalKind> {
    let mut kinds = vec![IntervalKind::Beat; lengths.len()];
    let mut i = 1;
    while i + 1 < lengths.len() {
        let around: Vec<f64> = (i.saturating_sub(LOCAL_BEATS)
            ..(i + 1 + LOCAL_BEATS).min(lengths.len()))
            .filter(|k| *k != i && *k != i + 1)
            .map(|k| lengths[k])
            .collect();
        let local = median(&around);
        let (early, pause) = (lengths[i], lengths[i + 1]);
        if early < EARLY_DROP_FROM_PREVIOUS * lengths[i - 1]
            && early < EARLY_OF_LOCAL * local
            && pause > PAUSE_OF_LOCAL * local
            && early + pause > EARLY_PAIR_OF_LOCAL * local
        {
            kinds[i] = IntervalKind::Early;
            kinds[i + 1] = IntervalKind::Pause;
            i += 2;
            continue;
        }
        i += 1;
    }
    kinds
}

fn median(values: &[f64]) -> f64 {
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    sorted[sorted.len() / 2]
}

/// Room before the first R and after the last for the P wave and the QRS
/// windows.
const ECG_EDGE_BEFORE_SECS: f64 = 0.35;
const ECG_EDGE_AFTER_SECS: f64 = 0.25;

/// Windows around each R, in seconds from it, each with the stretch just
/// before it that sets its baseline.
const QRS_SECS: std::ops::Range<f64> = -0.06..0.08;
const QRS_BASELINE_SECS: std::ops::Range<f64> = -0.06..-0.04;

/// A P wave starts up to 200 ms before the QRS.
const P_SECS: std::ops::Range<f64> = -0.26..-0.06;
const P_BASELINE_SECS: std::ops::Range<f64> = -0.08..-0.06;

/// Two QRS complexes of the same origin match almost sample for sample.
const USUAL_SHAPE: f64 = 0.9;

const P_WAVE_MATCH: f64 = 0.6;

/// When the atria fibrillate there is no P wave to line up across beats, and
/// the median of the stretches before each beat comes out near flat.
const P_TEMPLATE_OF_QRS: f64 = 0.05;

/// More beats unlike the rest than an occasional ventricular beat explains:
/// what was counted is mostly movement on the electrodes.
const NOISY_UNUSUAL_SHARE: f64 = 0.15;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BeatShape {
    Usual,
    Unusual,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct EcgBeat {
    pub at: usize,
    pub shape: BeatShape,
    pub p_wave: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub enum EcgVerdict {
    TooFew { beats: usize },
    Noisy,
    Shifted(Shift),
    Assessed(Rhythm),
}

#[derive(Debug, Clone, PartialEq)]
pub struct EcgReading {
    pub beats: Vec<EcgBeat>,
    pub intervals: Vec<Interval>,
    pub verdict: EcgVerdict,
}

pub fn read_ecg(samples: &[i16], sampling_freq: u16) -> EcgReading {
    let freq = sampling_freq.max(1) as f64;
    let at = |secs: f64| (secs * freq) as usize;
    let peaks: Vec<usize> = detect_r_peaks(samples, sampling_freq)
        .indices
        .into_iter()
        .filter(|r| *r >= at(ECG_EDGE_BEFORE_SECS) && r + at(ECG_EDGE_AFTER_SECS) < samples.len())
        .collect();
    let lengths: Vec<f64> = peaks
        .windows(2)
        .map(|w| (w[1] - w[0]) as f64 * 1000.0 / freq)
        .collect();
    if lengths.len() < MIN_INTERVALS {
        return EcgReading {
            beats: peaks
                .iter()
                .map(|r| EcgBeat {
                    at: *r,
                    shape: BeatShape::Usual,
                    p_wave: false,
                })
                .collect(),
            intervals: plain(&lengths),
            verdict: EcgVerdict::TooFew { beats: peaks.len() },
        };
    }

    let window = |r: usize, secs: &std::ops::Range<f64>, baseline: &std::ops::Range<f64>| {
        let from = |offset: f64| (r as f64 + offset * freq).round() as usize;
        let mut base: Vec<i16> = samples[from(baseline.start)..from(baseline.end)].to_vec();
        base.sort_unstable();
        let level = base[base.len() / 2] as f64;
        samples[from(secs.start)..from(secs.end)]
            .iter()
            .map(|s| *s as f64 - level)
            .collect::<Vec<f64>>()
    };
    let qrs: Vec<Vec<f64>> = peaks
        .iter()
        .map(|r| window(*r, &QRS_SECS, &QRS_BASELINE_SECS))
        .collect();
    let p: Vec<Vec<f64>> = peaks
        .iter()
        .map(|r| window(*r, &P_SECS, &P_BASELINE_SECS))
        .collect();
    let qrs_template = template(&qrs);
    let p_template = template(&p);
    let p_template_present = span(&p_template) >= P_TEMPLATE_OF_QRS * span(&qrs_template);

    let beats: Vec<EcgBeat> = peaks
        .iter()
        .zip(qrs.iter().zip(&p))
        .map(|(r, (qrs, p))| EcgBeat {
            at: *r,
            shape: if correlation(qrs, &qrs_template) >= USUAL_SHAPE {
                BeatShape::Usual
            } else {
                BeatShape::Unusual
            },
            p_wave: p_template_present && correlation(p, &p_template) >= P_WAVE_MATCH,
        })
        .collect();
    let unusual = beats
        .iter()
        .filter(|b| b.shape == BeatShape::Unusual)
        .count();
    if unusual as f64 > NOISY_UNUSUAL_SHARE * beats.len() as f64 {
        return EcgReading {
            beats,
            intervals: plain(&lengths),
            verdict: EcgVerdict::Noisy,
        };
    }

    let (rhythm, kinds) = match assess(&lengths) {
        Assessment::Shifted(shift) => {
            return EcgReading {
                beats,
                intervals: plain(&lengths),
                verdict: EcgVerdict::Shifted(shift),
            }
        }
        Assessment::Assessed(rhythm, kinds) => (rhythm, kinds),
    };
    EcgReading {
        beats,
        intervals: lengths
            .iter()
            .zip(kinds)
            .map(|(length, kind)| Interval {
                length: Millis(*length),
                kind,
            })
            .collect(),
        verdict: EcgVerdict::Assessed(rhythm),
    }
}

impl EcgReading {
    pub fn early_beats(&self) -> impl Iterator<Item = &EcgBeat> {
        self.intervals
            .iter()
            .zip(self.beats.iter().skip(1))
            .filter(|(interval, _)| interval.kind == IntervalKind::Early)
            .map(|(_, beat)| beat)
    }
}

fn plain(lengths: &[f64]) -> Vec<Interval> {
    lengths
        .iter()
        .map(|length| Interval {
            length: Millis(*length),
            kind: IntervalKind::Beat,
        })
        .collect()
}

fn template(windows: &[Vec<f64>]) -> Vec<f64> {
    (0..windows[0].len())
        .map(|k| median(&windows.iter().map(|w| w[k]).collect::<Vec<f64>>()))
        .collect()
}

fn span(values: &[f64]) -> f64 {
    let high = values.iter().copied().fold(f64::MIN, f64::max);
    let low = values.iter().copied().fold(f64::MAX, f64::min);
    high - low
}

fn correlation(a: &[f64], b: &[f64]) -> f64 {
    let n = a.len() as f64;
    let (mean_a, mean_b) = (a.iter().sum::<f64>() / n, b.iter().sum::<f64>() / n);
    let (mut ab, mut aa, mut bb) = (0.0, 0.0, 0.0);
    for (x, y) in a.iter().zip(b) {
        ab += (x - mean_a) * (y - mean_b);
        aa += (x - mean_a).powi(2);
        bb += (y - mean_b).powi(2);
    }
    if aa < f64::EPSILON || bb < f64::EPSILON {
        return 0.0;
    }
    ab / (aa * bb).sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn intervals(ms: &[u32]) -> Vec<Millis> {
        ms.iter().map(|v| Millis(*v as f64)).collect()
    }

    fn assessed(reading: &PulseReading) -> Rhythm {
        match &reading.verdict {
            PulseVerdict::Assessed(rhythm) => rhythm.clone(),
            other => panic!("not assessed: {other:?}"),
        }
    }

    /// Deterministic noise in -1..1, so the tests do not need a random crate.
    struct Noise(u64);

    impl Noise {
        fn next(&mut self) -> f64 {
            self.0 = self
                .0
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (self.0 >> 11) as f64 / (1u64 << 53) as f64 * 2.0 - 1.0
        }
    }

    #[test]
    fn a_night_reading_that_swings_with_each_breath_is_breathing() {
        let raw = intervals(&[
            1180, 1120, 880, 1240, 1210, 1170, 900, 900, 1190, 1170, 1030, 930, 1220, 1220, 1020,
            820, 910, 1210, 1210, 920, 1220, 1230, 1190, 1090, 870, 1190,
        ]);
        let rhythm = assessed(&read_pulse(&raw, Bpm(54)));
        assert!(rhythm.pattern == Pattern::Breathing, "{rhythm:?}");
    }

    #[test]
    fn beats_the_pulse_sensor_counted_twice_are_taken_back_together() {
        let raw = intervals(&[
            380, 590, 980, 1120, 360, 650, 370, 660, 370, 710, 370, 650, 370, 550, 390, 500, 370,
            660, 360, 630, 360, 550, 340, 480, 370, 730, 370, 750, 360, 600, 350, 480, 370, 550,
            370, 630, 370, 650, 360, 550, 350, 570, 360, 660, 380, 580, 360, 460, 770, 790, 370,
            480, 830, 340, 480, 860, 380,
        ]);
        let reading = read_pulse(&raw, Bpm(61));
        let merged = reading
            .intervals
            .iter()
            .filter(|i| i.kind == IntervalKind::Merged)
            .count();
        assert!(merged >= 20, "{merged} merged");
        let rhythm = assessed(&reading);
        assert!((55..=70).contains(&rhythm.rate.0), "{rhythm:?}");
        assert_ne!(rhythm.pattern, Pattern::Irregular);
    }

    #[test]
    fn intervals_with_no_pattern_to_them_are_irregular() {
        let raw = intervals(&[
            740, 720, 830, 990, 1050, 600, 750, 1010, 830, 600, 1340, 920, 840, 800, 890, 820, 420,
            1130, 470, 540, 1090, 630, 740, 720, 870, 750, 860, 1040, 890, 830, 770, 1480, 390,
            810, 770,
        ]);
        let rhythm = assessed(&read_pulse(&raw, Bpm(70)));
        assert_eq!(rhythm.pattern, Pattern::Irregular);
        assert_eq!(rhythm.early_beats, 0);
    }

    #[test]
    fn a_reading_far_off_the_watchs_own_rate_is_not_assessed() {
        let raw = intervals(&[
            1310, 820, 420, 820, 470, 540, 440, 830, 740, 350, 400, 1100, 640, 460, 380, 590, 470,
            590, 430, 560, 430, 480, 640, 430, 400, 370, 480, 420, 520, 450, 670, 370, 670, 1220,
            390, 470, 650, 720, 630, 440, 360, 640, 540, 460, 510, 350, 450, 810, 520, 340, 600,
        ]);
        let reading = read_pulse(&raw, Bpm(200));
        assert!(
            matches!(reading.verdict, PulseVerdict::Disagrees { .. }),
            "{:?}",
            reading.verdict
        );
    }

    #[test]
    fn an_early_beat_and_its_pause_are_marked_in_a_steady_rhythm() {
        let mut ms = vec![1000; 20];
        ms[9] = 640;
        ms[10] = 1340;
        let reading = read_pulse(&intervals(&ms), Bpm(60));
        let rhythm = assessed(&reading);
        assert_eq!(rhythm.pattern, Pattern::Steady);
        assert_eq!(rhythm.early_beats, 1);
        assert_eq!(reading.intervals[9].kind, IntervalKind::Early);
        assert_eq!(reading.intervals[10].kind, IntervalKind::Pause);
    }

    #[test]
    fn a_reading_whose_rate_jumps_partway_is_not_assessed() {
        let raw = intervals(&[
            1060, 830, 740, 760, 750, 710, 680, 700, 1210, 1100, 1030, 1000, 890, 890, 800, 780,
            860, 1230, 1170, 1110, 860, 880, 910, 970, 920, 830, 940, 1030, 1080, 980, 860,
        ]);
        match read_pulse(&raw, Bpm(67)).verdict {
            PulseVerdict::Shifted(shift) => {
                assert_eq!(shift.at, 8);
                assert!(shift.from.0 > 80 && shift.to.0 < 65, "{shift:?}");
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn the_breathing_wave_passes_through_the_intervals_it_was_fitted_to() {
        let raw = intervals(&[
            1180, 1120, 880, 1240, 1210, 1170, 900, 900, 1190, 1170, 1030, 930, 1220, 1220, 1020,
            820, 910, 1210, 1210, 920, 1220, 1230, 1190, 1090, 870, 1190,
        ]);
        let rhythm = assessed(&read_pulse(&raw, Bpm(54)));
        assert_eq!(rhythm.pattern, Pattern::Breathing);
        let wave = rhythm.wave.unwrap();
        assert!(
            (10.0..=16.0).contains(&wave.per_minute),
            "{}",
            wave.per_minute
        );
        assert!(wave.chance <= BREATHING_CHANCE);
        let (low, high) = wave
            .curve
            .iter()
            .fold((f64::MAX, f64::MIN), |(lo, hi), (_, ms)| {
                (lo.min(ms.0), hi.max(ms.0))
            });
        assert!(
            low > 850.0 && high < 1300.0 && high - low > 150.0,
            "{low}..{high}"
        );
    }

    #[test]
    fn a_weak_wave_under_large_variation_does_not_make_it_breathing() {
        let raw = intervals(&[
            690, 420, 570, 900, 930, 680, 700, 620, 590, 770, 620, 710, 760, 720, 460, 480, 540,
            410, 790, 630, 790, 700, 610, 760, 640, 470, 710, 640, 820, 470, 710, 630, 790, 600,
            590, 460, 540, 770, 690, 1250, 430, 530, 420, 630, 620,
        ]);
        let rhythm = assessed(&read_pulse(&raw, Bpm(93)));
        assert_ne!(rhythm.pattern, Pattern::Breathing);
        if let Some(wave) = rhythm.wave {
            assert!(wave.share < BREATHING_SHARE);
        }
    }

    #[test]
    fn breathing_whose_pace_changes_from_breath_to_breath_is_breathing() {
        let raw = intervals(&[
            840, 1170, 1280, 1320, 1070, 1150, 1360, 1310, 1200, 880, 1250, 1380, 1320, 1290, 1010,
            1030, 1300, 1270, 860, 800, 1300, 1260, 870, 840, 1050, 340,
        ]);
        let reading = read_pulse(&raw, Bpm(51));
        assert_eq!(
            reading.intervals.len(),
            raw.len() - 1,
            "the cut-off last piece is dropped"
        );
        assert_eq!(assessed(&reading).pattern, Pattern::Breathing);
    }

    #[test]
    fn too_few_intervals_are_not_assessed() {
        let reading = read_pulse(&intervals(&[1000; 8]), Bpm(60));
        assert_eq!(reading.verdict, PulseVerdict::TooFew { intervals: 8 });
    }

    const HZ: u16 = 300;

    fn bump(t: f64, at: f64, height: f64, width: f64) -> f64 {
        height * (-((t - at) / width).powi(2) / 2.0).exp()
    }

    /// A single lead with a QRS at each of `beats` (seconds), a T wave nearly
    /// half its height after it, and a P wave before it when `p_waves`.
    fn ecg(beats: &[f64], p_waves: bool, noise: &mut Noise) -> Vec<i16> {
        let seconds = beats.last().unwrap() + 1.0;
        (0..(seconds * HZ as f64) as usize)
            .map(|i| {
                let t = i as f64 / HZ as f64;
                let wave: f64 = beats
                    .iter()
                    .map(|r| {
                        let p = if p_waves {
                            bump(t, r - 0.16, 50.0, 0.025)
                        } else {
                            0.0
                        };
                        p + bump(t, *r, 800.0, 0.012) + bump(t, r + 0.28, 350.0, 0.05)
                    })
                    .sum();
                (wave + 4.0 * noise.next()) as i16
            })
            .collect()
    }

    #[test]
    fn a_sinus_ecg_has_a_p_wave_before_every_beat_and_t_waves_are_not_beats() {
        let mut noise = Noise(1);
        let beats: Vec<f64> = (0..30)
            .scan(0.5, |at, k| {
                let now = *at;
                *at += 1.0 + 0.15 * (2.0 * std::f64::consts::PI * k as f64 / 5.0).sin();
                Some(now)
            })
            .collect();
        let reading = read_ecg(&ecg(&beats, true, &mut noise), HZ);
        assert_eq!(reading.beats.len(), beats.len());
        assert!(reading
            .beats
            .iter()
            .all(|b| b.p_wave && b.shape == BeatShape::Usual));
        match reading.verdict {
            EcgVerdict::Assessed(rhythm) => {
                assert!(rhythm.pattern == Pattern::Breathing, "{rhythm:?}")
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn an_ecg_with_random_intervals_and_no_p_waves_is_irregular() {
        let mut noise = Noise(7);
        let beats: Vec<f64> = (0..40)
            .scan(0.5, |at, _| {
                let now = *at;
                *at += 0.75 + 0.3 * noise.next();
                Some(now)
            })
            .collect();
        let reading = read_ecg(&ecg(&beats, false, &mut noise), HZ);
        assert!(reading.beats.iter().all(|b| !b.p_wave));
        match reading.verdict {
            EcgVerdict::Assessed(rhythm) => assert_eq!(rhythm.pattern, Pattern::Irregular),
            other => panic!("{other:?}"),
        }
    }
}
