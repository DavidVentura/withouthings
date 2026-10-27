use std::sync::{Arc, Mutex};
use wpp_ffi::{AncsLink, Bitmap, LocationFix, Rasterizer, SetEdge, Transport, WatchService};

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

fn db_path() -> String {
    format!(
        "/tmp/wpp-ffi-test-{}.db",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )
}

// The store takes the database exclusively for as long as it is open, so a test
// that wants rows in place beforehand has to seed them and close before this.
fn service_at(recorder: &Arc<Recorder>, path: String) -> WatchService {
    WatchService::new(
        path,
        "a4:7e:fa:44:d6:10".to_string(),
        "gUf8Np69A4GvJxjY1XOcIHKQm2HcPZnO".to_string(),
        Box::new(Handle(recorder.clone())),
        Box::new(Handle(recorder.clone())),
        Box::new(Handle(recorder.clone())),
    )
    .expect("service")
}

fn service(recorder: &Arc<Recorder>) -> (WatchService, String) {
    let path = db_path();
    let service = service_at(recorder, path.clone());
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
fn a_fresh_battery_status_carries_the_watch_s_own_state() {
    use wpp::objects::BatteryStatus;
    use wpp::{Command, Frame, WppObject};
    use wpp_ffi::BatteryState;

    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;

    let frame = Frame::new(
        Command::CMD_BATTERY_STATUS,
        vec![WppObject::BatteryStatus(BatteryStatus {
            battery_percent: 9,
            battery_state: BatteryStatus::BATTERY_STATE_CRITICAL,
            battery_mv: 3500,
            reserved: 0,
        })],
    );
    service.on_bytes(frame.to_bytes(), now).unwrap();

    let battery = service.snapshot().unwrap().battery.expect("battery reading");
    assert_eq!(battery.percent, 9);
    assert_eq!(battery.state, Some(BatteryState::Critical));
    assert_eq!(battery.charging, Some(false));

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
    assert_eq!(&announced[0][4..], &id.to_le_bytes(), "id, little-endian");

    let mut write = vec![0x00];
    write.extend_from_slice(&id.to_le_bytes());
    write.extend_from_slice(&[0x01, 0x20, 0x00]);
    service.on_ancs_write(write, 128).unwrap();

    let attributes = recorder.attributes.lock().unwrap().clone();
    assert_eq!(attributes.len(), 1, "short enough for one fragment");
    let response = &attributes[0];
    assert_eq!(&response[1..5], &id.to_le_bytes(), "id, little-endian");
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
    write.extend_from_slice(&id.to_le_bytes());
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
    write.extend_from_slice(&id.to_le_bytes());
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
    let path = db_path();

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

    let service = service_at(&recorder, path.clone());
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

#[test]
fn trimming_a_session_moves_its_end_and_forgets_the_sets_after_it() {
    use wpp::client::Record;
    use wpp::units::UnixTime;

    let recorder = Arc::new(Recorder::default());
    let path = db_path();

    let mut store = wpp_store::Store::open(&path).unwrap();
    let device = store.device("a4:7e:fa:44:d6:10").unwrap();
    store
        .store(
            device,
            &[
                Record::WorkoutStarted {
                    started_at: UnixTime(1_784_000_000),
                    subcategory: 16,
                },
                Record::WorkoutEnded {
                    started_at: UnixTime(1_784_000_000),
                    ended_at: UnixTime(1_784_010_000),
                    paused_secs: 0,
                },
            ],
        )
        .unwrap();
    drop(store);

    let service = service_at(&recorder, path.clone());
    service.mark_set(1_784_001_000_000, SetEdge::Start).unwrap();
    service.mark_set(1_784_002_000_000, SetEdge::End).unwrap();
    service.mark_set(1_784_008_000_000, SetEdge::Start).unwrap();

    let id = service.workouts(10).unwrap()[0].id;
    let trimmed = service.trim_workout(id, 1_784_003_000_000).unwrap();

    assert_eq!(trimmed.ended_at_ms, Some(1_784_003_000_000));
    assert_eq!(
        service.workouts(10).unwrap()[0].ended_at_ms,
        Some(1_784_003_000_000)
    );
    assert_eq!(
        service.markers(0, 1_785_000_000_000).unwrap().len(),
        2,
        "the set timed after the new end goes with the stretch cut off"
    );

    assert!(
        service.trim_workout(id, 1_784_005_000_000).is_err(),
        "an end later than the one recorded is refused"
    );

    cleanup(&path);
}

#[test]
fn a_toggle_and_save_leaves_an_armed_scan_alone() {
    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    service.arm_respiratory_scan().unwrap();
    let armed = service.respiratory_scan().expect("armed");

    // What the sensors screen does on save: one call per changed row.
    service.set_health_feature(27, false).unwrap();
    recorder.written.lock().unwrap().clear();
    service.set_health_feature(27, true).unwrap();

    assert_eq!(service.respiratory_scan(), Some(armed.clone()));
    assert!(
        service
            .health_features()
            .iter()
            .any(|f| f.id == 27 && f.enabled),
        "the toggled feature came back on"
    );

    // A set this long does not fit one frame, so the write arrives split the
    // way the reference app splits it.
    let sent: Vec<(u16, u32, u32)> = recorder
        .written
        .lock()
        .unwrap()
        .iter()
        .filter_map(|buf| wpp::Frame::parse(buf).ok())
        .inspect(|f| {
            assert_eq!(
                f.command.opcode(),
                wpp::Command::CMD_FEATURE_TAGS_SET_DEPRECATED_V2.0
            )
        })
        .flat_map(|f| {
            f.objects
                .iter()
                .filter_map(|o| match o {
                    wpp::WppObject::FeatureTagsDeprecated(t) => {
                        Some((t.id, t.start_time, t.end_time))
                    }
                    _ => None,
                })
                .collect::<Vec<_>>()
        })
        .collect();
    let scan = sent
        .iter()
        .find(|(id, _, _)| *id == 9)
        .expect("the scan is still in the set that went to the watch");
    assert_eq!(scan.2 as i64, armed.ends_at);
    assert!(scan.1 > 0, "and still as a window, not permanent");
    assert!(sent.iter().any(|(id, _, _)| *id == 27), "as is the toggle");

    drop(service);
    let service = service_at(&recorder, path.clone());
    assert_eq!(
        service.respiratory_scan(),
        Some(armed),
        "and it survives a restart"
    );

    drop(service);
    cleanup(&path);
}

const CYCLING: i32 = 6;
const WEIGHTS: i32 = 16;

fn fix(at_ms: i64, lat: f64, lon: f64) -> LocationFix {
    LocationFix {
        at_ms,
        lat_e7: (lat * 1e7).round() as i32,
        lon_e7: (lon * 1e7).round() as i32,
        altitude_cm: Some(200),
        accuracy_cm: Some(600),
        speed_mm_s: None,
        bearing_cdeg: None,
    }
}

/// A ride out along one street, roughly 6 m/s, one fix a second.
fn ride() -> Vec<LocationFix> {
    (0..60)
        .map(|second| {
            fix(
                1_700_000_000_000 + second * 1_000,
                52.3700 + second as f64 * 0.000_054,
                4.8900,
            )
        })
        .collect()
}

#[test]
fn a_recorded_route_comes_back_measured() {
    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    service.record_fixes(ride()).unwrap();
    let track = service
        .track(1_700_000_000_000, 1_700_000_060_000, CYCLING)
        .unwrap()
        .expect("a route was recorded");

    let summary = track.summary();
    assert_eq!(summary.fixes, 60);
    assert!(
        (summary.distance_metres - 354.0).abs() < 10.0,
        "{} m is not the ride that was recorded",
        summary.distance_metres
    );
    assert_eq!(summary.moving_secs, 59);
    let speed = summary.average_speed_m_s.expect("under way throughout");
    assert!((speed - 6.0).abs() < 0.3, "{speed} m/s");

    cleanup(&path);
}

#[test]
fn an_activity_that_covers_no_ground_has_no_route_to_read() {
    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    service.record_fixes(ride()).unwrap();

    assert!(
        service
            .track(1_700_000_000_000, 1_700_000_060_000, WEIGHTS)
            .unwrap()
            .is_none(),
        "fixes recorded under a session on the spot are not a route"
    );

    cleanup(&path);
}

#[test]
fn the_table_says_which_activities_are_worth_a_receiver() {
    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);
    service.record_fixes(ride()).unwrap();

    let reads_a_route = |subcategory: i32| {
        service
            .track(1_700_000_000_000, 1_700_000_060_000, subcategory)
            .unwrap()
            .is_some()
    };

    for (subcategory, name) in [(6, "cycling"), (2, "running"), (34, "skiing")] {
        assert!(reads_a_route(subcategory), "{name} covers ground");
    }
    for (subcategory, name) in [
        (16, "weights"),
        (307, "indoor running"),
        (7, "swimming"),
        (9_999, "an activity the table has never heard of"),
    ] {
        assert!(!reads_a_route(subcategory), "{name} stays put");
    }

    cleanup(&path);
}

#[test]
fn a_map_frame_covers_the_box_it_was_fitted_to() {
    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    service.record_fixes(ride()).unwrap();
    let track = service
        .track(1_700_000_000_000, 1_700_000_060_000, CYCLING)
        .unwrap()
        .expect("a route was recorded");

    let view = track.fit(400.0, 300.0, 16.0);
    let frame = track.frame(view);

    assert!(!frame.tiles.is_empty());
    assert_eq!(frame.path.len(), 1, "one unbroken run");
    let drawn = &frame.path[0].points;
    assert!(drawn.len() >= 2);
    assert_eq!(drawn[0].at_ms, 1_700_000_000_000);
    assert_eq!(
        drawn[drawn.len() - 1].at_ms,
        1_700_000_059_000,
        "the ends are kept so a scrub can reach them"
    );

    cleanup(&path);
}

#[test]
fn a_ride_with_a_route_is_not_charged_at_the_rate_the_heart_read() {
    use wpp::client::{Record, SampleKind, Source};
    use wpp::units::{UnixMillis, UnixTime};
    use wpp_store::Store;

    const START: i64 = 1_700_000_000;
    let path = db_path();
    {
        // Seeded before the service opens the file, which takes it exclusively.
        let mut store = Store::open(&path).unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let mut records = vec![
            Record::User(wpp::client::UserProfile {
                id: 1,
                weight: 73_000,
                height: 175,
                gender: 0,
                birth: 738_892_800,
                first_name: String::new(),
            }),
            Record::WorkoutStarted {
                started_at: UnixTime(START),
                subcategory: CYCLING as i16,
            },
            Record::WorkoutEnded {
                started_at: UnixTime(START),
                ended_at: UnixTime(START + 600),
                paused_secs: 0,
            },
        ];
        // Ten minutes at 155 bpm, which Keytel reads as far harder work than
        // sixteen kilometres an hour actually is.
        for second in (0..600).step_by(10) {
            records.push(Record::Sample {
                measured_at: UnixMillis((START + second) * 1000),
                kind: SampleKind::HeartRate,
                value: 155,
                quality: Some(4),
                source: Source::Live,
                window_secs: None,
                context: None,
            });
        }
        store.store(device, &records).unwrap();
    }

    let recorder = Arc::new(Recorder::default());
    let service = service_at(&recorder, path.clone());

    let by_heart_rate = service.workouts(5).unwrap()[0]
        .calories
        .expect("a wearer is on file");

    // 2.66 km in ten minutes: 15.96 km/h, the Compendium's slowest band.
    let ride: Vec<LocationFix> = (0..600)
        .map(|second| {
            fix(
                (START + second) * 1000,
                52.3700 + second as f64 * 0.0000399,
                4.8900,
            )
        })
        .collect();
    service.record_fixes(ride).unwrap();

    let by_ground = service.workouts(5).unwrap()[0]
        .calories
        .expect("still on file");

    assert!(
        by_heart_rate > 130.0,
        "{by_heart_rate} kcal is not the overestimate this is about"
    );
    assert!(
        (by_ground - 75.0).abs() < 12.0,
        "{by_ground} kcal against the 75 the tables give for this ride"
    );

    cleanup(&path);
}

#[test]
fn hand_setting_goes_out_only_on_a_link_and_one_move_at_a_time() {
    use wpp::{Command, Frame};
    use wpp_ffi::{HandsPhase, WatchHand};

    // The session runs on the phone's clock, so the replies must arrive on it too.
    let t0 = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    assert_eq!(service.hands_start().unwrap().phase, HandsPhase::WaitingForWatch);
    assert!(recorder.written.lock().unwrap().is_empty(), "nothing before the watch is linked");

    service.on_connected().unwrap();
    service.on_bytes(Frame::new(Command::CMD_PROBE, Vec::new()).to_bytes(), t0 + 0).unwrap();
    recorder.written.lock().unwrap().clear();

    assert_eq!(service.hands_start().unwrap().phase, HandsPhase::Starting);
    service.on_bytes(Frame::new(Command::CMD_HANDS_CAL_START, Vec::new()).to_bytes(), t0 + 1).unwrap();
    let session = service.hands_nudge(WatchHand::Minute, -6).unwrap();
    assert_eq!(session.minute_steps, -6);
    service.hands_nudge(WatchHand::Minute, -6).unwrap();
    service.hands_save().unwrap();

    let written: Vec<Frame> = recorder
        .written
        .lock()
        .unwrap()
        .iter()
        .map(|bytes| Frame::parse(bytes).expect("valid frame"))
        .collect();
    let commands: Vec<u16> = written.iter().map(|f| f.command.opcode()).collect();
    assert_eq!(
        commands,
        vec![Command::CMD_HANDS_CAL_START.0, Command::CMD_HANDS_MOVE.0],
        "the second tap and the stop wait for the first move's answer"
    );

    service.on_bytes(Frame::new(Command::CMD_HANDS_MOVE, Vec::new()).to_bytes(), t0 + 2).unwrap();
    service.on_bytes(Frame::new(Command::CMD_HANDS_MOVE, Vec::new()).to_bytes(), t0 + 3).unwrap();
    assert_eq!(service.hands_session().unwrap().phase, HandsPhase::Saving);
    service.on_bytes(Frame::new(Command::CMD_HANDS_CAL_STOP, Vec::new()).to_bytes(), t0 + 4).unwrap();
    let session = service.hands_session().unwrap();
    assert_eq!(session.phase, HandsPhase::Saved);
    assert_eq!(session.minute_steps, -12);

    cleanup(&path);
}

#[test]
fn the_watch_clock_is_what_the_watch_answered_and_a_set_goes_out_and_is_read_back() {
    use wpp::objects::{TimeSet, TimeSetReply};
    use wpp::{Command, Frame, WppObject};
    use wpp_ffi::{ClockReadState, ClockSetState};

    let t0 = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let recorder = Arc::new(Recorder::default());
    let (service, path) = service(&recorder);

    let view = service.clock_read().unwrap();
    assert_eq!(view.read_state, ClockReadState::WaitingForWatch);
    assert_eq!(view.watch_utc_ms, None, "no time is shown before the watch answers");
    assert!(recorder.written.lock().unwrap().is_empty());

    service.on_connected().unwrap();
    service.on_bytes(Frame::new(Command::CMD_PROBE, Vec::new()).to_bytes(), t0).unwrap();
    recorder.written.lock().unwrap().clear();

    assert_eq!(service.watch_clock().unwrap().read_state, ClockReadState::Asking);
    let written = recorder.written.lock().unwrap().clone();
    assert_eq!(written, vec![Frame::new(Command::CMD_TIME_GET, Vec::new()).to_bytes()]);

    let watch_s = (t0 / 1000) - 90;
    service
        .on_bytes(
            Frame::new(
                Command::CMD_TIME_GET,
                vec![WppObject::TimeSet(TimeSet {
                    utc: watch_s as u32,
                    gmt_offset: 0,
                    dst_change_time: 0,
                    next_gmt_offset: 0,
                })],
            )
            .to_bytes(),
            t0 + 10,
        )
        .unwrap();
    let view = service.watch_clock().unwrap();
    assert_eq!(view.read_state, ClockReadState::Answered);
    assert_eq!(view.watch_utc_ms, Some(watch_s * 1000));
    assert_eq!(view.offset_ms, Some(watch_s * 1000 - (t0 + 10)));

    recorder.written.lock().unwrap().clear();
    let view = service.clock_set_from_phone(3600, None).unwrap();
    assert_eq!(view.set_state, ClockSetState::Sending);
    assert!(view.last_sync_from_screen);
    let written: Vec<Frame> = recorder
        .written
        .lock()
        .unwrap()
        .iter()
        .map(|b| Frame::parse(b).unwrap())
        .collect();
    assert_eq!(written.len(), 1);
    assert_eq!(written[0].command, Command::CMD_TIME_SET);
    match &written[0].objects[0] {
        WppObject::TimeSet(t) => {
            assert_eq!(t.gmt_offset, 3600);
            assert!((t.utc as i64 - t0 / 1000).abs() <= 5, "the phone's own second");
        }
        other => panic!("{other:?}"),
    }

    recorder.written.lock().unwrap().clear();
    service
        .on_bytes(
            Frame::new(
                Command::CMD_TIME_SET,
                vec![WppObject::TimeSetReply(TimeSetReply { drift: 0 })],
            )
            .to_bytes(),
            t0 + 20,
        )
        .unwrap();
    let view = service.watch_clock().unwrap();
    assert_eq!(view.set_state, ClockSetState::Set);
    assert_eq!(view.read_state, ClockReadState::Asking, "read back after the set");
    assert_eq!(
        recorder.written.lock().unwrap().clone(),
        vec![Frame::new(Command::CMD_TIME_GET, Vec::new()).to_bytes()]
    );

    cleanup(&path);
}
