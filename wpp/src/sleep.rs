//! Scoring a night from the watch's own staging.
//!
//! Not Withings' score. Theirs is computed server-side with an algorithm
//! version stamped on each night (`sleepScoreAlgoVersion` 17 at the time of
//! writing) and inputs we do not have; this is a transparent stand-in built
//! from the five things the staging alone can support. Every component is
//! reported next to the total so a number that looks wrong can be traced to
//! the part that produced it rather than argued with.
//!
//! The targets below are the conventional adult figures. They are a stated
//! opinion about what a good night looks like, not a measurement.

use crate::activity::SleepLevel;

/// One staged window, as the watch dated it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Band {
    pub from_ms: i64,
    pub to_ms: i64,
    pub level: SleepLevel,
}

impl Band {
    fn duration_ms(&self) -> i64 {
        (self.to_ms - self.from_ms).max(0)
    }
}

/// A night scored out of 100, with the parts that made it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Score {
    pub total: u8,
    /// Time asleep against the 7-9 h target.
    pub duration: u8,
    /// Time asleep as a share of time in bed.
    pub efficiency: u8,
    pub deep: u8,
    pub rem: u8,
    /// How broken up the night was, from the number of wakings after onset.
    pub continuity: u8,
}

const HOUR_MS: f64 = 3_600_000.0;

/// Weights over the four quality components, summing to 1.
const WEIGHT_EFFICIENCY: f64 = 0.35;
const WEIGHT_DEEP: f64 = 0.25;
const WEIGHT_REM: f64 = 0.20;
const WEIGHT_CONTINUITY: f64 = 0.20;

/// How much of the score quality can move, the rest being carried by duration
/// alone.
///
/// Duration multiplies rather than adds because no composition rescues a short
/// night: four perfect hours is not a good night's sleep, and a model that adds
/// duration to the others says it is. Health Mate's own score behaves the same
/// way — across eight scored nights its value tracks time asleep closely and
/// quality moves it by about 14 points at fixed duration.
const QUALITY_SHARE: f64 = 0.40;

/// A trapezoid: 0 at or below `zero_low`, full between `full_low` and
/// `full_high`, 0 at or above `zero_high`, straight lines between.
///
/// Preferred to a threshold because every one of these quantities is a
/// preference with a broad middle, and a step makes one minute either side of
/// an arbitrary cut worth several points.
fn ramp(value: f64, zero_low: f64, full_low: f64, full_high: f64, zero_high: f64) -> f64 {
    if value <= zero_low || value >= zero_high {
        return 0.0;
    }
    if value < full_low {
        return (value - zero_low) / (full_low - zero_low);
    }
    if value > full_high {
        return (zero_high - value) / (zero_high - full_high);
    }
    1.0
}

/// Wakings after the first sleep, which is what makes a night feel broken.
///
/// The awake stretch before sleep onset and the one that ends the night are
/// both excluded: going to bed early and lying there is not a waking, and
/// neither is getting up in the morning.
fn wakings(bands: &[Band]) -> u32 {
    let first_asleep = bands.iter().position(|b| b.level != SleepLevel::Awake);
    let last_asleep = bands.iter().rposition(|b| b.level != SleepLevel::Awake);
    let (Some(first), Some(last)) = (first_asleep, last_asleep) else {
        return 0;
    };
    let mut count = 0;
    let mut previous_awake = false;
    for band in &bands[first..=last] {
        let awake = band.level == SleepLevel::Awake;
        if awake && !previous_awake {
            count += 1;
        }
        previous_awake = awake;
    }
    count
}

