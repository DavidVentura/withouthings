use crate::units::{Kilocalories, Metres, UnixTime, ACTIVITY_HUNDREDTHS};

pub const WALK: u16 = 1;
pub const RUN: u16 = 2;

const MIN_CADENCE_SPM: i64 = 50;

const RUN_CADENCE_SPM: i64 = 140;

const MAX_BREAK_SECS: i64 = 300;

const MIN_SESSION_SECS: i64 = 300;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Minute {
    pub at: UnixTime,
    pub duration_secs: i64,
    pub steps: Option<i64>,
    pub distance: Option<i64>,
    pub ascent: Option<i64>,
    pub descent: Option<i64>,
    pub calories: Option<i64>,
    pub met: Option<i64>,
    pub walk_level: Option<i64>,
    pub run_level: Option<i64>,
    pub reco_v1: Option<i64>,
    pub reco_v2: Option<i64>,
    pub sleep_level: Option<i64>,
}

/// The official app's decoder names these 0 awake, 1 REM, 2 light, 3 deep;
/// that mapping is wrong and using it silently swaps light and REM in every
/// derived sleep total.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SleepLevel {
    Awake,
    Light,
    Deep,
    Rem,
}

impl SleepLevel {
    pub fn from_wire(value: i64) -> Option<SleepLevel> {
        match value {
            0 => Some(SleepLevel::Awake),
            1 => Some(SleepLevel::Light),
            2 => Some(SleepLevel::Deep),
            3 => Some(SleepLevel::Rem),
            _ => None,
        }
    }
}

impl Minute {
    pub fn opened(at: UnixTime) -> Minute {
        Minute {
            at,
            duration_secs: 0,
            steps: None,
            distance: None,
            ascent: None,
            descent: None,
            calories: None,
            met: None,
            walk_level: None,
            run_level: None,
            reco_v1: None,
            reco_v2: None,
            sleep_level: None,
        }
    }

    pub fn ended_at(self) -> UnixTime {
        UnixTime(self.at.0 + self.duration_secs)
    }

