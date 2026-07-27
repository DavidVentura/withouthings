//! The service driven exactly as Kotlin would drive it.

use std::sync::{Arc, Mutex};
use wpp_ffi::{SetEdge, Transport, WatchService};

#[derive(Default)]
struct Recorder {
    written: Mutex<Vec<Vec<u8>>>,
    changes: Mutex<u32>,
}

struct Handle(Arc<Recorder>);

impl Transport for Handle {
    fn write(&self, bytes: Vec<u8>) {
        self.0.written.lock().unwrap().push(bytes);
    }
    fn changed(&self) {
        *self.0.changes.lock().unwrap() += 1;
    }
}

fn service(recorder: &Arc<Recorder>) -> (WatchService, String) {
    let path = format!(
        "/tmp/wpp-ffi-test-{}.db",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let service = WatchService::new(
        path.clone(),
        "a4:7e:fa:44:d6:10".to_string(),
        "gUf8Np69A4GvJxjY1XOcIHKQm2HcPZnO".to_string(),
        Box::new(Handle(recorder.clone())),
    )
    .expect("service");
    (service, path)
}

fn cleanup(path: &str) {
    for suffix in ["", "-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{path}{suffix}"));
    }
}

#[test]
fn connecting_probes_the_watch_and_asks_nothing_else() {
    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    service.on_connected().unwrap();

    let written = recorder.written.lock().unwrap().clone();
    // Anything asked before the handshake completes draws ERR_NOT_AUTH, which
    // is indistinguishable from the watch refusing the probe outright.
    assert_eq!(written.len(), 1, "the probe, and nothing else");
    let frame = wpp::Frame::parse(&written[0]).expect("valid frame");
    assert_eq!(frame.command.opcode(), wpp::Command::CMD_PROBE.0);

    cleanup(&path);
}

/// A notification split across two writes, as the MTU forces.
#[test]
fn a_frame_split_across_notifications_is_reassembled_and_stored() {
    use wpp::objects::BatteryStatus;
    use wpp::{Command, Frame, WppObject};

    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    let frame = Frame::new(
        Command::CMD_BATTERY_STATUS,
        vec![WppObject::BatteryStatus(BatteryStatus {
            battery_percent: 31,
            battery_state: BatteryStatus::BATTERY_STATE_OK,
            battery_mv: 3836,
            reserved: 0,
        })],
    );
    let bytes = frame.to_bytes();
    service
        .on_bytes(bytes[..6].to_vec(), 1_700_000_000_000)
        .unwrap();
    service
        .on_bytes(bytes[6..].to_vec(), 1_700_000_000_000)
        .unwrap();

    let snapshot = service.snapshot().unwrap();
    let battery = snapshot.battery.expect("battery reading");
    assert_eq!(battery.percent, 31);
    // The reading carries when it was taken, so the UI can say how old it is
    // instead of implying it is current.
    assert_eq!(battery.at_ms, 1_700_000_000_000);

    cleanup(&path);
}

#[test]
fn set_markers_round_trip_through_the_service() {
    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    service.mark_set(1_000_000, SetEdge::Start).unwrap();
    service.mark_set(1_060_000, SetEdge::End).unwrap();

    let markers = service.markers(0, 2_000_000).unwrap();
    assert_eq!(markers.len(), 2);
    assert_eq!(markers[0].edge, SetEdge::Start);
    assert_eq!(markers[1].edge, SetEdge::End);
    assert!(*recorder.changes.lock().unwrap() >= 2, "the UI is nudged");

    cleanup(&path);
}

/// Reduction for drawing must keep the extremes: a dip between sets is the
/// point of the chart, and averaging would erase it.
#[test]
fn reducing_a_series_for_drawing_keeps_peaks_and_troughs() {
    use wpp::client::{Record, SampleKind, Source};
    use wpp::units::UnixMillis;

    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    let mut store = wpp_store::Store::open(&path).unwrap();
    let device = store.device("a4:7e:fa:44:d6:10").unwrap();
    let records: Vec<Record> = (0..600)
        .map(|i| Record::Sample {
            measured_at: UnixMillis(1_000_000 + i * 1000),
            kind: SampleKind::HeartRate,
            // a slow ramp with one sharp dip in the middle
            value: if i == 300 { 55 } else { 120 + (i % 5) },
            quality: None,
            source: Source::Live,
        })
        .collect();
    store.store(device, &records).unwrap();
    drop(store);

    let full = service.hr_series(1_000_000, 1_600_000, 10_000).unwrap();
    assert_eq!(full.len(), 600, "under the cap, nothing is dropped");

    let reduced = service.hr_series(1_000_000, 1_600_000, 40).unwrap();
    assert!(reduced.len() <= 60, "reduced to roughly the cap");
    assert!(
        reduced.iter().any(|p| p.bpm == 55),
        "the dip must survive reduction"
    );
    assert!(reduced.iter().any(|p| p.bpm == 124), "so must the peak");

    cleanup(&path);
}
