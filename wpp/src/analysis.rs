//! Heart rate from an ECG lead.
//!
//! A Pan-Tompkins style detector: differentiate to emphasise the QRS slope,
//! square, integrate over a QRS-width window, then take peaks above an
//! adaptive threshold with a physiological refractory period.

use crate::units::{Bpm, Millis};

/// R-wave positions found in a lead, with the rate they imply.
#[derive(Debug, Clone, PartialEq)]
pub struct RPeaks {
    pub indices: Vec<usize>,
    pub sampling_freq: u16,
}

impl RPeaks {
    /// Intervals between successive R waves.
    pub fn rr_intervals(&self) -> Vec<Millis> {
        let freq = self.sampling_freq.max(1) as f64;
        self.indices
            .windows(2)
            .map(|w| Millis((w[1] - w[0]) as f64 * 1000.0 / freq))
            .collect()
    }

    /// Median instantaneous rate. The median rather than the mean so that a
    /// single missed or doubled beat does not drag the answer.
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

    /// Spread of the RR intervals; a large value means the detector is
    /// probably missing or doubling beats rather than that the rhythm varies.
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

/// Emphasise the QRS complex: 5-point derivative, squared, then integrated
/// over a window about as wide as a QRS.
fn energy(samples: &[i16], sampling_freq: u16) -> Vec<f64> {
    let derivative: Vec<f64> = (0..samples.len())
        .map(|i| {
            let at = |k: usize| samples[i.saturating_sub(k)] as f64;
            (2.0 * samples[i] as f64 + at(1) - at(3) - 2.0 * at(4)) / 8.0
        })
        .collect();

    let window = ((sampling_freq as f64 * 0.150) as usize).max(1);
    let mut integrated = Vec::with_capacity(derivative.len());
    let mut running = 0.0;
    for i in 0..derivative.len() {
        running += derivative[i] * derivative[i];
        if i >= window {
            running -= derivative[i - window] * derivative[i - window];
        }
        integrated.push(running / window as f64);
    }
    integrated
}

/// Locate R waves in a single lead.
///
/// Returns an empty result rather than guessing when the lead is too short to
/// hold a beat.
pub fn detect_r_peaks(samples: &[i16], sampling_freq: u16) -> RPeaks {
    let freq = sampling_freq.max(1);
    let refractory = (freq as f64 * 0.20) as usize; // 200 ms; 300 bpm ceiling
    if samples.len() < refractory * 2 {
        return RPeaks {
            indices: Vec::new(),
            sampling_freq: freq,
        };
    }

    let integrated = energy(samples, freq);
    let mut sorted: Vec<f64> = integrated.iter().copied().filter(|v| *v > 0.0).collect();
    if sorted.is_empty() {
        return RPeaks {
            indices: Vec::new(),
            sampling_freq: freq,
        };
    }
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    // QRS energy sits far above the bulk of the signal.
    let threshold = sorted[sorted.len() * 9 / 10] * 0.5;

    let mut indices: Vec<usize> = Vec::new();
    let mut i = 1;
    while i < integrated.len() - 1 {
        if integrated[i] < threshold {
            i += 1;
            continue;
        }
        // Take the highest point of this excursion, then step past the
        // refractory period so one QRS yields one peak.
        let start = i;
        let mut best = i;
        while i < integrated.len() && integrated[i] >= threshold {
            if integrated[i] > integrated[best] {
                best = i;
            }
            i += 1;
        }
        if i == start {
            i += 1;
        }
        // The integration window delays the energy peak; walk back to the
        // steepest point of the raw lead.
        let window = ((freq as f64 * 0.150) as usize).max(1);
        let from = best.saturating_sub(window);
        let peak = (from..=best.min(samples.len() - 1))
            .max_by_key(|&k| samples[k])
            .unwrap_or(best);
        if indices.last().is_none_or(|&last| peak - last >= refractory) {
            indices.push(peak);
        }
    }
    RPeaks {
        indices,
        sampling_freq: freq,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A synthetic 75 bpm rhythm: one narrow spike every 0.8 s at 300 Hz.
    fn synthetic(bpm: f64, seconds: f64, freq: u16) -> Vec<i16> {
        let n = (seconds * freq as f64) as usize;
        let period = (60.0 / bpm * freq as f64) as usize;
        (0..n)
            .map(|i| match i % period {
                0 => 1500,
                1 => 900,
                2 => -400,
                _ => (i % 7) as i16 - 3, // low-level noise
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