    pub fn cadence(self) -> Option<i64> {
        let steps = self.steps?;
        if self.duration_secs <= 0 {
            return None;
        }
        Some(steps * 60 / self.duration_secs)
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Session {
    pub started_at: UnixTime,
    pub ended_at: UnixTime,
    pub subcategory: u16,
    pub steps: i64,
    pub distance: Metres,
    pub calories: Kilocalories,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Totals {
    pub steps: i64,
    pub distance: Metres,
    pub ascent: Metres,
    pub calories: Kilocalories,
}

pub fn totals(minutes: &[Minute]) -> Totals {
    let sum = |pick: fn(&Minute) -> Option<i64>| -> i64 { minutes.iter().filter_map(pick).sum() };
    Totals {
        steps: sum(|m| m.steps),
        distance: Metres(sum(|m| m.distance) as f64 / ACTIVITY_HUNDREDTHS),
        ascent: Metres(sum(|m| m.ascent) as f64 / ACTIVITY_HUNDREDTHS),
        calories: Kilocalories(sum(|m| m.calories) as f64 / ACTIVITY_HUNDREDTHS),
    }
}

pub fn detect(minutes: &[Minute]) -> Vec<Session> {
    let mut sessions = Vec::new();
    let mut open: Vec<Minute> = Vec::new();
    for minute in minutes {
        if minute.cadence().unwrap_or(0) < MIN_CADENCE_SPM {
            continue;
        }
        if let Some(last) = open.last() {
            if minute.at.0 - last.ended_at().0 > MAX_BREAK_SECS {
                sessions.extend(close(&open));
                open.clear();
            }
        }
        open.push(*minute);
    }
    sessions.extend(close(&open));
    sessions
}

fn close(minutes: &[Minute]) -> Option<Session> {
    let started_at = minutes.first()?.at;
    let ended_at = minutes.last()?.ended_at();
    if ended_at.0 - started_at.0 < MIN_SESSION_SECS {
        return None;
    }

    let summed = totals(minutes);
    let moving_secs: i64 = minutes.iter().map(|m| m.duration_secs).sum();
    let cadence = summed.steps * 60 / moving_secs.max(1);
    Some(Session {
        started_at,
        ended_at,
        subcategory: if cadence >= RUN_CADENCE_SPM {
            RUN
        } else {
            WALK
        },
        steps: summed.steps,
        distance: summed.distance,
        calories: summed.calories,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minute(at: i64, steps: i64) -> Minute {
        Minute {
            duration_secs: 60,
            steps: Some(steps),
            distance: Some(steps * 75),
            calories: Some(steps * 25),
            met: Some(400),
            ..Minute::opened(UnixTime(at))
        }
    }

    fn walk(from: i64, minutes: i64, steps: i64) -> Vec<Minute> {
        (0..minutes).map(|i| minute(from + i * 60, steps)).collect()
    }

    #[test]
    fn a_quarter_of_an_hour_of_walking_is_one_session() {
        let found = detect(&walk(1_000, 15, 90));
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].subcategory, WALK);
        assert_eq!(found[0].started_at, UnixTime(1_000));
        assert_eq!(found[0].ended_at, UnixTime(1_000 + 15 * 60));
        assert_eq!(found[0].steps, 15 * 90);
        assert_eq!(found[0].distance, Metres(15.0 * 90.0 * 0.75));
    }

    #[test]
    fn a_window_missing_a_column_is_skipped_rather_than_counted_as_zero() {
        let mut minutes = walk(1_000, 3, 90);
        minutes[0].ascent = Some(150);
        minutes[1].ascent = None;
        minutes[2].ascent = Some(450);

        let summed = totals(&minutes);
        assert_eq!(summed.steps, 270);
        assert_eq!(summed.ascent, Metres(6.0));
    }

    #[test]
    fn crossing_a_car_park_is_not_a_session() {
        assert!(detect(&walk(1_000, 3, 90)).is_empty());
    }

    #[test]
    fn a_walk_round_the_block_is_a_session() {
        let found = detect(&walk(1_000, 6, 95));
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].steps, 6 * 95);
    }

    #[test]
    fn pottering_about_the_house_is_not_a_session() {
        assert!(detect(&walk(1_000, 30, 30)).is_empty());
    }

    #[test]
    fn a_short_pause_does_not_split_a_session() {
        let mut minutes = walk(1_000, 8, 95);
        minutes.extend(walk(1_000 + 10 * 60, 8, 95));
        let found = detect(&minutes);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].ended_at, UnixTime(1_000 + 18 * 60));
    }

    #[test]
    fn a_long_break_ends_the_session() {
        let mut minutes = walk(1_000, 12, 95);
        minutes.extend(walk(1_000 + 3600, 12, 95));
        let found = detect(&minutes);
        assert_eq!(found.len(), 2);
        assert_eq!(found[0].started_at, UnixTime(1_000));
        assert_eq!(found[1].started_at, UnixTime(1_000 + 3600));
    }

    #[test]
    fn cadence_tells_a_run_from_a_walk() {
        let found = detect(&walk(1_000, 15, 165));
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].subcategory, RUN);
    }

    #[test]
    fn a_compressed_idle_window_breaks_the_session() {
        let mut minutes = walk(1_000, 12, 95);
        minutes.push(Minute {
            duration_secs: 900,
            ..Minute::opened(UnixTime(1_000 + 12 * 60))
        });
        minutes.extend(walk(1_000 + 27 * 60, 12, 95));
        assert_eq!(detect(&minutes).len(), 2);
    }

    #[test]
    fn a_window_without_a_duration_cannot_be_judged() {
        let minutes = vec![Minute {
            steps: Some(90),
            ..Minute::opened(UnixTime(1_000))
        }];
        assert!(detect(&minutes).is_empty());
    }
}
