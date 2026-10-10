use crate::units::{Bpm, Millis};

#[derive(Debug, Clone, PartialEq)]
pub struct RPeaks {
    pub indices: Vec<usize>,
    pub sampling_freq: u16,
}

impl RPeaks {
    pub fn rr_intervals(&self) -> Vec<Millis> {
        let freq = self.sampling_freq.max(1) as f64;
        self.indices
            .windows(2)
            .map(|w| Millis((w[1] - w[0]) as f64 * 1000.0 / freq))
            .collect()
    }

    pub fn heart_rate(&self) -> Option<Bpm> {
        let mut rates: Vec<f64> = self
            .rr_intervals()
            .iter()
            .filter(|ms| ms.0 > 0.0)
            .map(|ms| 60_000.0 / ms.0)
            .collect();
        if rates.is_empty() {
            return None;
        }
        rates.sort_by(|a, b| a.partial_cmp(b).unwrap());
        Some(Bpm(rates[rates.len() / 2].round() as u16))
    }

    pub fn rr_stddev(&self) -> Option<Millis> {
        let rr: Vec<f64> = self.rr_intervals().iter().map(|m| m.0).collect();
        if rr.len() < 2 {
            return None;
        }
        let mean = rr.iter().sum::<f64>() / rr.len() as f64;
        let var = rr.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / rr.len() as f64;
        Some(Millis(var.sqrt()))
    }
}

/// Squaring the slope lifts the QRS, the steepest edge in a beat, well clear of
/// a T wave that can stand more than half as tall but rises slowly.
const SLOPE_THRESHOLD: f64 = 0.12;

/// Taken over the slopes rather than the largest, so one spike from a moving
/// electrode does not raise the bar over every beat.
const SLOPE_REFERENCE_QUANTILE: f64 = 0.995;

/// 180 bpm. A T wave peaks some 250-300 ms after its R, so a shorter wait would
/// let a tall one count as a beat of its own.
const REFRACTORY_SECS: f64 = 0.33;

/// How far either side of a steep edge the R peak itself may sit.
const PEAK_SEARCH_SECS: f64 = 0.06;

/// The baseline an R is measured against, so an inverted complex is found as
/// readily as an upright one.
const BASELINE_SECS: f64 = 0.3;

pub fn detect_r_peaks(samples: &[i16], sampling_freq: u16) -> RPeaks {
    let freq = sampling_freq.max(1);
    let at = |secs: f64| (secs * freq as f64) as usize;
    let none = RPeaks {
        indices: Vec::new(),
        sampling_freq: freq,
    };
    if samples.len() < at(REFRACTORY_SECS) * 2 {
        return none;
    }

    let slopes: Vec<f64> = samples
        .windows(2)
        .map(|w| (w[1] as f64 - w[0] as f64).powi(2))
        .collect();
    let mut sorted = slopes.clone();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let reference = sorted[((sorted.len() - 1) as f64 * SLOPE_REFERENCE_QUANTILE) as usize];
    if reference == 0.0 {
        return none;
    }
    let threshold = reference * SLOPE_THRESHOLD;

    let search = at(PEAK_SEARCH_SECS).max(1);
    let mut indices: Vec<usize> = Vec::new();
    let mut i = 0;
    while i < slopes.len() {
        if slopes[i] < threshold {
            i += 1;
            continue;
        }
        let mut before: Vec<i16> = samples[i.saturating_sub(at(BASELINE_SECS))..=i].to_vec();
        before.sort_unstable();
        let baseline = before[before.len() / 2] as f64;
        let height = |k: usize| (samples[k] as f64 - baseline).abs();
        let to = (i + search).min(samples.len());
        let peak = (i.saturating_sub(search)..to)
            .max_by(|&a, &b| height(a).partial_cmp(&height(b)).unwrap())
            .unwrap();
        match indices.last_mut() {
            Some(last) if peak.abs_diff(*last) <= at(REFRACTORY_SECS) => {
                if height(peak) > height(*last) {
                    *last = peak;
                }
            }
            _ => indices.push(peak),
        }
        i = to;
    }
    RPeaks {
        indices,
        sampling_freq: freq,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn synthetic(bpm: f64, seconds: f64, freq: u16) -> Vec<i16> {
        let n = (seconds * freq as f64) as usize;
        let period = (60.0 / bpm * freq as f64) as usize;
        (0..n)
            .map(|i| match i % period {
                0 => 1500,
                1 => 900,
                2 => -400,
                _ => (i % 7) as i16 - 3,
            })
            .collect()
    }

    #[test]
    fn a_synthetic_rhythm_is_recovered() {
        let samples = synthetic(75.0, 30.0, 300);
        let peaks = detect_r_peaks(&samples, 300);
        assert_eq!(peaks.heart_rate(), Some(Bpm(75)));
        assert!(peaks.rr_stddev().unwrap().0 < 5.0);
    }

    #[test]
    fn rates_across_the_physiological_range_are_recovered() {
        for bpm in [45.0, 60.0, 100.0, 150.0] {
            let samples = synthetic(bpm, 30.0, 300);
            let got = detect_r_peaks(&samples, 300).heart_rate().unwrap();
            assert!(
                (got.0 as f64 - bpm).abs() <= 2.0,
                "expected ~{bpm} bpm, got {}",
                got.0
            );
        }
    }

    #[test]
    fn a_lead_too_short_to_hold_a_beat_yields_nothing() {
        let peaks = detect_r_peaks(&[0, 1, 2], 300);
        assert!(peaks.indices.is_empty());
        assert_eq!(peaks.heart_rate(), None);
    }
}
