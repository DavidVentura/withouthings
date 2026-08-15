use crate::client::UserProfile;
use crate::units::{Bpm, Centimetres, Kilocalories, Kilograms, UnixMillis, UnixTime, Years};

const KJ_PER_KCAL: f64 = 4.184;

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

    /// The regression is fitted to exercise rates and falls under the resting
    /// burn below them, where the wearer is spending no more than lying down.
    fn earned_per_minute(self, rate: Bpm) -> f64 {
        (self.gross_per_minute(rate) - self.resting_per_minute()).max(0.0)
    }
}

pub fn burned(wearer: Wearer, beats: &[Beat]) -> Kilocalories {
    let earned = beats
        .windows(2)
        .map(|pair| {
            let held = (pair[1].at.0 - pair[0].at.0).clamp(0, LONGEST_HELD_MS) as f64;
            let mean = Bpm((pair[0].rate.0 + pair[1].rate.0) / 2);
            wearer.earned_per_minute(mean) * held / MILLIS_PER_MINUTE
        })
        .sum();
    Kilocalories(earned)
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
        let burned = burned(wearer(), &beats(0, 61, 60, 102));
        assert!(burned.0 > 200.0 && burned.0 < 500.0, "{burned:?}");
    }

    #[test]
    fn what_the_wearer_would_have_spent_lying_still_is_not_theirs_to_earn() {
        let wearer = wearer();
        let hour = burned(wearer, &beats(0, 61, 60, 102));
        let gross = wearer.gross_per_minute(Bpm(102)) * 60.0;
        assert!((gross - hour.0 - wearer.resting_per_minute() * 60.0).abs() < 1e-9);
    }

    #[test]
    fn sitting_still_earns_nothing() {
        assert_eq!(burned(wearer(), &beats(0, 61, 60, 55)), Kilocalories(0.0));
    }

    #[test]
    fn a_harder_hour_earns_more_than_an_easier_one() {
        let easy = burned(wearer(), &beats(0, 61, 60, 102));
        let hard = burned(wearer(), &beats(0, 61, 60, 140));
        assert!(hard.0 > easy.0);
    }

    #[test]
    fn a_gap_in_the_trace_is_not_charged_at_the_rate_either_side_of_it() {
        let mut sparse = beats(0, 2, 60, 130);
        sparse.push(Beat {
            at: UnixMillis(3_600_000),
            rate: Bpm(130),
        });
        let held = burned(wearer(), &sparse);
        let dense = burned(wearer(), &beats(0, 4, 60, 130));
        assert_eq!(held, dense);
    }

    #[test]
    fn one_reading_covers_no_time_at_all() {
        assert_eq!(burned(wearer(), &beats(0, 1, 60, 130)), Kilocalories(0.0));
    }

    #[test]
    fn a_heavier_wearer_burns_more_at_the_same_rate() {
        let heavier = Wearer {
            weight: Kilograms(95.0),
            ..wearer()
        };
        assert!(
            burned(heavier, &beats(0, 61, 60, 120)).0 > burned(wearer(), &beats(0, 61, 60, 120)).0
        );
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
