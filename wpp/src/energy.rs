use crate::client::UserProfile;
use crate::units::{
    Bpm, Centimetres, Kilocalories, Kilograms, Met, MetresPerSecond, UnixMillis, UnixTime, Years,
};

const KJ_PER_KCAL: f64 = 4.184;

const ML_OXYGEN_PER_MET_MINUTE: f64 = 3.5;

const KCAL_PER_ML_OXYGEN: f64 = 0.005;

const MINUTES_PER_DAY: f64 = 1440.0;

const MILLIS_PER_MINUTE: f64 = 60_000.0;

/// A rate stands in for the stretch it was measured over. Past this the watch
/// stopped reporting rather than the wearer holding one rate for that long.
const LONGEST_HELD_MS: i64 = 120_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sex {
    Male,
    Female,
}

impl Sex {
    pub fn from_wire(value: u8) -> Option<Sex> {
        match value {
            0 => Some(Sex::Male),
            1 => Some(Sex::Female),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Wearer {
    pub sex: Sex,
    pub weight: Kilograms,
    pub height: Centimetres,
    pub age: Years,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Reading {
    pub at: UnixMillis,
    pub total: Kilocalories,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Beat {
    pub at: UnixMillis,
    pub rate: Bpm,
}

impl Wearer {
    pub fn of(user: &UserProfile, at: UnixTime) -> Option<Wearer> {
        Some(Wearer {
            sex: Sex::from_wire(user.gender)?,
            weight: Kilograms(user.weight as f64 / 1000.0),
            height: Centimetres(user.height as u16),
            age: at.years_since(UnixTime(user.birth as i64)),
        })
    }

    /// Mifflin-St Jeor.
    fn resting_per_minute(self) -> f64 {
        let shared = 10.0 * self.weight.0 + 6.25 * self.height.0 as f64 - 5.0 * self.age.0;
        let daily = match self.sex {
            Sex::Male => shared + 5.0,
            Sex::Female => shared - 161.0,
        };
        daily / MINUTES_PER_DAY
    }

    /// Keytel's regression, which yields gross expenditure in kilojoules.
    fn gross_per_minute(self, rate: Bpm) -> f64 {
        let rate = rate.0 as f64;
        let kj = match self.sex {
            Sex::Male => -55.0969 + 0.6309 * rate + 0.1988 * self.weight.0 + 0.2017 * self.age.0,
            Sex::Female => -20.4022 + 0.4472 * rate - 0.1263 * self.weight.0 + 0.074 * self.age.0,
        };
        kj / KJ_PER_KCAL
    }

    fn met_per_minute(self, met: Met) -> f64 {
        met.0 * ML_OXYGEN_PER_MET_MINUTE * self.weight.0 * KCAL_PER_ML_OXYGEN
    }

    /// The regression is fitted to steady-state aerobic work, where the rate
    /// tracks the effort. Under intermittent effort the rate stays up through
    /// the pauses and reads above anything the activity can sustain, so the
    /// ceiling the activity does reach bounds it.
    ///
    /// The regression also falls under the resting burn at low rates, where the
    /// wearer is spending no more than lying down.
    fn earned_per_minute(self, rate: Bpm, ceiling: Met) -> f64 {
        let gross = self
            .gross_per_minute(rate)
            .min(self.met_per_minute(ceiling));
        (gross - self.resting_per_minute()).max(0.0)
    }
}

/// Above this a body runs rather than walks, and the oxygen it costs roughly
/// doubles for the same speed.
const RUNS_ABOVE_M_S: f64 = 2.2;

const SECONDS_PER_MINUTE: f64 = 60.0;

/// The Compendium of Physical Activities' bands for bicycling, which is the
/// same table every calculator on the web is quoting. A ride is read at the
/// speed it averaged, so the stop-start of a commute is already in the figure.
pub fn cycling_met(speed: MetresPerSecond) -> Met {
    let kmh = speed.0 * 3.6;
    Met(if kmh < 16.0 {
        6.8
    } else if kmh < 19.3 {
        8.0
    } else if kmh < 22.5 {
        10.0
    } else if kmh < 25.7 {
        12.0
    } else if kmh < 30.6 {
        14.0
    } else {
        16.0
    })
}

/// ACSM's metabolic equations: the oxygen a body costs rises with the speed it
/// carries itself at, and running costs about twice what walking does.
pub fn on_foot_met(speed: MetresPerSecond) -> Met {
    let metres_per_minute = speed.0 * SECONDS_PER_MINUTE;
    let millilitres = if speed.0 >= RUNS_ABOVE_M_S {
        0.2 * metres_per_minute + ML_OXYGEN_PER_MET_MINUTE
    } else {
        0.1 * metres_per_minute + ML_OXYGEN_PER_MET_MINUTE
    };
    Met(millilitres / ML_OXYGEN_PER_MET_MINUTE)
}

/// What the session had earned by each of its readings. The energy lands over
/// the span rather than at the end of it, so anything laid against a clock has
/// to be able to ask what was earned by a given moment.
pub fn accrued(wearer: Wearer, ceiling: Met, beats: &[Beat]) -> Vec<Reading> {
    let mut running = 0.0;
    let mut readings = Vec::with_capacity(beats.len().saturating_sub(1));
    for pair in beats.windows(2) {
        let held = (pair[1].at.0 - pair[0].at.0).clamp(0, LONGEST_HELD_MS) as f64;
        let mean = Bpm((pair[0].rate.0 + pair[1].rate.0) / 2);
        running += wearer.earned_per_minute(mean, ceiling) * held / MILLIS_PER_MINUTE;
        readings.push(Reading {
            at: pair[1].at,
            total: Kilocalories(running),
        });
    }
    readings
}

pub fn burned(wearer: Wearer, ceiling: Met, beats: &[Beat]) -> Kilocalories {
    accrued(wearer, ceiling, beats)
        .last()
        .map_or(Kilocalories(0.0), |reading| reading.total)
}

/// The watch's own counter is a resting estimate plus what its pedometer saw,
/// so a session the wrist never stepped through is missing from it. Adding the
/// sessions back is what makes it expenditure rather than walking.
///
/// The counter starts again at the watch's own midnight, which shows in the
/// readings as a drop. Cutting the day there rather than at a timezone is what
/// keeps history right across a move, and `opened_at` carries where the day
/// began for a window that starts partway through one.
pub fn with_workouts(
    counter: &[Reading],
    workouts: &[Vec<Reading>],
    opened_at: UnixMillis,
) -> Vec<Reading> {
    let mut opened_at = opened_at;
    let mut previous: Option<Kilocalories> = None;
    let mut lifted = Vec::with_capacity(counter.len());
    for reading in counter {
        if previous.is_some_and(|seen| reading.total.0 < seen.0) {
            opened_at = reading.at;
        }
        previous = Some(reading.total);
        let earned: f64 = workouts
            .iter()
            .filter(|session| {
                session
                    .first()
                    .is_some_and(|first| first.at.0 >= opened_at.0)
            })
            .filter_map(|session| {
                let reached = session.partition_point(|earlier| earlier.at.0 <= reading.at.0);
                reached.checked_sub(1).map(|last| session[last].total.0)
            })
            .sum();
        lifted.push(Reading {
            at: reading.at,
            total: Kilocalories(reading.total.0 + earned),
        });
    }
    lifted
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wearer() -> Wearer {
        Wearer {
            sex: Sex::Male,
            weight: Kilograms(75.0),
            height: Centimetres(178),
            age: Years(35.0),
        }
    }

    const LIFTING: Met = Met(6.0);

    const CYCLING: Met = Met(15.8);

    const RUNNING: Met = Met(19.0);

    fn beats(from_ms: i64, count: i64, every_secs: i64, rate: u16) -> Vec<Beat> {
        (0..count)
            .map(|i| Beat {
                at: UnixMillis(from_ms + i * every_secs * 1000),
                rate: Bpm(rate),
            })
            .collect()
    }

    #[test]
    fn an_hour_of_lifting_is_worth_a_few_hundred_kilocalories() {
        let burned = burned(wearer(), LIFTING, &beats(0, 61, 60, 102));
        assert!(burned.0 > 200.0 && burned.0 < 500.0, "{burned:?}");
    }

    #[test]
    fn what_the_wearer_would_have_spent_lying_still_is_not_theirs_to_earn() {
        let wearer = wearer();
        let hour = burned(wearer, RUNNING, &beats(0, 61, 60, 102));
        let gross = wearer.gross_per_minute(Bpm(102)) * 60.0;
        assert!((gross - hour.0 - wearer.resting_per_minute() * 60.0).abs() < 1e-9);
    }

    #[test]
    fn sitting_still_earns_nothing() {
        assert_eq!(
            burned(wearer(), LIFTING, &beats(0, 61, 60, 55)),
            Kilocalories(0.0)
        );
    }

    #[test]
    fn a_harder_hour_earns_more_than_an_easier_one() {
        let easy = burned(wearer(), RUNNING, &beats(0, 61, 60, 102));
        let hard = burned(wearer(), RUNNING, &beats(0, 61, 60, 140));
        assert!(hard.0 > easy.0);
    }

    #[test]
    fn a_gap_in_the_trace_is_not_charged_at_the_rate_either_side_of_it() {
        let mut sparse = beats(0, 2, 60, 130);
        sparse.push(Beat {
            at: UnixMillis(3_600_000),
            rate: Bpm(130),
        });
        let held = burned(wearer(), RUNNING, &sparse);
        let dense = burned(wearer(), RUNNING, &beats(0, 4, 60, 130));
        assert_eq!(held, dense);
    }

    #[test]
    fn one_reading_covers_no_time_at_all() {
        assert_eq!(
            burned(wearer(), RUNNING, &beats(0, 1, 60, 130)),
            Kilocalories(0.0)
        );
    }

    #[test]
    fn a_heavier_wearer_burns_more_at_the_same_rate() {
        let heavier = Wearer {
            weight: Kilograms(95.0),
            ..wearer()
        };
        assert!(
            burned(heavier, RUNNING, &beats(0, 61, 60, 120)).0
                > burned(wearer(), RUNNING, &beats(0, 61, 60, 120)).0
        );
    }

    #[test]
    fn a_rate_no_lifting_sustains_is_charged_at_what_lifting_reaches() {
        let wearer = wearer();
        let hour = burned(wearer, LIFTING, &beats(0, 61, 60, 150));
        let held = (wearer.met_per_minute(LIFTING) - wearer.resting_per_minute()) * 60.0;
        assert!((hour.0 - held).abs() < 1e-9, "{hour:?} against {held}");
    }

    #[test]
    fn the_same_rate_on_a_bike_is_not_held_to_the_lifting_ceiling() {
        let lifting = burned(wearer(), LIFTING, &beats(0, 61, 60, 150));
        let cycling = burned(wearer(), CYCLING, &beats(0, 61, 60, 150));
        assert!(cycling.0 > lifting.0, "{cycling:?} against {lifting:?}");
    }

    fn counter(from_ms: i64, every_secs: i64, totals: &[f64]) -> Vec<Reading> {
        totals
            .iter()
            .enumerate()
            .map(|(i, total)| Reading {
                at: UnixMillis(from_ms + i as i64 * every_secs * 1000),
                total: Kilocalories(*total),
            })
            .collect()
    }

    #[test]
    fn what_a_session_accrued_by_its_end_is_what_it_burned() {
        let beats = beats(0, 61, 60, 130);
        let curve = accrued(wearer(), LIFTING, &beats);
        assert_eq!(curve.len(), 60);
        assert_eq!(
            curve.last().unwrap().total,
            burned(wearer(), LIFTING, &beats)
        );
        assert!(curve[0].total.0 < curve[59].total.0);
    }

    #[test]
    fn the_counter_carries_a_session_from_where_it_reaches_it() {
        let session = accrued(wearer(), LIFTING, &beats(600_000, 41, 60, 130));
        let earned = session.last().unwrap().total.0;
        let lifted = with_workouts(
            &counter(0, 1800, &[100.0, 150.0, 200.0]),
            &[session],
            UnixMillis(0),
        );
        assert_eq!(lifted[0].total, Kilocalories(100.0));
        assert!(lifted[1].total.0 > 150.0 && lifted[1].total.0 < 150.0 + earned);
        assert!((lifted[2].total.0 - (200.0 + earned)).abs() < 1e-9);
    }

    #[test]
    fn a_session_the_day_before_is_not_carried_past_the_counters_restart() {
        let session = accrued(wearer(), LIFTING, &beats(0, 21, 60, 130));
        let lifted = with_workouts(
            &counter(0, 3600, &[100.0, 1800.0, 20.0, 90.0]),
            &[session],
            UnixMillis(0),
        );
        assert!(lifted[1].total.0 > 1800.0, "earned before the restart");
        assert_eq!(lifted[2].total, Kilocalories(20.0));
        assert_eq!(lifted[3].total, Kilocalories(90.0));
    }

    #[test]
    fn a_session_from_before_the_day_opened_is_not_this_days_to_carry() {
        let session = accrued(wearer(), LIFTING, &beats(0, 21, 60, 130));
        let lifted = with_workouts(
            &counter(7_200_000, 1800, &[100.0, 150.0]),
            &[session],
            UnixMillis(3_600_000),
        );
        assert_eq!(lifted[0].total, Kilocalories(100.0));
        assert_eq!(lifted[1].total, Kilocalories(150.0));
    }

    #[test]
    fn a_gender_the_watch_never_set_is_not_a_wearer() {
        let user = UserProfile {
            id: 1,
            weight: 75_000,
            height: 178,
            gender: 9,
            birth: 644198400,
            first_name: String::new(),
        };
        assert!(Wearer::of(&user, UnixTime(1_785_057_972)).is_none());
    }
}

#[cfg(test)]
mod speed_tests {
    use super::*;

    #[test]
    fn a_commute_is_read_at_the_band_its_speed_falls_in() {
        assert_eq!(cycling_met(MetresPerSecond(15.96 / 3.6)), Met(6.8));
        assert_eq!(cycling_met(MetresPerSecond(20.0 / 3.6)), Met(10.0));
        assert_eq!(cycling_met(MetresPerSecond(35.0 / 3.6)), Met(16.0));
    }

    #[test]
    fn walking_and_running_are_not_the_same_cost_at_the_same_speed() {
        // A brisk walk is about three and a half METs, and a sixteen minute
        // mile run is about ten.
        let walk = on_foot_met(MetresPerSecond(5.0 / 3.6));
        assert!((walk.0 - 3.4).abs() < 0.2, "{walk:?}");

        let run = on_foot_met(MetresPerSecond(16.0 / 3.6));
        assert!((run.0 - 16.2).abs() < 0.5, "{run:?}");

        assert!(on_foot_met(MetresPerSecond(3.0)).0 > on_foot_met(MetresPerSecond(2.1)).0 * 1.5);
    }

    #[test]
    fn a_ride_at_a_measured_speed_costs_what_the_tables_say() {
        let wearer = Wearer {
            sex: Sex::Male,
            weight: Kilograms(73.0),
            height: Centimetres(175),
            age: Years(33.2),
        };
        let ceiling = cycling_met(MetresPerSecond(15.96 / 3.6));
        let per_minute = wearer.met_per_minute(ceiling) - wearer.resting_per_minute();

        assert!(
            (per_minute * 10.0 - 75.0).abs() < 3.0,
            "{} kcal over ten minutes, against the 75 every calculator gives",
            per_minute * 10.0
        );
    }
}
