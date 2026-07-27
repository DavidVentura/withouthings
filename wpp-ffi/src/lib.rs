//! The Kotlin-facing surface.
//!
//! Deliberately narrow: flat records rather than domain types, and one call
//! per screenful rather than per sample. The Store -> Stored -> Delete
//! ordering that protects watch data is enforced in here, so no caller can
//! delete a measurement that has not been committed.

use std::sync::Mutex;

use wpp::capture::{FrameReassembler, StreamItem};
use wpp::client::{Action, Category, Client, Credentials, Event, Phase};
use wpp::units::{Celsius, Millivolts, UnixMillis};
use wpp::Frame;
use wpp_store::Store;

uniffi::setup_scaffolding!();

/// Series the watch keeps separately, each walked with its own watermark.
/// Category 0 is the body stream carrying heart rate; the rest are the
/// VasistasType values the official app asks for. Queued so the body stream
/// is taken first.
/// Taken from the end, so the order walked is: body (heart rate), 10 (core
/// temperature), 11 (HRV), 12 (respiratory rate), 6 (activity), then 8, 9, 5 —
/// which carry SpO2 and AHI in bulk and would otherwise starve the rest.
const CATEGORIES: [Category; 8] = [
    Category(5),
    Category(9),
    Category(8),
    Category(6),
    Category(12),
    Category(11),
    Category(10),
    Category::BODY,
];

#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum WatchError {
    // Not named `message`: uniffi maps this onto a Kotlin exception, where
    // that field collides with Throwable.
    #[error("storage: {reason}")]
    Storage { reason: String },
}

impl From<wpp_store::Error> for WatchError {
    fn from(err: wpp_store::Error) -> Self {
        WatchError::Storage {
            reason: err.to_string(),
        }
    }
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum Origin {
    /// Historical series pulled during a sync.
    Stored,
    /// 1 Hz push while connected.
    Live,
}

/// A series the watch records, as the UI refers to it.
#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum Metric {
    HeartRate,
    Temperature,
    HrvSdnn,
    HrvRmssd,
    RespiratoryRate,
    Battery,
    Steps,
}

impl Metric {
    fn kind(self) -> i64 {
        match self {
            Metric::HeartRate => 1,
            Metric::Temperature => 2,
            Metric::HrvSdnn => 3,
            Metric::HrvRmssd => 4,
            Metric::RespiratoryRate => 5,
            Metric::Battery => 6,
            Metric::Steps => 7,
        }
    }

    /// Divisor from the stored wire value to the displayed unit.
    fn scale(self) -> f64 {
        match self {
            Metric::Temperature => 1000.0,
            _ => 1.0,
        }
    }
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum SetEdge {
    Start,
    End,
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum Progress {
    Idle,
    Connecting,
    Syncing,
    Finished,
    /// The watch refused the association secret.
    NotAuthenticated,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct HrPoint {
    pub at_ms: i64,
    pub bpm: u16,
    pub origin: Origin,
}

/// A point of any series, already converted to its display unit.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Point {
    pub at_ms: i64,
    pub value: f64,
    pub origin: Origin,
}

/// The window a series covers, for framing a first view of it.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Extent {
    pub from_ms: i64,
    pub to_ms: i64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct WorkoutSummary {
    pub id: i64,
    pub started_at_ms: i64,
    /// Absent while the workout is still running.
    pub ended_at_ms: Option<i64>,
    pub subcategory: i32,
    /// The sport, named. Unknown ids keep their number rather than being
    /// hidden — the watch may know activities this build does not.
    pub activity: String,
}

/// Where the watch is worn, from `TYPE_TRACKER_WEAR_POS`.
#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum WearPosition {
    NotSet,
    Hip,
    LeftWrist,
    RightWrist,
}

impl WearPosition {
    fn wire(self) -> u8 {
        match self {
            WearPosition::NotSet => 0,
            WearPosition::Hip => 1,
            WearPosition::LeftWrist => 2,
            WearPosition::RightWrist => 3,
        }
    }

    fn of(value: u8) -> WearPosition {
        match value {
            1 => WearPosition::Hip,
            2 => WearPosition::LeftWrist,
            3 => WearPosition::RightWrist,
            _ => WearPosition::NotSet,
        }
    }
}

/// An activity in the watch's quick-launch menu.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Activity {
    pub id: u32,
    pub name: String,
    pub enabled: bool,
}

