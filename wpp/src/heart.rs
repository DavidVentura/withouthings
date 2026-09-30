use std::ops::Range;

use crate::energy::{Beat, LONGEST_HELD_MS};
use crate::units::Bpm;

/// As slow as the wearer's heart gets asleep. A waking body under it during a
/// session is the optical sensor having lost the wrist, not a rest between sets.
const DROPOUT_CEILING: Bpm = Bpm(60);

/// How far under the rest of the session a stretch must sit before it reads as
/// a lost signal. A session that is gentle throughout sits near the ceiling
/// all along, and its slow stretches are real.
const DROPOUT_MARGIN_BPM: u16 = 30;

/// The sensor flickers while it hunts for a pulse, so a reading or two clearing
/// the ceiling inside a dropout does not end it.
const DROPOUT_BRIDGE_MS: i64 = 30_000;

const RESTING_QUANTILE: f64 = 0.05;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Effort {
    pub median: Bpm,
    pub p95: Bpm,
    pub measured_ms: i64,
    pub dropout_ms: i64,
}

/// How many readings a median stands on is what says whether it can be read,
/// and that is the caller's to judge.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AtRest {
    pub median: Bpm,
    pub readings: u32,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Asleep {
    pub median: Bpm,
    pub resting: Bpm,
}

/// Quantiles are over time rather than over readings: the live stream and the
/// stored series describe the same session at 1 Hz and at half-minute steps,
/// and counting readings would let the denser one outvote the other.
pub fn effort(beats: &[Beat]) -> Option<Effort> {
    assert!(
        beats.is_sorted_by_key(|beat| beat.at.0),
        "beats out of order"
    );
    if beats.is_empty() {
        return None;
    }
    let held = held_ms(beats);
    let lost = dropouts(beats, &held);
    let kept: Vec<(Bpm, i64)> = beats
        .iter()
        .zip(&held)
        .zip(&lost)
        .filter(|(_, lost)| !**lost)
        .map(|((beat, held), _)| (beat.rate, *held))
        .collect();
    Some(Effort {
        median: weighted_quantile(&kept, 0.5)?,
        p95: weighted_quantile(&kept, 0.95)?,
        measured_ms: kept.iter().map(|(_, held)| held).sum(),
        dropout_ms: held
            .iter()
            .zip(&lost)
            .filter(|(_, lost)| **lost)
            .map(|(held, _)| held)
            .sum(),
    })
}

/// Asleep the watch measures every few minutes on its own schedule, so each
/// reading is one vote.
pub fn asleep(beats: &[Beat]) -> Option<Asleep> {
    let votes: Vec<(Bpm, i64)> = beats.iter().map(|beat| (beat.rate, 1)).collect();
    Some(Asleep {
        median: weighted_quantile(&votes, 0.5)?,
        resting: weighted_quantile(&votes, RESTING_QUANTILE)?,
    })
}

/// The typical rate of a waking body left alone: the readings outside every
/// stretch it was asleep, training or getting over having trained, one vote
/// each since the watch takes them on its own schedule.
pub fn at_rest(beats: &[Beat], busy: &[Range<i64>]) -> Option<AtRest> {
    let votes: Vec<(Bpm, i64)> = beats
        .iter()
        .filter(|beat| !busy.iter().any(|span| span.contains(&beat.at.0)))
        .map(|beat| (beat.rate, 1))
        .collect();
    Some(AtRest {
        median: weighted_quantile(&votes, 0.5)?,
        readings: votes.len() as u32,
    })
}

/// A reading stands until the next one. The last has nothing after it to say
/// how long it held, and a wider gap is the watch not measuring.
fn held_ms(beats: &[Beat]) -> Vec<i64> {
    beats
        .windows(2)
        .map(|pair| {
            let gap = pair[1].at.0 - pair[0].at.0;
            if gap <= LONGEST_HELD_MS {
                gap
            } else {
                0
            }
        })
        .chain(std::iter::once(0))
        .collect()
}

fn dropouts(beats: &[Beat], held: &[i64]) -> Vec<bool> {
    let runs = low_runs(beats);
    let mut low = vec![false; beats.len()];
    for run in &runs {
        low[run.clone()].fill(true);
    }
    let rest: Vec<(Bpm, i64)> = beats
        .iter()
        .zip(held)
        .zip(&low)
        .filter(|(_, low)| !**low)
        .map(|((beat, held), _)| (beat.rate, *held))
        .collect();
    let mut lost = vec![false; beats.len()];
    let Some(typical) = weighted_quantile(&rest, 0.5) else {
        return lost;
    };
    for run in runs {
        let votes: Vec<(Bpm, i64)> = beats[run.clone()]
            .iter()
            .map(|beat| (beat.rate, 1))
            .collect();
        let run_median = weighted_quantile(&votes, 0.5).expect("a run holds a reading");
        if typical.0.saturating_sub(run_median.0) >= DROPOUT_MARGIN_BPM {
            lost[run].fill(true);
        }
    }
    lost
}

