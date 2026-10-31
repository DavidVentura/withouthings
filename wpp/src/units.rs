use std::marker::PhantomData;
use std::ops::{Add, Sub};

use crate::objects::LiveHr;

macro_rules! measured {
    ($($name:ident),* $(,)?) => {
        $(
            #[derive(Debug, Clone, Copy, PartialEq, PartialOrd)]
            pub struct $name(pub f64);
        )*
    };
}

macro_rules! counted {
    ($($name:ident: $inner:ty),* $(,)?) => {
        $(
            #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
            pub struct $name(pub $inner);
        )*
    };
}

measured!(Celsius, Kilograms, Millivolts, Millis, Metres, MetresPerSecond, Kilocalories, Met, Years);
counted!(Bpm: u16, Centimetres: u16, UnixTime: i64, UnixMillis: i64);

/// A stretch of time, signed because the difference of two clock readings can
/// be negative. Which of seconds or milliseconds it came in stops mattering
/// once it is one of these.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct Duration {
    millis: i64,
}

impl Duration {
    pub const ZERO: Duration = Duration { millis: 0 };

    pub const fn from_millis(millis: i64) -> Duration {
        Duration { millis }
    }

    pub const fn from_secs(secs: i64) -> Duration {
        Duration {
            millis: secs * 1000,
        }
    }

    pub const fn as_millis(self) -> i64 {
        self.millis
    }

    pub const fn as_secs(self) -> i64 {
        self.millis.div_euclid(1000)
    }
}

impl Add for Duration {
    type Output = Duration;
    fn add(self, other: Duration) -> Duration {
        Duration::from_millis(self.millis + other.millis)
    }
}

impl Sub for UnixMillis {
    type Output = Duration;
    fn sub(self, earlier: UnixMillis) -> Duration {
        Duration::from_millis(self.0 - earlier.0)
    }
}

impl Add<Duration> for UnixMillis {
    type Output = UnixMillis;
    fn add(self, by: Duration) -> UnixMillis {
        UnixMillis(self.0 + by.millis)
    }
}

impl Sub<Duration> for UnixMillis {
    type Output = UnixMillis;
    fn sub(self, by: Duration) -> UnixMillis {
        UnixMillis(self.0 - by.millis)
    }
}

impl Sub for UnixTime {
    type Output = Duration;
    fn sub(self, earlier: UnixTime) -> Duration {
        Duration::from_secs(self.0 - earlier.0)
    }
}

impl Add<Duration> for UnixTime {
    type Output = UnixTime;
    fn add(self, by: Duration) -> UnixTime {
        UnixTime(self.0 + by.as_secs())
    }
}

const SECONDS_PER_YEAR: f64 = 365.2425 * 24.0 * 60.0 * 60.0;

impl UnixTime {
    pub fn years_since(self, earlier: UnixTime) -> Years {
        Years((self.0 - earlier.0) as f64 / SECONDS_PER_YEAR)
    }

    pub fn to_millis(self) -> UnixMillis {
        UnixMillis(self.0 * 1000)
    }
}

impl UnixMillis {
    pub fn to_seconds(self) -> UnixTime {
        UnixTime(self.0.div_euclid(1000))
    }
}

pub const COUNTS_PER_MILLIVOLT: f64 = 1000.0;

impl Millivolts {
    pub fn from_counts(counts: i16) -> Millivolts {
        Millivolts(counts as f64 / COUNTS_PER_MILLIVOLT)
    }
}

impl LiveHr {
    pub fn heart_rate(&self) -> Bpm {
        Bpm(self.hr as u16)
    }
}

/// A figure the watch sends scaled by a hundred, kept as sent so a sum of them
/// loses nothing; [`Hundredths::value`] is the one place the scale is applied.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Hundredths<T> {
    raw: i64,
    unit: PhantomData<T>,
}

impl<T> Hundredths<T> {
    pub const fn new(raw: i64) -> Hundredths<T> {
        Hundredths {
            raw,
            unit: PhantomData,
        }
    }

    pub const fn raw(self) -> i64 {
        self.raw
    }
}

impl<T: From<f64>> Hundredths<T> {
    pub fn value(self) -> T {
        T::from(self.raw as f64 / 100.0)
    }
}

impl<T> std::iter::Sum for Hundredths<T> {
    fn sum<I: Iterator<Item = Hundredths<T>>>(iter: I) -> Hundredths<T> {
        Hundredths::new(iter.map(|h| h.raw).sum())
    }
}

impl From<f64> for Metres {
    fn from(value: f64) -> Metres {
        Metres(value)
    }
}

impl From<f64> for Kilocalories {
    fn from(value: f64) -> Kilocalories {
        Kilocalories(value)
    }
}

impl From<f64> for Met {
    fn from(value: f64) -> Met {
        Met(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ecg_counts_are_microvolts() {
        assert_eq!(Millivolts::from_counts(1768), Millivolts(1.768));
        assert_eq!(Millivolts::from_counts(-658), Millivolts(-0.658));
    }

    #[test]
    fn clock_arithmetic_keeps_its_unit() {
        assert_eq!(UnixTime(10) - UnixTime(4), Duration::from_millis(6000));
        assert_eq!(UnixMillis(1500) - UnixMillis(2000), Duration::from_millis(-500));
        assert_eq!(Duration::from_millis(-500).as_secs(), -1);
        assert_eq!(UnixTime(10) + Duration::from_secs(5), UnixTime(15));
    }

    #[test]
    fn hundredths_sum_before_they_scale() {
        let total: Hundredths<Metres> = [Hundredths::new(150), Hundredths::new(450)].into_iter().sum();
        assert_eq!(total.value(), Metres(6.0));
    }
}
