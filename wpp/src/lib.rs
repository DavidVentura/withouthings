//! Withings Proprietary Protocol (WPP).
//!
//! The object layouts, command ids and enum values in [`objects`] and
//! [`commands`] are generated from the Withings Android app by
//! `tools/extract_wpp.py`; see `tools/wpp.json` for the extracted description.

pub mod analysis;
pub mod capture;
pub mod client;
pub mod codec;
pub mod commands;
pub mod frame;
pub mod objects;
pub mod signal;
pub mod units;

pub use analysis::{detect_r_peaks, RPeaks};
pub use client::{Action, Category, Client, Credentials, Event, Record, SampleKind, Source};
pub use codec::{ParseError, Reader, WppObjectCodec, Writer};
pub use commands::Command;
pub use frame::{Channel, Frame, FrameError, PROTOCOL_VERSION};
pub use objects::WppObject;
pub use signal::{Lead, SampleFormat, Signal, SignalCollector, SignalKind};
pub use units::{Bpm, Celsius, Millis, Millivolts, UnixTime, COUNTS_PER_MILLIVOLT};

#[cfg(test)]
mod generated_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::BatteryStatus;

    #[test]
    fn battery_status_matches_the_apk_layout() {
        // type 0x0504, size 0x000a, 62%, state OK, 4180 mV, reserved 0
        let bytes = [
            0x01, 0x05, 0x04, 0x00, 0x0e, 0x05, 0x04, 0x00, 0x0a, 0x3e, 0x02, 0x00, 0x00, 0x10,
            0x54, 0x00, 0x00, 0x00, 0x00,
        ];
        let frame = Frame::parse(&bytes).unwrap();
        assert_eq!(frame.command, Command::CMD_BATTERY_STATUS);
        assert_eq!(
            frame.objects,
            vec![WppObject::BatteryStatus(BatteryStatus {
                battery_percent: 62,
                battery_state: BatteryStatus::BATTERY_STATE_OK,
                battery_mv: 4180,
                reserved: 0,
            })]
        );
        assert_eq!(frame.to_bytes(), bytes);
    }

    #[test]
    fn channel_bits_split_off_the_opcode() {
        let notif = Command::CMD_BATTERY_STATUS.with_channel(Channel::Notification);
        assert_eq!(notif.0, 0x8504);
        assert_eq!(notif.channel(), Some(Channel::Notification));
        assert_eq!(notif.opcode(), Command::CMD_BATTERY_STATUS.0);
        assert_eq!(notif.opcode_name(), Some("CMD_BATTERY_STATUS"));
    }

    #[test]
    fn unknown_type_ids_keep_their_bytes() {
        let frame = Frame::new(
            Command::CMD_PROBE,
            vec![WppObject::Unknown {
                type_id: 0x3fff,
                data: vec![1, 2, 3],
            }],
        );
        let bytes = frame.to_bytes();
        assert_eq!(Frame::parse(&bytes).unwrap(), frame);
    }

    #[test]
    fn a_short_buffer_is_reported_as_incomplete_not_corrupt() {
        let bytes = [0x01, 0x05, 0x04, 0x00, 0x0e, 0x05];
        assert_eq!(
            Frame::parse(&bytes),
            Err(FrameError::IncompletePayload {
                declared: 14,
                available: 1
            })
        );
        assert_eq!(Frame::declared_len(&bytes), Some(19));
    }
}
