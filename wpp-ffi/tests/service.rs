use std::sync::{Arc, Mutex};
use wpp_ffi::{AncsLink, Bitmap, Rasterizer, SetEdge, Transport, WatchService};

#[derive(Default)]
struct Recorder {
    written: Mutex<Vec<Vec<u8>>>,
    changes: Mutex<u32>,
    reconnects: Mutex<u32>,
    announced: Mutex<Vec<Vec<u8>>>,
    attributes: Mutex<Vec<Vec<u8>>>,
    glyphs: Mutex<Vec<(u32, u8, u8)>>,
}

struct Handle(Arc<Recorder>);

impl Transport for Handle {
    fn write(&self, frames: Vec<Vec<u8>>) {
        self.0.written.lock().unwrap().extend(frames);
    }
    fn changed(&self) {
        *self.0.changes.lock().unwrap() += 1;
    }
    fn reconnect(&self) {
        *self.0.reconnects.lock().unwrap() += 1;
    }
}

impl AncsLink for Handle {
    fn announce(&self, bytes: Vec<u8>) {
        self.0.announced.lock().unwrap().push(bytes);
    }
    fn attributes(&self, bytes: Vec<u8>) {
        self.0.attributes.lock().unwrap().push(bytes);
    }
}

impl Rasterizer for Handle {
    fn glyph(&self, codepoint: u32, width: u8, height: u8) -> Bitmap {
        self.0
            .glyphs
            .lock()
            .unwrap()
            .push((codepoint, width, height));
        Bitmap {
            width,
            height,
            pixels: vec![0xffff_ffff; width as usize * height as usize],
        }
    }
    fn icon(&self, _app_id: String, width: u8, height: u8) -> Bitmap {
        Bitmap {
            width,
            height,
            pixels: vec![0xffff_ffff; width as usize * height as usize],
        }
    }
    fn activity_glyph(&self, _activity: u32, width: u8, height: u8) -> Bitmap {
        Bitmap {
            width,
            height,
            pixels: vec![0xffff_ffff; width as usize * height as usize],
        }
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
        Box::new(Handle(recorder.clone())),
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
    assert_eq!(written.len(), 1, "the probe, and nothing else");
    let frame = wpp::Frame::parse(&written[0]).expect("valid frame");
    assert_eq!(frame.command.opcode(), wpp::Command::CMD_PROBE.0);

    cleanup(&path);
}

#[test]
fn the_probe_reply_leaves_a_firmware_version_behind() {
    use wpp::objects::ProbeReply;
    use wpp::{Command, Frame, WppObject};

    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    service
        .on_bytes(
            Frame::new(
                Command::CMD_PROBE,
                vec![WppObject::ProbeReply(ProbeReply {
                    vid: 0,
                    pid: 0,
                    name: "ScanWatch 2".to_string(),
                    mac: "a4:7e:fa:44:d6:10".to_string(),
                    secret: String::new(),
                    hard_version: 16777215,
                    mfg_id: "00280074".to_string(),
                    bl_version: 8,
                    soft_version: 3411,
                    rescue_version: 16777215,
                })],
            )
            .to_bytes(),
            1_700_000_000_000,
        )
        .unwrap();

    let device = service.snapshot().unwrap().device.expect("identity");
    assert_eq!(device.name, "ScanWatch 2");
    assert_eq!(device.firmware, 3411);
    assert_eq!(device.bootloader, 8);
    assert_eq!(device.hardware, None);
    assert_eq!(device.rescue, None);

    cleanup(&path);
}

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
    assert_eq!(battery.at_ms, 1_700_000_000_000);

    cleanup(&path);
}

#[test]
fn a_notification_is_announced_then_served_when_the_watch_asks() {
    use wpp_ffi::NotificationCategory;

    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    let id = service.post_notification(
        "dev.davidv.withoutings".into(),
        "Title".into(),
        String::new(),
        "Hello".into(),
        NotificationCategory::Social,
    );

    let announced = recorder.announced.lock().unwrap().clone();
    assert_eq!(announced.len(), 1);
    assert_eq!(announced[0].len(), 8);
    assert_eq!(announced[0][0], 0, "added");
    assert_eq!(announced[0][2], 4, "social");
    assert_eq!(&announced[0][4..], &id.to_be_bytes(), "id, big-endian");

    let mut write = vec![0x00];
    write.extend_from_slice(&id.to_be_bytes());
    write.extend_from_slice(&[0x01, 0x20, 0x00]);
    service.on_ancs_write(write, 128).unwrap();

    let attributes = recorder.attributes.lock().unwrap().clone();
    assert_eq!(attributes.len(), 1, "short enough for one fragment");
    let response = &attributes[0];
    assert_eq!(&response[1..5], &id.to_le_bytes(), "id, little-endian back");
    assert_eq!(
        &response[5..],
        &[0x01, 0x05, 0x00, b'T', b'i', b't', b'l', b'e']
    );

    service.dismiss_notification(id);
    let announced = recorder.announced.lock().unwrap().clone();
    assert_eq!(announced.len(), 2);
    assert_eq!(announced[1][0], 2, "removed");

    recorder.attributes.lock().unwrap().clear();
    let mut write = vec![0x00];
    write.extend_from_slice(&id.to_be_bytes());
    write.push(0x01);
    write.extend_from_slice(&[0x20, 0x00]);
    service.on_ancs_write(write, 128).unwrap();
    assert!(
        recorder.attributes.lock().unwrap().is_empty(),
        "a dismissed notification has nothing to say"
    );

    cleanup(&path);
}