/// A health feature the watch can be told to measure.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct HealthFeature {
    pub id: u16,
    pub name: String,
    pub description: String,
    pub enabled: bool,
}

/// One screen the watch can cycle through.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct WatchScreen {
    pub id: u8,
    pub name: String,
    pub enabled: bool,
}

/// A reading with the time it was taken, so the UI can say how old it is
/// rather than implying it is current.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Temperature {
    pub celsius: f64,
    pub at_ms: i64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Battery {
    pub percent: u32,
    pub at_ms: i64,
}

/// Daily totals belong to the day the watch counted them, which is not
/// necessarily today.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Steps {
    pub count: u32,
    /// Local midnight of the day these cover, as the watch reported it.
    pub day_start_ms: i64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Marker {
    pub at_ms: i64,
    pub edge: SetEdge,
}

/// What a sync is doing, for a progress indicator that means something.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct SyncProgress {
    /// Fraction of the gap between the last sync and now that has been walked,
    /// 0..1. An estimate: the watch may hold less history than the gap implies.
    pub history_fraction: Option<f64>,
    /// A signal transfer in progress. Exact, unlike the history fraction.
    pub transfer_received: Option<u32>,
    pub transfer_total: Option<u32>,
    /// Records committed since the connection opened.
    pub records_stored: u64,
    /// Streams finished and streams in total; the fraction above covers only
    /// the stream in progress.
    pub streams_done: u32,
    pub streams_total: u32,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Snapshot {
    pub progress: Progress,
    pub battery: Option<Battery>,
    pub latest_hr: Option<HrPoint>,
    pub latest_temperature: Option<Temperature>,
    pub steps: Option<Steps>,
    pub active_workout: Option<WorkoutSummary>,
    /// Data read from the watch but not yet committed here.
    pub pending_deletes: u32,
    pub sync: SyncProgress,
}

/// One lead of a recording, already converted to millivolts.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct EcgLead {
    pub name: String,
    pub millivolts: Vec<f64>,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct EcgRecording {
    pub id: i64,
    pub measured_at_ms: i64,
    pub sampling_hz: u32,
    pub leads: Vec<EcgLead>,
}

/// What the host must do on the Rust side's behalf.
#[uniffi::export(callback_interface)]
pub trait Transport: Send + Sync {
    /// Write one frame to the watch's characteristic.
    fn write(&self, bytes: Vec<u8>);
    /// Something the UI displays has changed; re-query.
    fn changed(&self);
}

struct Inner {
    client: Client,
    reassembler: FrameReassembler,
}

#[derive(uniffi::Object)]
pub struct WatchService {
    inner: Mutex<Inner>,
    store: Mutex<Store>,
    transport: Box<dyn Transport>,
    device_id: i64,
    /// Enabled features as (id, start, end). Write-only on the wire, so this is
    /// the only record of them.
    features: Mutex<Vec<(u16, u32, u32)>>,
}

#[uniffi::export]
impl WatchService {
    #[uniffi::constructor]
    pub fn new(
        db_path: String,
        mac: String,
        secret: String,
        transport: Box<dyn Transport>,
    ) -> Result<Self, WatchError> {
        let store = Store::open(&db_path)?;
        let device_id = store.device(&mac)?;
        let watermarks = store.watermarks(device_id, &CATEGORIES)?;
        let client = Client::new(Credentials { mac, secret }, watermarks);
        Ok(WatchService {
            inner: Mutex::new(Inner {
                client,
                reassembler: FrameReassembler::new(),
            }),
            store: Mutex::new(store),
            transport,
            device_id,
            features: Mutex::new(DEFAULT_FEATURES.iter().map(|id| (*id, 0, 0)).collect()),
        })
    }

