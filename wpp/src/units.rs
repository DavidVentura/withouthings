use std::marker::PhantomData;
use std::num::{NonZeroU16, NonZeroUsize};
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

/// A sampling rate, which divides everywhere it is used and so cannot be zero.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SamplingHz(NonZeroU16);

impl SamplingHz {
    pub const fn new(hz: u16) -> Option<SamplingHz> {
        match NonZeroU16::new(hz) {
            Some(hz) => Some(SamplingHz(hz)),
            None => None,
        }
    }

    pub const fn get(self) -> u16 {
        self.0.get()
    }

    pub const fn as_f64(self) -> f64 {
        self.0.get() as f64
    }
}

/// How many leads a recording interleaves; a modulus when it is taken apart.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LeadCount(NonZeroUsize);

impl LeadCount {
    pub const fn new(leads: usize) -> Option<LeadCount> {
        match NonZeroUsize::new(leads) {
            Some(leads) => Some(LeadCount(leads)),
            None => None,
        }
    }

    pub const fn get(self) -> usize {
        self.0.get()
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

/// Boundaries of consecutive windows, in time order, so each instant falls in
/// at most one of them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Edges(Vec<UnixMillis>);

impl Edges {
    /// None when out of order. Fewer than two edges hold no window.
    pub fn new(edges: Vec<UnixMillis>) -> Option<Edges> {
        edges.windows(2).all(|w| w[0] <= w[1]).then_some(Edges(edges))
    }

    pub fn windows(&self) -> usize {
        self.0.len().saturating_sub(1)
    }

    /// From the first edge up to, and not including, the last; None without a window.
    pub fn span(&self) -> Option<(UnixMillis, UnixMillis)> {
        (self.windows() > 0).then(|| (self.0[0], self.0[self.0.len() - 1]))
    }

    pub fn window_of(&self, at: UnixMillis) -> Option<usize> {
        self.0.windows(2).position(|w| at >= w[0] && at < w[1])
    }

    pub fn as_slice(&self) -> &[UnixMillis] {
        &self.0
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
    fn edges_refuse_to_run_backwards_and_place_each_instant_once() {
        assert_eq!(Edges::new(vec![UnixMillis(2), UnixMillis(1)]), None);
        let edges = Edges::new(vec![UnixMillis(0), UnixMillis(10), UnixMillis(20)]).unwrap();
        assert_eq!(edges.windows(), 2);
        assert_eq!(edges.window_of(UnixMillis(10)), Some(1));
        assert_eq!(edges.window_of(UnixMillis(20)), None);
        assert_eq!(Edges::new(Vec::new()).unwrap().span(), None);
    }

    #[test]
    fn hundredths_sum_before_they_scale() {
        let total: Hundredths<Metres> = [Hundredths::new(150), Hundredths::new(450)].into_iter().sum();
        assert_eq!(total.value(), Metres(6.0));
    }
}
