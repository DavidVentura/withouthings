//! Physical quantities, kept distinct from the wire encoding.
//!
//! The generated structs in [`crate::objects`] hold what the device sent;
//! converting to a physical value happens only here. A field with no method
//! here has no established scale — use the raw field and treat it as
//! uncalibrated rather than assuming one.

use crate::objects::{
    BatteryStatus, LiveHr, TimeSet, TrackerUser, VasistasCbt, VasistasHeartrate, VasistasHrv,
    VasistasRr, WamVasistasAwake, WamVasistasHead, WamVasistasMetCalEarned,
};

macro_rules! quantity {
    ($(#[$doc:meta])* $name:ident, $inner:ty) => {
        $(#[$doc])*
        #[derive(Debug, Clone, Copy, PartialEq, PartialOrd)]
        pub struct $name(pub $inner);

        impl $name {
            pub fn value(self) -> $inner {
                self.0
            }
        }
    };
}

quantity!(
    /// Heart rate in beats per minute.
    Bpm,
    u16
);
quantity!(
    /// Temperature in degrees Celsius.
    Celsius,
    f64
);
quantity!(
    /// Potential in millivolts.
    Millivolts,
    f64
);
quantity!(
    /// A duration in milliseconds.
    Millis,
    f64
);
quantity!(
    /// Mass in kilograms.
    Kilograms,
    f64
);
quantity!(
    /// Length in centimetres.
    Centimetres,
    u16
);
quantity!(
    /// Breaths per minute.
    BreathsPerMinute,
    u16
);
quantity!(
    /// Length in metres.
    Metres,
    f64
);
quantity!(
    /// Energy in kilocalories.
    Kilocalories,
    f64
);
quantity!(
    /// Metabolic equivalent of task: multiples of resting expenditure.
    Met,
    f64
);
quantity!(
    /// Seconds since the Unix epoch, UTC. What the protocol carries.
    UnixTime,
    i64
);
quantity!(
    /// Milliseconds since the Unix epoch, UTC.
    ///
    /// Live pushes arrive faster than the protocol's one-second timestamps can
    /// distinguish, so stored measurement times use this instead.
    UnixMillis,
    i64
);

impl UnixTime {
    /// Offset from UTC in seconds, as carried alongside device timestamps.
    pub fn with_offset(self, seconds: i32) -> UnixTime {
        UnixTime(self.0 + seconds as i64)
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

/// ECG samples are microvolts: one ADC count is 1 µV.
///
/// From the decompiled app's chart axis; measuring its rendering agrees within
/// 1%. `UnitConversionParameters.gain` is an analog front-end parameter, not
/// this scale.
pub const COUNTS_PER_MILLIVOLT: f64 = 1000.0;

impl Millivolts {
    pub fn from_counts(counts: i16) -> Millivolts {
        Millivolts(counts as f64 / COUNTS_PER_MILLIVOLT)
    }

    /// For a device whose samples are not microvolts.
    pub fn from_counts_scaled(counts: i16, counts_per_mv: f64) -> Millivolts {
        Millivolts(counts as f64 / counts_per_mv)
    }
}

impl VasistasCbt {
    /// Core body temperature, milli-degrees on the wire.
    ///
    /// Confirmed against the watch's own display.
    pub fn core_temperature(&self) -> Celsius {
        Celsius(self.temperature as f64 / 1000.0)
    }
}

impl VasistasHeartrate {
    /// Sampled heart rate; the wire value is already bpm.
    pub fn heart_rate(&self) -> Bpm {
        Bpm(self.heartrate as u16)
    }
}

impl LiveHr {
    /// Heart rate pushed once a second during a workout or an ECG.
    pub fn heart_rate(&self) -> Bpm {
        Bpm(self.hr as u16)
    }
}

impl VasistasHrv {
    pub fn heart_rate(&self) -> Bpm {
        Bpm(self.hr as u16)
    }

    /// Standard deviation of NN intervals. Unit inferred from magnitude.
    pub fn sdnn(&self) -> Millis {
        Millis(self.sdnn as f64)
    }

    /// Root mean square of successive NN interval differences.
    pub fn rmssd(&self) -> Millis {
        Millis(self.rmssd as f64)
    }
}

impl VasistasRr {
    /// Respiratory rate. Unit inferred from magnitude.
    pub fn respiratory_rate(&self) -> BreathsPerMinute {
        BreathsPerMinute(self.rr as u16)
    }
}

impl BatteryStatus {
    /// Cell voltage. The field names its own unit.
    pub fn voltage(&self) -> Millivolts {
        Millivolts(self.battery_mv as f64)
    }
}

impl TimeSet {
    /// Device clock. Verified against a capture's wall-clock time.
    pub fn time(&self) -> UnixTime {
        UnixTime(self.utc as i64)
    }

    pub fn dst_change_time(&self) -> UnixTime {
        UnixTime(self.dst_change_time as i64)
    }
}

impl WamVasistasHead {
    /// Start of the sample window this record covers.
    pub fn time(&self) -> UnixTime {
        UnixTime(self.utc as i64)
    }
}

/// The activity stream counts in hundredths: centimetres of ground, hundredths
/// of a calorie, hundredths of a MET.
///
/// Established against the official app's own database, which stores these
/// fields divided by exactly this — 4260 steps of one walk against 3132 m,
/// 0.735 m a step, and a walking minute at 2.90 MET.
pub const ACTIVITY_HUNDREDTHS: f64 = 100.0;

impl WamVasistasAwake {
    /// Ground covered, centimetres on the wire.
    pub fn distance(&self) -> Metres {
        Metres(self.distance as f64 / ACTIVITY_HUNDREDTHS)
    }

    pub fn ascent(&self) -> Metres {
        Metres(self.ascent as f64 / ACTIVITY_HUNDREDTHS)
    }

    pub fn descent(&self) -> Metres {
        Metres(self.descent as f64 / ACTIVITY_HUNDREDTHS)
    }
}

impl WamVasistasMetCalEarned {
    /// Calories earned over the window, above what resting would have cost.
    pub fn calories(&self) -> Kilocalories {
        Kilocalories(self.calories as f64 / ACTIVITY_HUNDREDTHS)
    }

    /// Walking reads 290: three times what resting costs.
    pub fn met(&self) -> Met {
        Met(self.met as f64 / ACTIVITY_HUNDREDTHS)
    }
}

impl TrackerUser {
    /// Body mass, grams on the wire. Scale inferred from magnitude.
    pub fn weight(&self) -> Kilograms {
        Kilograms(self.weight as f64 / 1000.0)
    }

    /// Stature, centimetres. Inferred from magnitude.
    pub fn height(&self) -> Centimetres {
        Centimetres(self.height as u16)
    }

    pub fn birth_date(&self) -> UnixTime {
        UnixTime(self.birth as i64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_wire_value_and_the_converted_value_stay_distinct() {
        let cbt = VasistasCbt {
            algo: 0,
            attrib: 1,
            temperature: 37255,
        };
        assert_eq!(cbt.temperature, 37255);
        assert_eq!(cbt.core_temperature(), Celsius(37.255));
    }

    #[test]
    fn heart_rate_needs_no_scaling_but_is_still_typed() {
        assert_eq!(LiveHr { hr: 82 }.heart_rate(), Bpm(82));
        assert_eq!(
            VasistasHeartrate {
                heartrate: 67,
                quality: 5,
                temperature: 0
            }
            .heart_rate(),
            Bpm(67)
        );
    }

    #[test]
    fn ecg_counts_are_microvolts() {
        assert_eq!(Millivolts::from_counts(1768), Millivolts(1.768));
        assert_eq!(Millivolts::from_counts(-658), Millivolts(-0.658));
        assert_eq!(
            Millivolts::from_counts_scaled(1610, 1610.0),
            Millivolts(1.0)
        );
    }

    #[test]
    fn tracker_user_converts_grams_and_centimetres() {
        let user = TrackerUser {
            id: 1,
            weight: 72000,
            height: 175,
            gender: 0,
            birth: 644198400,
            first_name: String::new(),
        };
        assert_eq!(user.weight(), Kilograms(72.0));
        assert_eq!(user.height(), Centimetres(175));
    }
}