    pub fn on_connected(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            // A fresh link starts a fresh byte stream.
            inner.reassembler.reset();
            inner.client.handle(Event::Connected)
        };
        self.dispatch(actions)
    }

    /// Feed one GATT notification. Frames span several of these.
    pub fn on_bytes(&self, bytes: Vec<u8>, received_at_ms: i64) -> Result<(), WatchError> {
        let mut pending = Vec::new();
        {
            let mut inner = self.inner.lock().unwrap();
            let items = inner.reassembler.push(&bytes);
            for item in items {
                if let StreamItem::Frame { frame, .. } = item {
                    pending.extend(inner.client.handle(Event::Frame {
                        frame,
                        received_at: UnixMillis(received_at_ms),
                    }));
                }
            }
        }
        self.dispatch(pending)
    }

    pub fn on_disconnected(&self) {
        self.inner.lock().unwrap().reassembler.reset();
        self.transport.changed();
    }

    /// Walk every stream again from its watermark. Cheap when there is nothing
    /// new: each stream answers with one empty reply.
    pub fn sync_now(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.sync_now();
        self.dispatch(actions)
    }

    /// Bring everything up to date: the numbers the watch does not push
    /// (daily totals, battery) and another pass over the history.
    ///
    /// Asking only for the pushed numbers looks like a refresh that does
    /// nothing, because the readings on screen come from the history walk.
    pub fn request_refresh(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            let mut actions = inner.client.force_refresh();
            actions.extend(inner.client.walk_now());
            actions
        };
        self.dispatch(actions)
    }

    pub fn snapshot(&self) -> Result<Snapshot, WatchError> {
        let store = self.store.lock().unwrap();
        let (phase, pending, progress) = {
            let inner = self.inner.lock().unwrap();
            let now = now_ms();
            let transfer = inner.client.transfer_progress();
            (
                match inner.client.phase() {
                    Phase::Idle => Progress::Idle,
                    Phase::Probing | Phase::Authenticating => Progress::Connecting,
                    Phase::Syncing => Progress::Syncing,
                    Phase::Finished => Progress::Finished,
                    Phase::NotAuthenticated => Progress::NotAuthenticated,
                },
                inner.client.pending_deletes() as u32,
                SyncProgress {
                    history_fraction: inner.client.walk_span().and_then(|(from, at)| {
                        let span = now / 1000 - from.0;
                        // A first sync starts from the epoch, where a fraction
                        // of the gap would be meaningless.
                        if span <= 0 || from.0 <= 0 {
                            None
                        } else {
                            Some(((at.0 - from.0) as f64 / span as f64).clamp(0.0, 1.0))
                        }
                    }),
                    transfer_received: transfer.map(|(got, _)| got as u32),
                    transfer_total: transfer.map(|(_, total)| total as u32),
                    records_stored: inner.client.records_emitted(),
                    streams_done: inner.client.stream_position().0,
                    streams_total: inner.client.stream_position().1,
                },
            )
        };
        let progress_phase = phase;
        Ok(Snapshot {
            progress: progress_phase,
            battery: store.latest(self.device_id, 6)?.map(|(at, value)| Battery {
                percent: value as u32,
                at_ms: at,
            }),
            latest_hr: store.latest(self.device_id, 1)?.map(|(at, value)| HrPoint {
                at_ms: at,
                bpm: value as u16,
                origin: Origin::Stored,
            }),
            latest_temperature: store
                .latest(self.device_id, 2)?
                .map(|(at, value)| Temperature {
                    celsius: Celsius(value as f64 / 1000.0).0,
                    at_ms: at,
                }),
            steps: store.latest(self.device_id, 7)?.map(|(at, value)| Steps {
                count: value as u32,
                day_start_ms: at,
            }),
            active_workout: store
                .active_workout(self.device_id)?
                .map(|(id, start, sub)| WorkoutSummary {
                    id,
                    started_at_ms: start * 1000,
                    ended_at_ms: None,
                    subcategory: sub as i32,
                    activity: activity_name(sub as u32),
                }),
            pending_deletes: pending,
            sync: progress,
        })
    }

    /// Heart rate over a window, reduced to at most `max_points` for drawing.
    /// The reduction keeps peaks and troughs; the stored data is untouched.
    pub fn hr_series(
        &self,
        from_ms: i64,
        to_ms: i64,
        max_points: u32,
    ) -> Result<Vec<HrPoint>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .series(self.device_id, 1, from_ms, to_ms, max_points)?
            .into_iter()
            .map(|(at, value, source)| HrPoint {
                at_ms: at,
                bpm: value as u16,
                origin: if source == 1 {
                    Origin::Live
                } else {
                    Origin::Stored
                },
            })
            .collect())
    }

    /// Any series over a window, reduced for drawing the same way heart rate is.
    pub fn series(
        &self,
        metric: Metric,
        from_ms: i64,
        to_ms: i64,
        max_points: u32,
    ) -> Result<Vec<Point>, WatchError> {
        let store = self.store.lock().unwrap();
        let scale = metric.scale();
        Ok(store
            .series(self.device_id, metric.kind(), from_ms, to_ms, max_points)?
            .into_iter()
            .map(|(at, value, source)| Point {
                at_ms: at,
                value: value as f64 / scale,
                origin: if source == 1 {
                    Origin::Live
                } else {
                    Origin::Stored
                },
            })
            .collect())
    }

    /// Most recent value of a series, in its display unit.
    pub fn latest_value(&self, metric: Metric) -> Result<Option<Point>, WatchError> {
        let store = self.store.lock().unwrap();
        let scale = metric.scale();
        Ok(store
            .latest(self.device_id, metric.kind())?
            .map(|(at, value)| Point {
                at_ms: at,
                value: value as f64 / scale,
                origin: Origin::Stored,
            }))
    }

    /// When a series starts and ends, or nothing if it has no samples.
    pub fn extent(&self, metric: Metric) -> Result<Option<Extent>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .extent(self.device_id, metric.kind())?
            .map(|(from_ms, to_ms)| Extent { from_ms, to_ms }))
    }

    pub fn workouts(&self, limit: u32) -> Result<Vec<WorkoutSummary>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .workouts(self.device_id, limit)?
            .into_iter()
            .map(|(id, start, end, sub)| WorkoutSummary {
                id,
                started_at_ms: start * 1000,
                ended_at_ms: end.map(|e| e * 1000),
                subcategory: sub as i32,
                activity: activity_name(sub as u32),
            })
            .collect())
    }

    /// Screens the watch shows, enabled ones first in the order it cycles them.
    /// Empty until the watch has answered [`Self::request_screens`].
    pub fn screens(&self) -> Vec<WatchScreen> {
        let current = self.inner.lock().unwrap().client.screens();
        let Some(current) = current else {
            return Vec::new();
        };
        let mut screens: Vec<WatchScreen> = current
            .iter()
            .map(|id| WatchScreen {
                id: *id,
                name: screen_name(*id),
                enabled: true,
            })
            .collect();
        // Only offer screens we can name. An unnamed number is one this watch
        // has never reported having, and enabling it is a shot in the dark.
        for (id, name) in KNOWN_SCREENS {
            if !current.contains(id) {
                screens.push(WatchScreen {
                    id: *id,
                    name: name.to_string(),
                    enabled: false,
                });
            }
        }
        screens
    }

    pub fn request_screens(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.request_screens();
        self.dispatch(actions)
    }

    /// Replace the watch's screens. The order given is the order it cycles.
    pub fn set_screens(&self, ids: Vec<u8>) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.set_screens(&ids);
        self.dispatch(actions)
    }

    pub fn wear_position(&self) -> WearPosition {
        WearPosition::of(
            self.inner
                .lock()
                .unwrap()
                .client
                .wear_position()
                .unwrap_or(0),
        )
    }

    pub fn set_wear_position(&self, position: WearPosition) -> Result<(), WatchError> {
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .set_wear_position(position.wire());
        self.dispatch(actions)
    }

    /// Read the quick-launch menu and wear position from the watch.
    pub fn request_device_config(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.request_device_config();
        self.dispatch(actions)
    }

    /// The quick-launch menu, chosen ones first in the order the watch shows
    /// them, then everything else that can be added.
    pub fn activities(&self) -> Vec<Activity> {
        let current = self
            .inner
            .lock()
            .unwrap()
            .client
            .activities()
            .unwrap_or_default();
        let mut activities: Vec<Activity> = current
            .iter()
            .map(|id| Activity {
                id: *id,
                name: activity_name(*id),
                enabled: true,
            })
            .collect();
        for (id, name) in ACTIVITIES {
            if !current.contains(id) {
                activities.push(Activity {
                    id: *id,
                    name: name.to_string(),
                    enabled: false,
                });
            }
        }
        activities
    }

    pub fn set_activities(&self, ids: Vec<u32>) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.set_activities(&ids);
        self.dispatch(actions)
    }

    /// Health features, as last written by this app.
    ///
    /// The watch has no read side for these, so this reflects what we told it,
    /// seeded from what the official app had configured.
    pub fn health_features(&self) -> Vec<HealthFeature> {
        let enabled = self.features.lock().unwrap().clone();
        HEALTH_FEATURES
            .iter()
            .map(|(id, name, description)| HealthFeature {
                id: *id,
                name: name.to_string(),
                description: description.to_string(),
                enabled: enabled.iter().any(|(known, _, _)| known == id),
            })
            .collect()
    }

    /// Turn one feature on or off, sending the whole set as the protocol
    /// requires. Features this app does not know about are carried through
    /// untouched rather than being silently switched off.
    pub fn set_health_feature(&self, id: u16, enabled: bool) -> Result<(), WatchError> {
        let features = {
            let mut features = self.features.lock().unwrap();
            features.retain(|(known, _, _)| *known != id);
            if enabled {
                features.push((id, 0, 0));
            }
            features.clone()
        };
        let actions = self.inner.lock().unwrap().client.set_features(&features);
        self.dispatch(actions)
    }

    pub fn mark_set(&self, at_ms: i64, edge: SetEdge) -> Result<(), WatchError> {
        let store = self.store.lock().unwrap();
        store.mark_set(
            self.device_id,
            at_ms,
            if edge == SetEdge::End { 1 } else { 0 },
        )?;
        drop(store);
        self.transport.changed();
        Ok(())
    }

    pub fn markers(&self, from_ms: i64, to_ms: i64) -> Result<Vec<Marker>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .markers(self.device_id, from_ms, to_ms)?
            .into_iter()
            .map(|(at_ms, edge)| Marker {
                at_ms,
                edge: if edge == 1 {
                    SetEdge::End
                } else {
                    SetEdge::Start
                },
            })
            .collect())
    }

    /// A whole recording in one call; crossing per-sample would be absurd at
    /// 300 Hz across two leads.
    pub fn ecg(&self, id: i64) -> Result<Option<EcgRecording>, WatchError> {
        let store = self.store.lock().unwrap();
        let Some((measured_at, signal_type, hz, lead_count, samples)) = store.ecg(id)? else {
            return Ok(None);
        };
        let names = wpp::signal::SignalKind::from_type_id(signal_type as u16)
            .map(|k| k.leads().iter().map(|l| l.name().to_string()).collect())
            .unwrap_or_else(Vec::new);
        let lead_count = lead_count.max(1) as usize;
        let mut leads: Vec<EcgLead> = (0..lead_count)
            .map(|i| EcgLead {
                name: names.get(i).cloned().unwrap_or_else(|| format!("CH{i}")),
                millivolts: Vec::new(),
            })
            .collect();
        for (index, pair) in samples.chunks_exact(2).enumerate() {
            let counts = i16::from_le_bytes([pair[0], pair[1]]);
            leads[index % lead_count]
                .millivolts
                .push(Millivolts::from_counts(counts).0);
        }
        Ok(Some(EcgRecording {
            id,
            measured_at_ms: measured_at * 1000,
            sampling_hz: hz as u32,
            leads,
        }))
    }
}