#[test]
fn a_long_message_is_split_across_data_source_fragments() {
    use wpp_ffi::NotificationCategory;

    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    let id = service.post_notification(
        "app".into(),
        String::new(),
        String::new(),
        "x".repeat(100),
        NotificationCategory::Other,
    );
    let mut write = vec![0x00];
    write.extend_from_slice(&id.to_be_bytes());
    write.extend_from_slice(&[0x03, 0xff, 0x00]);
    service.on_ancs_write(write, 20).unwrap();

    let attributes = recorder.attributes.lock().unwrap().clone();
    assert!(attributes.len() > 1, "one notification cannot carry it");
    assert!(attributes.iter().all(|f| f.len() <= 20));
    let rejoined: Vec<u8> = attributes.concat();
    assert_eq!(rejoined.len(), 5 + 3 + 100);
    assert_eq!(&rejoined[8..], "x".repeat(100).as_bytes());

    cleanup(&path);
}

#[test]
fn a_glyph_request_is_answered_with_a_packed_bitmap() {
    use wpp::objects::GlyphId;
    use wpp::{Channel, Command, Frame, WppObject};

    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    let request = Frame::new(
        Command::CMD_GLYPH_GET.with_channel(Channel::SlaveRequest),
        vec![WppObject::GlyphId(GlyphId {
            unicode: 0x41u32.swap_bytes(),
        })],
    );
    service
        .on_bytes(request.to_bytes(), 1_700_000_000_000)
        .unwrap();

    assert_eq!(
        recorder.glyphs.lock().unwrap().clone(),
        vec![(0x41, 22, 22)],
        "the codepoint is unswapped before it reaches the host"
    );

    let written = recorder.written.lock().unwrap().clone();
    let reply = wpp::Frame::parse(written.last().expect("a reply")).expect("valid frame");
    assert_eq!(reply.command.opcode(), Command::CMD_GLYPH_GET.0);
    assert_eq!(reply.command.channel(), Some(Channel::SlaveRequest));
    assert_eq!(
        reply.objects[0],
        WppObject::GlyphId(GlyphId {
            unicode: 0x41u32.swap_bytes()
        }),
        "the id goes back exactly as it came"
    );
    let bits: Vec<u8> = reply
        .objects
        .iter()
        .filter_map(|o| match o {
            WppObject::ImageData(d) => Some(d.data.clone()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .concat();
    assert_eq!(bits.len(), 66);
    assert_eq!(bits[2], 0b0011_1111);

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

#[test]
fn a_walk_arrives_as_windows_and_comes_back_as_one_activity() {
    use wpp::objects::{WamVasistasAwake, WamVasistasDuration, WamVasistasHead};
    use wpp::{Command, Frame, WppObject};

    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    let start = 1_784_969_340;
    let objects: Vec<WppObject> = (0..15)
        .flat_map(|i| {
            [
                WppObject::WamVasistasHead(WamVasistasHead {
                    utc: (start + i * 60) as u32,
                }),
                WppObject::WamVasistasDuration(WamVasistasDuration { duration: 60 }),
                WppObject::WamVasistasAwake(WamVasistasAwake {
                    steps: 95,
                    distance: 7180,
                    ascent: 0,
                    descent: 0,
                }),
            ]
        })
        .collect();
    service
        .on_bytes(
            Frame::new(Command::CMD_WAM_VASISTAS_GET, objects).to_bytes(),
            start as i64 * 1000,
        )
        .unwrap();

    let found = service
        .detected_activities((start as i64 - 3600) * 1000, (start as i64 + 3600) * 1000)
        .unwrap();
    assert_eq!(found.len(), 1);
    assert_eq!(found[0].activity, "Walking");
    assert_eq!(found[0].steps, 15 * 95);
    assert_eq!(found[0].started_at_ms, start as i64 * 1000);
    assert_eq!(found[0].ended_at_ms, (start as i64 + 15 * 60) * 1000);
    assert!((found[0].distance_metres - 15.0 * 71.8).abs() < 0.001);

    cleanup(&path);
}

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
            value: if i == 300 { 55 } else { 120 + (i % 5) },
            quality: None,
            source: Source::Live,
            window_secs: None,
            context: None,
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
