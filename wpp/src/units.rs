//! Physical quantities, kept distinct from the wire encoding.
//!
//! The generated structs in [`crate::objects`] hold exactly what the device
//! sent: `VasistasCbt { temperature: 37255 }` is the integer off the wire. The
//! conversions here are the only place a raw field becomes a physical value,
//! and each one records the evidence for its scale factor.
//!
//! Conversions exist only where that evidence does. A field with no method
//! here is one whose units have not been established — reach for the raw field
//! and treat it as uncalibrated rather than assuming a scale.

use crate::objects::{
    BatteryStatus, LiveHr, TimeSet, TrackerUser, VasistasCbt, VasistasHeartrate, VasistasHrv,
    VasistasRr, WamVasistasHead,
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
    /// Seconds since the Unix epoch, UTC.
    UnixTime,
    i64
);

impl UnixTime {
    /// Offset from UTC in seconds, as carried alongside device timestamps.
    pub fn with_offset(self, seconds: i32) -> UnixTime {
        UnixTime(self.0 + seconds as i64)
    }
}

/// Counts-to-millivolts for an ECG lead.
///
/// The scale is NOT established. `UnitConversionParameters.gain` is 1610 for
/// the ScanWatch 2, but comparing decoded counts against the app's own 10 mm/mV
/// render implies roughly 950 counts/mV, and the app's trace is post-processed
/// by `libecg` so the comparison cannot settle it. The conversion therefore
/// takes the factor from the caller rather than guessing one.
impl Millivolts {
    pub fn from_counts(counts: i16, counts_per_mv: f64) -> Millivolts {
        Millivolts(counts as f64 / counts_per_mv)
    }
}

impl VasistasCbt {
    /// Core body temperature.
    ///
    /// Scale inferred, not read from the app: observed values cluster at
    /// 37255/37245/37237, which is 37.2 °C in milli-degrees and is the correct
    /// range for core body temperature. The app copies the field into its model
    /// unscaled, so the divisor is applied somewhere downstream that has not
    /// been traced.
    pub fn core_temperature(&self) -> Celsius {
        Celsius(self.temperature as f64 / 1000.0)
    }
}

impl VasistasHeartrate {
    /// Sampled heart rate. The field is already in bpm; `quality` grades it and
    /// is not a physical quantity.
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

    /// Standard deviation of NN intervals.
    ///
    /// Unit inferred from magnitude: observed 73-76 alongside RMSSD 48-81,
    /// which are conventional millisecond figures for these indices.
    pub fn sdnn(&self) -> Millis {
        Millis(self.sdnn as f64)
    }

    /// Root mean square of successive NN interval differences.
    pub fn rmssd(&self) -> Millis {
        Millis(self.rmssd as f64)
    }
}

impl VasistasRr {
    /// Respiratory rate. Observed 10-13, the normal resting range in
    /// breaths per minute.
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
    /// Device clock, verified: a captured value decoded to the wall-clock time
    /// of the capture, and `dst_change_time` to the correct EU DST boundary.
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

impl TrackerUser {
    /// Body mass. Scale inferred from magnitude: 72000 for an adult is grams.
    pub fn weight(&self) -> Kilograms {
        Kilograms(self.weight as f64 / 1000.0)
    }

    /// Stature. Observed 175 for an adult, so centimetres.
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
        // The struct keeps exactly what arrived.
        assert_eq!(cbt.temperature, 37255);
        // Conversion is a separate, typed step.
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
    fn ecg_millivolts_require_an_explicit_scale() {
        assert_eq!(Millivolts::from_counts(1768, 1000.0), Millivolts(1.768));
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