/// `WAM_SCREEN_MAX_NUMBER` from the app.
const MAX_SCREEN_ID: u8 = 24;

/// Screen numbers and names, recovered from the official app's `DeviceScreen`
/// table by pairing `embeddedId` with `name`.
///
/// That table is populated per device from Withings' API, so another model may
/// number its screens differently.
const KNOWN_SCREENS: &[(u8, &str)] = &[
    (1, "Steps"),
    (2, "Distance"),
    (3, "Calories"),
    (4, "Heart rate"),
    (6, "Date"),
    (9, "Electrocardiogram (ECG)"),
    (10, "Oxygen saturation (SpO2)"),
    (11, "Workouts"),
    (12, "Elevation"),
    (16, "Clock"),
    (17, "Settings"),
    (18, "Breathe"),
    (20, "Body temperature"),
    (21, "Cycle tracking"),
    (22, "Sleep"),
];

fn screen_name(id: u8) -> String {
    KNOWN_SCREENS
        .iter()
        .find(|(known, _)| *known == id)
        .map(|(_, name)| name.to_string())
        .unwrap_or_else(|| format!("Screen {id}"))
}

/// What the official app had enabled on this watch, read out of its
/// `PlatformFeature` table. Used as the starting set because the watch will not
/// say what it currently has.
const DEFAULT_FEATURES: &[u16] = &[3, 5, 9, 10, 11, 14, 17, 19, 20, 27, 53, 71, 88, 113];