/// `None` for a night with no sleep in it at all — a score of 0 would claim the
/// night was bad rather than absent.
pub fn score(bands: &[Band]) -> Option<Score> {
    let total_ms = |level: SleepLevel| -> i64 {
        bands
            .iter()
            .filter(|b| b.level == level)
            .map(Band::duration_ms)
            .sum()
    };

    let light = total_ms(SleepLevel::Light);
    let deep = total_ms(SleepLevel::Deep);
    let rem = total_ms(SleepLevel::Rem);
    let awake = total_ms(SleepLevel::Awake);
    let asleep = light + deep + rem;
    if asleep == 0 {
        return None;
    }
    let in_bed = asleep + awake;

    let hours = asleep as f64 / HOUR_MS;
    let duration = ramp(hours, 0.0, 7.5, 9.5, 14.0);
    let efficiency = ramp(asleep as f64 / in_bed as f64, 0.55, 0.88, 1.0, 1.01);
    // Shares of time asleep, which is how the conventional figures are quoted.
    let deep_share = ramp(deep as f64 / asleep as f64, 0.0, 0.13, 0.23, 0.45);
    let rem_share = ramp(rem as f64 / asleep as f64, 0.0, 0.18, 0.28, 0.50);
    let continuity = ramp(wakings(bands) as f64, -1.0, 0.0, 1.0, 9.0);

    let quality = efficiency * WEIGHT_EFFICIENCY
        + deep_share * WEIGHT_DEEP
        + rem_share * WEIGHT_REM
        + continuity * WEIGHT_CONTINUITY;
    let total = 100.0 * duration * (1.0 - QUALITY_SHARE + QUALITY_SHARE * quality);

    Some(Score {
        total: total.round() as u8,
        duration: (duration * 100.0).round() as u8,
        efficiency: (efficiency * 100.0).round() as u8,
        deep: (deep_share * 100.0).round() as u8,
        rem: (rem_share * 100.0).round() as u8,
        continuity: (continuity * 100.0).round() as u8,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn band(from_min: i64, minutes: i64, level: SleepLevel) -> Band {
        Band {
            from_ms: from_min * 60_000,
            to_ms: (from_min + minutes) * 60_000,
            level,
        }
    }

    /// 8 h asleep, well split, one waking: everything in its target band.
    #[test]
    fn a_good_night_scores_near_full() {
        let night = vec![
            band(0, 240, SleepLevel::Light),
            band(240, 10, SleepLevel::Awake),
            band(250, 70, SleepLevel::Deep),
            band(320, 160, SleepLevel::Rem),
        ];
        let score = score(&night).unwrap();
        assert_eq!(score.duration, 100);
        assert!(score.total >= 85, "{score:?}");
    }

    #[test]
    fn a_night_with_no_sleep_has_no_score() {
        assert_eq!(score(&[band(0, 300, SleepLevel::Awake)]), None);
        assert_eq!(score(&[]), None);
    }

    /// Four hours of sleep cannot reach a good score however clean it is.
    #[test]
    fn a_short_night_is_capped_by_its_duration() {
        let night = vec![
            band(0, 130, SleepLevel::Light),
            band(130, 50, SleepLevel::Deep),
            band(180, 60, SleepLevel::Rem),
        ];
        let score = score(&night).unwrap();
        assert_eq!(score.efficiency, 100, "unbroken, so efficiency is perfect");
        assert!(
            score.total < 60,
            "perfect composition must not rescue four hours: {score:?}"
        );
    }

    /// A night laid out to match one Health Mate scored, so the two can be
    /// compared. Stages are emitted in whole minutes with the wakings spread
    /// through the sleep; the totals are what the score reads, not the order.
    fn night_like(
        light_min: i64,
        deep_min: i64,
        rem_min: i64,
        awake_min: i64,
        wakings: i64,
    ) -> Vec<Band> {
        let mut out = Vec::new();
        let mut at = 0;
        let mut push = |out: &mut Vec<Band>, at: &mut i64, minutes: i64, level| {
            if minutes > 0 {
                out.push(band(*at, minutes, level));
                *at += minutes;
            }
        };
        // One waking splits the sleep into two, so N wakings need N+1 blocks.
        let blocks = (wakings + 1).max(1);
        for i in 0..blocks {
            let share = |total: i64| total / blocks + if i == 0 { total % blocks } else { 0 };
            push(&mut out, &mut at, share(light_min), SleepLevel::Light);
            push(&mut out, &mut at, share(deep_min), SleepLevel::Deep);
            push(&mut out, &mut at, share(rem_min), SleepLevel::Rem);
            if i < blocks - 1 && wakings > 0 {
                push(
                    &mut out,
                    &mut at,
                    (awake_min / wakings).max(1),
                    SleepLevel::Awake,
                );
            }
        }
        out
    }

    /// Health Mate's own scores for eight nights off this watch, read out of
    /// `Track.dataJson` in `room-healthmate.db`, against ours for the same
    /// stage totals.
    ///
    /// Not a correctness proof — theirs is a different algorithm with inputs we
    /// do not have, and it floors naps at 20 where ours reports them low. It is
    /// a guard that the shape stays right: a full night must not come out as a
    /// nap, and quality must not swamp duration.
    #[test]
    fn tracks_health_mates_score_on_real_nights() {
        // light, deep, rem, awake (minutes), wakings, Health Mate's score
        let nights = [
            (347, 0, 95, 103, 7, 80),
            (329, 55, 89, 4, 0, 84),
            (16, 33, 51, 9, 1, 31),
            (55, 32, 47, 13, 0, 28),
            (222, 42, 83, 11, 1, 70),
            (168, 10, 160, 65, 3, 56),
        ];
        for (light, deep, rem, awake, wakings, theirs) in nights {
            let night = night_like(light, deep, rem, awake, wakings);
            let ours = score(&night).expect("a night with sleep in it scores");
            let gap = (ours.total as i32 - theirs as i32).abs();
            assert!(
                gap <= 15,
                "{light}/{deep}/{rem}/{awake} {wakings} wakings: ours {} vs theirs {theirs}",
                ours.total,
            );
        }
    }

    /// The same sleep, broken into many pieces, must score below the whole.
    #[test]
    fn fragmentation_costs_continuity_and_efficiency() {
        let whole = vec![
            band(0, 240, SleepLevel::Light),
            band(240, 70, SleepLevel::Deep),
            band(310, 130, SleepLevel::Rem),
        ];
        let broken: Vec<Band> = (0..8)
            .flat_map(|i| {
                [
                    band(i * 60, 30, SleepLevel::Light),
                    band(i * 60 + 30, 10, SleepLevel::Deep),
                    band(i * 60 + 40, 5, SleepLevel::Rem),
                    band(i * 60 + 45, 15, SleepLevel::Awake),
                ]
            })
            .collect();
        let whole = score(&whole).unwrap();
        let broken = score(&broken).unwrap();
        assert!(broken.continuity < whole.continuity, "{broken:?}");
        assert!(broken.total < whole.total, "{broken:?} vs {whole:?}");
    }

    /// Lying awake before falling asleep is not a waking, and neither is
    /// getting up: only the interruptions between count.
    #[test]
    fn wakings_ignore_the_ends_of_the_night() {
        let night = vec![
            band(0, 20, SleepLevel::Awake),
            band(20, 100, SleepLevel::Light),
            band(120, 15, SleepLevel::Awake),
            band(135, 100, SleepLevel::Deep),
            band(235, 30, SleepLevel::Awake),
        ];
        assert_eq!(wakings(&night), 1);
    }

    #[test]
    fn a_run_of_awake_windows_is_one_waking() {
        let night = vec![
            band(0, 100, SleepLevel::Light),
            band(100, 10, SleepLevel::Awake),
            band(110, 10, SleepLevel::Awake),
            band(120, 100, SleepLevel::Deep),
        ];
        assert_eq!(wakings(&night), 1);
    }
}
