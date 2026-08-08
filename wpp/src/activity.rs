use crate::units::{Kilocalories, Metres, UnixTime, ACTIVITY_HUNDREDTHS};
use std::ops::Range;

pub const WALK: u16 = 1;
pub const RUN: u16 = 2;

const WALKING_FLOOR_SPM: i64 = 30;

const RUN_CADENCE_SPM: i64 = 140;

const STILLNESS_BUDGET_SECS: i64 = 600;

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

/// Scaled by sixty so the floor can be charged per second without dividing.
/// A window with no duration earns nothing: steps over no time are not a pace.
fn earned(minute: &Minute) -> i64 {
    if minute.duration_secs <= 0 {
        return 0;
    }
    minute.steps.unwrap_or(0) * 60 - WALKING_FLOOR_SPM * minute.duration_secs
}

pub fn detect(minutes: &[Minute]) -> Vec<Session> {
    let mut sessions = Vec::new();
    let mut from = 0;
    while let Some(span) = next_span(minutes, from) {
        sessions.extend(close(&minutes[span.start..span.end]));
        from = span.end;
    }
    sessions
}

fn next_span(minutes: &[Minute], from: usize) -> Option<Range<usize>> {
    let start = from
        + minutes[from..]
            .iter()
            .position(|minute| earned(minute) > 0)?;

    // Stillness before the opening window belongs to whatever came before.
    let mut total = earned(&minutes[start]);
    let mut peak = total;
    let mut last_moving = start;
    for (i, minute) in minutes.iter().enumerate().skip(start + 1) {
        // Windows tile the day only while the watch has something to report; an
        // untiled gap is time spent standing still like any other.
        let still = (minute.at.0 - minutes[i - 1].ended_at().0).max(0);
        let credit = earned(minute) - WALKING_FLOOR_SPM * still;
        total += credit;
        if credit > 0 {
            last_moving = i;
        }
        peak = peak.max(total);
        if peak - total > WALKING_FLOOR_SPM * STILLNESS_BUDGET_SECS {
            break;
        }
    }
    // Trimmed to the last window of walking, leaving out the pause that ended it.
    Some(start..last_moving + 1)
}

fn close(minutes: &[Minute]) -> Option<Session> {
    let started_at = minutes.first()?.at;
    let ended_at = minutes.last()?.ended_at();
    let span_secs = ended_at.0 - started_at.0;
    if span_secs < MIN_SESSION_SECS {
        return None;
    }

    // The budget clears window by window, so the odd step every few minutes can
    // stitch together a stretch nobody walked.
    let summed = totals(minutes);
    if summed.steps * 60 < WALKING_FLOOR_SPM * span_secs {
        return None;
    }

    // Pauses would drag a run's cadence down into walking territory.
    let moving = minutes.iter().filter(|minute| earned(minute) > 0);
    let (moving_steps, moving_secs) = moving.fold((0, 0), |(steps, secs), minute| {
        (
            steps + minute.steps.unwrap_or(0),
            secs + minute.duration_secs,
        )
    });
    let cadence = moving_steps * 60 / moving_secs;

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

    fn idle(at: i64, secs: i64) -> Minute {
        Minute {
            duration_secs: secs,
            ..Minute::opened(UnixTime(at))
        }
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
    fn waiting_at_a_crossing_is_still_the_same_walk() {
        let mut minutes = walk(1_000, 15, 90);
        minutes.push(idle(1_900, 480));
        minutes.extend(walk(2_380, 15, 90));
        let found = detect(&minutes);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].started_at, UnixTime(1_000));
        assert_eq!(found[0].ended_at, UnixTime(3_280));
    }

    #[test]
    fn a_stop_the_walk_never_pays_back_ends_it() {
        let mut minutes = walk(1_000, 15, 90);
        minutes.push(idle(1_900, 720));
        minutes.extend(walk(2_620, 15, 90));
        let found = detect(&minutes);
        assert_eq!(found.len(), 2);
        assert_eq!(found[0].ended_at, UnixTime(1_900));
        assert_eq!(found[1].started_at, UnixTime(2_620));
    }

    #[test]
    fn the_steps_taken_during_a_pause_belong_to_the_walk() {
        let mut minutes = walk(1_000, 10, 90);
        minutes.push(minute(1_600, 20));
        minutes.extend(walk(1_660, 10, 90));
        let found = detect(&minutes);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].steps, 10 * 90 + 20 + 10 * 90);
    }

    #[test]
    fn a_walk_ends_where_the_walking_did_rather_than_where_the_data_did() {
        let mut minutes = walk(1_000, 10, 90);
        minutes.push(idle(1_600, 420));
        minutes.push(minute(2_020, 20));
        let found = detect(&minutes);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].ended_at, UnixTime(1_600));
    }

    #[test]
    fn an_afternoon_of_odd_errands_is_not_one_long_walk() {
        let minutes: Vec<Minute> = (0..12)
            .flat_map(|i| [minute(1_000 + i * 360, 90), idle(1_060 + i * 360, 300)])
            .collect();
        assert!(detect(&minutes).is_empty());
    }

    #[test]
    fn a_stretch_with_no_windows_at_all_is_time_spent_standing_still() {
        let mut minutes = walk(1_000, 10, 90);
        minutes.extend(walk(2_800, 10, 90));
        assert_eq!(detect(&minutes).len(), 2);
    }

    #[test]
    fn a_breather_does_not_demote_a_run_to_a_walk() {
        let mut minutes = walk(1_000, 10, 165);
        minutes.push(idle(1_600, 480));
        minutes.extend(walk(2_080, 10, 165));
        let found = detect(&minutes);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].subcategory, RUN);
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