/// User-facing features, named from `ConstantsWs.FEATURE_ID_*`.
const HEALTH_FEATURES: &[(u16, &str, &str)] = &[
    (17, "Signs of AFib", "Monitor for irregular heartbeat"),
    (
        14,
        "AFib (medical grade)",
        "Medically certified AFib detection",
    ),
    (5, "SpO2 during sleep", "Measure blood oxygen while asleep"),
    (3, "SpO2", "Blood oxygen measurement"),
    (10, "Respiratory scan", "Breathing rate measurement"),
    (
        11,
        "Respiratory scan (smart)",
        "Choose when to measure automatically",
    ),
    (
        9,
        "Respiratory monitoring",
        "Continuous respiratory monitoring",
    ),
    (71, "Body temperature", "Skin and core temperature"),
    (27, "Electrocardiogram", "On-demand ECG recording"),
    (53, "ECG intervals", "QT, QTc, PQ and QRS from the ECG"),
    (
        22,
        "Resting heart rate alerts",
        "Alert on unusually high or low resting rate",
    ),
    (
        26,
        "Automatic resting HR alerts",
        "Set the alert thresholds automatically",
    ),
    (70, "Cycle tracking", "Menstrual cycle logging"),
    (20, "Activity tracking", "Steps, distance and calories"),
    (19, "Notifications", "Phone notifications on the watch"),
];

