use crate::activity::SleepLevel;

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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Score {
    pub total: u8,
    pub duration: u8,
    pub efficiency: u8,
    pub deep: u8,
    pub rem: u8,
    pub continuity: u8,
}

const HOUR_MS: f64 = 3_600_000.0;

const WEIGHT_EFFICIENCY: f64 = 0.20;
const WEIGHT_DEEP: f64 = 0.30;
const WEIGHT_REM: f64 = 0.35;
const WEIGHT_CONTINUITY: f64 = 0.15;

const QUALITY_SHARE: f64 = 0.75;

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
    let deep_share = ramp(deep as f64 / asleep as f64, 0.0, 0.16, 1.0, 1.01);
    let rem_share = ramp(rem as f64 / asleep as f64, 0.0, 0.22, 1.0, 1.01);
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

    #[test]
    fn ranks_nights_the_way_health_mate_does() {
        let nights = [
            (347, 0, 95, 103, 7, 80),
            (329, 55, 89, 4, 0, 84),
            (16, 33, 51, 9, 1, 31),
            (55, 32, 47, 13, 0, 28),
            (222, 42, 83, 11, 1, 70),
            (168, 10, 160, 65, 3, 56),
        ];
        let scored: Vec<(u8, u8)> = nights
            .iter()
            .map(|&(light, deep, rem, awake, wakings, theirs)| {
                let night = night_like(light, deep, rem, awake, wakings);
                (
                    score(&night).expect("a night with sleep scores").total,
                    theirs,
                )
            })
            .collect();
        for (i, (ours_a, theirs_a)) in scored.iter().enumerate() {
            for (ours_b, theirs_b) in &scored[i + 1..] {
                if theirs_a.abs_diff(*theirs_b) < 15 {
                    continue;
                }
                assert_eq!(
                    theirs_a > theirs_b,
                    ours_a > ours_b,
                    "ours {ours_a}/{ours_b} disagrees with theirs {theirs_a}/{theirs_b}",
                );
            }
        }
    }

    #[test]
    fn separates_two_nights_the_first_version_could_not() {
        let july_27 = score(&night_like(360, 147, 31, 31, 2)).unwrap();
        let july_28 = score(&night_like(373, 63, 10, 17, 2)).unwrap();
        assert!(
            july_27.total >= july_28.total + 5,
            "27 Jul {} should lead 28 Jul {} clearly",
            july_27.total,
            july_28.total,
        );
        assert!(
            july_28.total < 80,
            "a night that is 84% light with 2% REM is not an 80: {july_28:?}",
        );
    }

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
