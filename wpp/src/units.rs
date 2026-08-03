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

quantity!(Bpm, u16);
quantity!(Celsius, f64);
quantity!(Millivolts, f64);
quantity!(Millis, f64);
quantity!(Kilograms, f64);
quantity!(Centimetres, u16);
quantity!(BreathsPerMinute, u16);
quantity!(Metres, f64);
quantity!(Kilocalories, f64);
quantity!(Met, f64);
quantity!(UnixTime, i64);
quantity!(UnixMillis, i64);

impl UnixTime {
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

pub const COUNTS_PER_MILLIVOLT: f64 = 1000.0;

impl Millivolts {
    pub fn from_counts(counts: i16) -> Millivolts {
        Millivolts(counts as f64 / COUNTS_PER_MILLIVOLT)
    }

    pub fn from_counts_scaled(counts: i16, counts_per_mv: f64) -> Millivolts {
        Millivolts(counts as f64 / counts_per_mv)
    }
}

impl VasistasCbt {
    pub fn core_temperature(&self) -> Celsius {
        Celsius(self.temperature as f64 / 1000.0)
    }
}

impl VasistasHeartrate {
    pub fn heart_rate(&self) -> Bpm {
        Bpm(self.heartrate as u16)
    }
}

impl LiveHr {
    pub fn heart_rate(&self) -> Bpm {
        Bpm(self.hr as u16)
    }
}

impl VasistasHrv {
    pub fn heart_rate(&self) -> Bpm {
        Bpm(self.hr as u16)
    }

    pub fn sdnn(&self) -> Millis {
        Millis(self.sdnn as f64)
    }

    pub fn rmssd(&self) -> Millis {
        Millis(self.rmssd as f64)
    }
}

impl VasistasRr {
    pub fn respiratory_rate(&self) -> BreathsPerMinute {
        BreathsPerMinute(self.rr as u16)
    }
}

impl BatteryStatus {
    pub fn voltage(&self) -> Millivolts {
        Millivolts(self.battery_mv as f64)
    }
}

impl TimeSet {
    pub fn time(&self) -> UnixTime {
        UnixTime(self.utc as i64)
    }

    pub fn dst_change_time(&self) -> UnixTime {
        UnixTime(self.dst_change_time as i64)
    }
}

impl WamVasistasHead {
    pub fn time(&self) -> UnixTime {
        UnixTime(self.utc as i64)
    }
}

pub const ACTIVITY_HUNDREDTHS: f64 = 100.0;

impl WamVasistasAwake {
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
    pub fn calories(&self) -> Kilocalories {
        Kilocalories(self.calories as f64 / ACTIVITY_HUNDREDTHS)
    }

    pub fn met(&self) -> Met {
        Met(self.met as f64 / ACTIVITY_HUNDREDTHS)
    }
}

impl TrackerUser {
    pub fn weight(&self) -> Kilograms {
        Kilograms(self.weight as f64 / 1000.0)
    }

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