/// Activity ids and names, from `ConstantsWs.WITHINGS_ACTIVITY_SUBCATEGORY_*`.
const ACTIVITIES: &[(u32, &str)] = &[
    (1, "Walking"),
    (2, "Running"),
    (3, "Hiking"),
    (4, "Skating"),
    (5, "BMX"),
    (6, "Cycling"),
    (7, "Swimming"),
    (8, "Surfing"),
    (9, "Kitesurfing"),
    (10, "Windsurfing"),
    (11, "Bodyboard"),
    (12, "Tennis"),
    (13, "Table tennis"),
    (14, "Squash"),
    (15, "Badminton"),
    (16, "Weights"),
    (17, "Calisthenics"),
    (18, "Elliptical"),
    (19, "Pilates"),
    (20, "Basketball"),
    (21, "Soccer"),
    (22, "Football"),
    (23, "Rugby"),
    (24, "Volleyball"),
    (25, "Water polo"),
    (26, "Horse riding"),
    (27, "Golf"),
    (28, "Yoga"),
    (29, "Dancing"),
    (30, "Boxing"),
    (31, "Fencing"),
    (32, "Wrestling"),
    (33, "Martial arts"),
    (34, "Skiing"),
    (35, "Snowboarding"),
    (36, "Other"),
    (187, "Rowing"),
    (188, "Zumba"),
    (191, "Baseball"),
    (192, "Handball"),
    (193, "Hockey"),
    (194, "Ice hockey"),
    (195, "Climbing"),
    (196, "Ice skating"),
    (306, "Indoor walk"),
    (307, "Indoor running"),
];

fn activity_name(id: u32) -> String {
    ACTIVITIES
        .iter()
        .find(|(known, _)| *known == id)
        .map(|(_, name)| name.to_string())
        .unwrap_or_else(|| format!("Activity {id}"))
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

impl WatchService {
    /// Run the actions a step produced. Deletes only ever appear here as the
    /// result of a commit, so watch data cannot be dropped before it is safe.
    fn dispatch(&self, mut actions: Vec<Action>) -> Result<(), WatchError> {
        let mut changed = false;
        while let Some(action) = actions.pop() {
            match action {
                Action::Send(frame) | Action::Delete(frame) => self.write(&frame),
                Action::Finished => changed = true,
                Action::Store { token, records } => {
                    self.store.lock().unwrap().store(self.device_id, &records)?;
                    changed = true;
                    let released = self
                        .inner
                        .lock()
                        .unwrap()
                        .client
                        .handle(Event::Stored { token });
                    actions.extend(released);
                }
            }
        }

        let watermarks = self.inner.lock().unwrap().client.watermarks();
        let store = self.store.lock().unwrap();
        for (category, through) in watermarks {
            store.set_watermark(self.device_id, category, through)?;
        }
        drop(store);

        if changed {
            self.transport.changed();
        }
        Ok(())
    }

    fn write(&self, frame: &Frame) {
        self.transport.write(frame.to_bytes());
    }
}
