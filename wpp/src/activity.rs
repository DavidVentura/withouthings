//! The per-minute activity stream, and grouping it into sessions.
//!
//! `CMD_WAM_VASISTAS_GET` hands over one record per minute carrying the
//! counters the watch keeps itself: steps, distance, climb, earned calories,
//! MET, and the two features its activity classifier runs on.
//!
//! The official app turns those records into "Walk" and "Run" entries on the
//! phone, not on the watch: `WorkoutActivityRecognitionBuilder` feeds them to
//! `libactirec.so` with a per-user classifier downloaded from Withings. That
//! blob is not available here, so the sessions below are found from step
//! cadence instead, and will not agree with the official app on anything
//! subtler than walking and running.

use crate::units::{Kilocalories, Metres, UnixTime, ACTIVITY_HUNDREDTHS};

/// `ConstantsWs.WITHINGS_ACTIVITY_SUBCATEGORY_WALK`.
pub const WALK: u16 = 1;
/// `ConstantsWs.WITHINGS_ACTIVITY_SUBCATEGORY_RUN`.
pub const RUN: u16 = 2;

/// Steps per minute a window must hold to count as walking.
///
/// Cadence over a recorded day is bimodal: idle minutes sit at zero, walking
/// runs 85-112, and the thin band between is moving about indoors. Cutting at
/// 50 keeps whole walks together without letting a trip to the kitchen open a
/// session.
const MIN_CADENCE_SPM: i64 = 50;

const RUN_CADENCE_SPM: i64 = 140;

/// A pause longer than this ends the session rather than being absorbed into
/// it.
///
/// Five minutes, because a walk of three blocks, a wait, and three more blocks
/// is one walk: the watch reports that wait as a single idle window, and at
/// three minutes the two halves fell under [`MIN_SESSION_SECS`] and neither
/// was reported at all.
const MAX_BREAK_SECS: i64 = 300;

/// Shorter stretches are not reported. Walking this long is an activity;
/// walking two minutes is crossing a car park.
const MIN_SESSION_SECS: i64 = 600;

/// One record of the stream: what the watch counted over the window a
/// `WamVasistasHead` opened.
///
/// Every counter is optional because the watch sends only the objects it has
/// something to say about — an idle stretch arrives as a head and a duration
/// and nothing else, and it compresses several of them into one long window.
/// Values are as they came off the wire; [`crate::units`] holds the scales.
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
        }
    }

    pub fn ended_at(self) -> UnixTime {
        UnixTime(self.at.0 + self.duration_secs)
    }

    /// Steps per minute over the window, for windows that have one.
    pub fn cadence(self) -> Option<i64> {
        let steps = self.steps?;
        if self.duration_secs <= 0 {
            return None;
        }
        Some(steps * 60 / self.duration_secs)
    }
}

/// A stretch of walking or running, found in the stream.
///
/// Derived rather than stored, so unlike a [`Minute`] it carries converted
/// quantities: nothing will re-read it years from now against a corrected
/// scale.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Session {
    pub started_at: UnixTime,
    pub ended_at: UnixTime,
    /// `WITHINGS_ACTIVITY_SUBCATEGORY_*`, so it can be named alongside the
    /// workouts the watch reports itself.
    pub subcategory: u16,
    pub steps: i64,
    pub distance: Metres,
    pub calories: Kilocalories,
}

/// Sessions in `minutes`, which must be sorted by time.
///
/// Windows below the cadence cut are skipped rather than treated as breaks:
/// the watch omits a record entirely when nothing happened, so a session that
/// survives a standing pause has to survive a missing minute the same way.
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

    let steps: i64 = minutes.iter().filter_map(|m| m.steps).sum();
    let distance: i64 = minutes.iter().filter_map(|m| m.distance).sum();
    let calories: i64 = minutes.iter().filter_map(|m| m.calories).sum();
    let moving_secs: i64 = minutes.iter().map(|m| m.duration_secs).sum();
    // Cadence over the windows that were moving, not over the whole span: the
    // pauses inside a session would drag a run down into walking territory.
    let cadence = steps * 60 / moving_secs.max(1);
    Some(Session {
        started_at,
        ended_at,
        subcategory: if cadence >= RUN_CADENCE_SPM {
            RUN
        } else {
            WALK
        },
        steps,
        distance: Metres(distance as f64 / ACTIVITY_HUNDREDTHS),
        calories: Kilocalories(calories as f64 / ACTIVITY_HUNDREDTHS),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A minute of walking at `steps` per minute, `at` seconds in.
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
    fn crossing_a_car_park_is_not_a_session() {
        assert!(detect(&walk(1_000, 3, 90)).is_empty());
    }

    #[test]
    fn pottering_about_the_house_is_not_a_session() {
        assert!(detect(&walk(1_000, 30, 30)).is_empty());
    }

    /// The watch sends nothing at all for a window it counted no steps in, so
    /// a pause inside a walk is a hole in the series rather than a zero.
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

    /// Idle time arrives as one long window, not as a run of empty minutes.
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
