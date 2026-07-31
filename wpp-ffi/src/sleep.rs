//! Deriving a sleep period from heart rate.
//!
//! The watch stages sleep itself but has not been sending it; this is the
//! fallback, and it is weaker than what the watch would give. Step counts
//! cannot help — sitting still produces exactly the flat count that sleeping
//! does — so heart rate is the only signal that separates the two, and
//! anything lying motionless with a low rate reads as sleep whether or not it
//! is.

/// A run of samples low enough to be sleep.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Period {
    pub from_ms: i64,
    pub to_ms: i64,
}

/// Samples this far apart do not break a run: the watch comes off to charge,
/// and an hour on the cable is not an hour awake.
const MAX_GAP_MS: i64 = 90 * 60 * 1000;

/// Anything shorter is a nap, and more often a quiet sit.
const MIN_PERIOD_MS: i64 = 2 * 3600 * 1000;

/// How far the resting and waking levels must sit apart for the window to hold
/// a night at all.
const MIN_DROP_BPM: f64 = 8.0;

/// Where between resting and waking the cut falls.
///
/// Chosen against 24 recorded nights. Lower truncates: at 0.4 the night of
/// 28 Jul came out as five hours rather than the eight it was, because the
/// first hours of sleep sit only a few beats under the cut. Higher swallows the
/// evening before it.
const CUT: f64 = 0.6;

/// Window either side of a sample for the rolling median.
const SMOOTH_MS: i64 = 15 * 60 * 1000;

/// The longest stretch of heart rate low enough to be sleep, if there is one.
///
/// `points` must be sorted by time. `core_ms` is the middle of the night being
/// asked about: a period that does not cover it is an evening on the sofa or a
/// lie-in, both of which this otherwise reports as sleep on days when the watch
/// recorded no night at all.
pub fn detect(points: &[(i64, f64)], core_ms: i64) -> Option<Period> {
    let threshold = threshold(points)?;
    let smoothed = smooth(points);

    let mut best: Option<Period> = None;
    let mut open: Option<(i64, i64)> = None;
    for &(at, value) in &smoothed {
        match open {
            Some((start, last)) if value <= threshold && at - last <= MAX_GAP_MS => {
                open = Some((start, at));
            }
            Some((start, last)) => {
                best = longer(
                    best,
                    Period {
                        from_ms: start,
                        to_ms: last,
                    },
                );
                open = if value <= threshold {
                    Some((at, at))
                } else {
                    None
                };
            }
            None if value <= threshold => open = Some((at, at)),
            None => {}
        }
    }
    if let Some((start, last)) = open {
        best = longer(
            best,
            Period {
                from_ms: start,
                to_ms: last,
            },
        );
    }
    best.filter(|p| {
        p.to_ms - p.from_ms >= MIN_PERIOD_MS && p.from_ms <= core_ms && p.to_ms >= core_ms
    })
}

/// Where the window's own resting level ends and its waking level begins.
///
/// A fixed cutoff would need a resting rate this cannot know, so it comes from
/// the window: the 10th percentile stands for resting and the 75th for awake,
/// both far enough from the extremes to survive a dropped beat. A window whose
/// two ends are close together holds no night at all, and saying so is the only
/// honest answer — otherwise every quiet evening scores as sleep.
fn threshold(points: &[(i64, f64)]) -> Option<f64> {
    if points.is_empty() {
        return None;
    }
    let mut values: Vec<f64> = points.iter().map(|&(_, v)| v).collect();
    values.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let resting = values[values.len() / 10];
    let awake = values[values.len() * 3 / 4];
    if awake - resting < MIN_DROP_BPM {
        return None;
    }
    Some(resting + (awake - resting) * CUT)
}

/// Rolling median, so one dropped beat or one turn over cannot open or close a
/// period on its own.
fn smooth(points: &[(i64, f64)]) -> Vec<(i64, f64)> {
    points
        .iter()
        .map(|&(at, _)| {
            let mut around: Vec<f64> = points
                .iter()
                .filter(|(other, _)| (other - at).abs() <= SMOOTH_MS)
                .map(|&(_, v)| v)
                .collect();
            around.sort_by(|a, b| a.partial_cmp(b).unwrap());
            (at, around[around.len() / 2])
        })
        .collect()
}

fn longer(best: Option<Period>, candidate: Period) -> Option<Period> {
    match best {
        Some(b) if b.to_ms - b.from_ms >= candidate.to_ms - candidate.from_ms => Some(b),
        _ => Some(candidate),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINUTE: i64 = 60_000;

    /// Middle of the sleep every fixture below contains.
    const CORE: i64 = 200 * MINUTE;

    /// Heart rate every ten minutes: `evening` awake, then `night` asleep, then
    /// `morning` awake again.
    fn night(evening: &[f64], asleep: &[f64], morning: &[f64]) -> Vec<(i64, f64)> {
        evening
            .iter()
            .chain(asleep)
            .chain(morning)
            .enumerate()
            .map(|(i, &v)| (i as i64 * 10 * MINUTE, v))
            .collect()
    }

    #[test]
    fn finds_the_night_between_two_waking_stretches() {
        // Six samples awake at ~72, thirty asleep at ~52, six awake again:
        // the shape of the night measured on 27 Jul.
        let points = night(&[72.0; 6], &[52.0; 30], &[75.0; 6]);
        let found = detect(&points, CORE).expect("a night");
        assert_eq!(found.from_ms, 6 * 10 * MINUTE);
        assert_eq!(found.to_ms, 35 * 10 * MINUTE);
    }

    #[test]
    fn a_charging_gap_does_not_split_the_night() {
        let mut points = night(&[72.0; 6], &[52.0; 30], &[75.0; 6]);
        // An hour on the charger, in the middle of the night.
        points.retain(|&(at, _)| !(120 * MINUTE..180 * MINUTE).contains(&at));
        let found = detect(&points, CORE).expect("a night");
        assert_eq!(found.to_ms - found.from_ms, 29 * 10 * MINUTE);
    }

    #[test]
    fn a_quiet_evening_on_the_sofa_is_not_sleep() {
        // Awake and sedentary all window: nothing sits far enough below the
        // middle of it to be a night.
        let points = night(&[72.0; 6], &[70.0; 30], &[74.0; 6]);
        assert_eq!(detect(&points, CORE), None);
    }

    #[test]
    fn a_single_low_reading_is_not_a_night() {
        let mut points = night(&[72.0; 20], &[71.0; 20], &[73.0; 20]);
        points[30].1 = 45.0;
        assert_eq!(detect(&points, CORE), None);
    }

    #[test]
    fn a_nap_is_too_short_to_report() {
        // Ninety minutes low, well under the two-hour floor.
        let points = night(&[72.0; 20], &[52.0; 9], &[72.0; 20]);
        assert_eq!(detect(&points, CORE), None);
    }

    #[test]
    fn nothing_from_nothing() {
        assert_eq!(detect(&[], CORE), None);
    }
}