fn low_runs(beats: &[Beat]) -> Vec<Range<usize>> {
    let mut runs: Vec<Range<usize>> = Vec::new();
    for (index, beat) in beats.iter().enumerate() {
        if beat.rate >= DROPOUT_CEILING {
            continue;
        }
        match runs.last_mut() {
            Some(run) if beat.at.0 - beats[run.end - 1].at.0 <= DROPOUT_BRIDGE_MS => {
                run.end = index + 1;
            }
            _ => runs.push(index..index + 1),
        }
    }
    runs
}

fn weighted_quantile(values: &[(Bpm, i64)], quantile: f64) -> Option<Bpm> {
    let total: i64 = values.iter().map(|(_, weight)| weight).sum();
    if total == 0 {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by_key(|(rate, _)| rate.0);
    let target = quantile * total as f64;
    let mut seen = 0;
    for (rate, weight) in &sorted {
        seen += weight;
        if seen as f64 >= target && *weight > 0 {
            return Some(*rate);
        }
    }
    unreachable!("the weights sum to the total")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::units::UnixMillis;

    fn trace(from_s: i64, every_s: i64, rates: &[u16]) -> Vec<Beat> {
        rates
            .iter()
            .enumerate()
            .map(|(index, rate)| Beat {
                at: UnixMillis((from_s + index as i64 * every_s) * 1000),
                rate: Bpm(*rate),
            })
            .collect()
    }

    #[test]
    fn a_steady_session_reads_its_own_rate() {
        let effort = effort(&trace(0, 1, &[120; 600])).unwrap();
        assert_eq!(effort.median, Bpm(120));
        assert_eq!(effort.p95, Bpm(120));
        assert_eq!(effort.measured_ms, 599_000);
        assert_eq!(effort.dropout_ms, 0);
    }

    #[test]
    fn a_lost_wrist_is_left_out_rather_than_averaged_in() {
        let beats = [
            trace(0, 1, &[130; 600]),
            trace(600, 1, &[42; 300]),
            trace(900, 1, &[140; 300]),
        ]
        .concat();
        let effort = effort(&beats).unwrap();
        assert_eq!(effort.median, Bpm(130));
        assert_eq!(effort.dropout_ms, 300_000);
        assert_eq!(effort.measured_ms, 899_000);
    }

    #[test]
    fn a_reading_clearing_the_ceiling_inside_a_dropout_does_not_end_it() {
        let beats = [
            trace(0, 1, &[130; 600]),
            trace(600, 1, &[45; 100]),
            trace(700, 1, &[64; 5]),
            trace(705, 1, &[45; 100]),
            trace(805, 1, &[130; 600]),
        ]
        .concat();
        assert_eq!(effort(&beats).unwrap().dropout_ms, 205_000);
    }

    #[test]
    fn slow_stretches_of_a_gentle_session_are_real() {
        let beats = [trace(0, 1, &[72; 600]), trace(600, 1, &[56; 120])].concat();
        let effort = effort(&beats).unwrap();
        assert_eq!(effort.dropout_ms, 0);
        assert_eq!(effort.measured_ms, 719_000);
    }

    #[test]
    fn a_session_all_under_the_ceiling_has_nothing_to_be_measured_against() {
        let effort = effort(&trace(0, 1, &[50; 300])).unwrap();
        assert_eq!(effort.median, Bpm(50));
        assert_eq!(effort.dropout_ms, 0);
    }

    #[test]
    fn half_minute_readings_weigh_as_much_time_as_the_live_stream() {
        let beats = [trace(0, 30, &[100; 20]), trace(600, 1, &[150; 901])].concat();
        assert_eq!(effort(&beats).unwrap().median, Bpm(150));
        let beats = [trace(0, 30, &[100; 21]), trace(630, 1, &[150; 300])].concat();
        assert_eq!(effort(&beats).unwrap().median, Bpm(100));
    }

    #[test]
    fn a_gap_in_measuring_counts_no_time() {
        let beats = [trace(0, 1, &[120; 60]), trace(3600, 1, &[120; 60])].concat();
        assert_eq!(effort(&beats).unwrap().measured_ms, 118_000);
    }

    #[test]
    fn nothing_measured_is_no_effort() {
        assert_eq!(effort(&[]), None);
        assert_eq!(effort(&trace(0, 1, &[120])), None);
    }

    #[test]
    fn a_waking_rate_leaves_out_what_the_body_was_busy_with() {
        let beats = [
            trace(0, 600, &[70, 72, 74]),
            trace(1800, 600, &[150, 140, 110]),
            trace(3600, 600, &[50, 52]),
        ]
        .concat();
        let busy = [1_800_000..3_600_000, 3_600_000..4_800_000];
        assert_eq!(
            at_rest(&beats, &busy),
            Some(AtRest {
                median: Bpm(72),
                readings: 3
            })
        );
        assert_eq!(at_rest(&beats, &[0..i64::MAX]), None);
    }

    #[test]
    fn a_night_reads_its_typical_and_its_lowest_rates() {
        let rates: Vec<u16> = (50..70).collect();
        let night = asleep(&trace(0, 600, &rates)).unwrap();
        assert_eq!(night.median, Bpm(59));
        assert_eq!(night.resting, Bpm(50));
        assert_eq!(asleep(&[]), None);
    }
}
