use std::sync::Mutex;

use wpp::activity;
use wpp::ancs::{self, NotificationCenter, NotificationId};
use wpp::capture::{FrameReassembler, StreamItem};
use wpp::client::{Action, Category, Client, Credentials, Event, Phase};
use wpp::image::{GlyphRequest, IconRequest, Mono};
use wpp::pairing::{Pairing, PairingState};
use wpp::units::{Celsius, Millivolts, UnixMillis, UnixTime};
use wpp::Frame;
use wpp_store::Store;

uniffi::setup_scaffolding!();

fn origin_of(source: i64) -> Origin {
    if source == 1 {
        Origin::Live
    } else {
        Origin::Stored
    }
}

const CATEGORIES: [Category; 9] = [
    Category(5),
    Category(9),
    Category(8),
    Category(6),
    Category(12),
    Category(11),
    Category(10),
    Category::ACTIVITY,
    Category::BODY,
];

#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum WatchError {
    #[error("storage: {reason}")]
    Storage { reason: String },
    #[error("protocol: {reason}")]
    Protocol { reason: String },
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
    Stored,
    Live,
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum Metric {
    HeartRate,
    Temperature,
    HrvSdnn,
    HrvRmssd,
    RespiratoryRate,
    Battery,
    Steps,
    Spo2,
    Ascent,
    Calories,
    Distance,
    TrackedDuration,
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
            Metric::Spo2 => 11,
            Metric::Ascent => 12,
            Metric::Calories => 13,
            Metric::Distance => 14,
            Metric::TrackedDuration => 15,
        }
    }

    fn scale(self) -> f64 {
        match self {
            Metric::Temperature => 1000.0,
            Metric::Ascent | Metric::Distance | Metric::Calories => 100.0,
            Metric::TrackedDuration => 3600.0,
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
    NotAuthenticated,
}

impl Progress {
    fn of(phase: Phase) -> Progress {
        match phase {
            Phase::Idle => Progress::Idle,
            Phase::Probing | Phase::Authenticating => Progress::Connecting,
            Phase::Syncing => Progress::Syncing,
            Phase::Finished => Progress::Finished,
            Phase::NotAuthenticated => Progress::NotAuthenticated,
        }
    }
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct HrPoint {
    pub at_ms: i64,
    pub bpm: u16,
    pub origin: Origin,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Point {
    pub at_ms: i64,
    pub value: f64,
    pub origin: Origin,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Extent {
    pub from_ms: i64,
    pub to_ms: i64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct WorkoutSummary {
    pub id: i64,
    pub started_at_ms: i64,
    pub ended_at_ms: Option<i64>,
    pub subcategory: i32,
    pub activity: String,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct ActivityTotals {
    pub steps: i64,
    pub distance_metres: f64,
    pub ascent_metres: f64,
    pub calories: f64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct DetectedActivity {
    pub started_at_ms: i64,
    pub ended_at_ms: i64,
    pub subcategory: i32,
    pub activity: String,
    pub steps: i64,
    pub distance_metres: f64,
    pub calories: f64,
}

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

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct DstChange {
    pub at_ms: i64,
    pub gmt_offset_seconds: i32,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Activity {
    pub id: u32,
    pub name: String,
    pub enabled: bool,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct HealthFeature {
    pub id: u16,
    pub name: String,
    pub description: String,
    pub enabled: bool,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct WatchScreen {
    pub id: u8,
    pub name: String,
    pub enabled: bool,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Temperature {
    pub celsius: f64,
    pub at_ms: i64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Battery {
    pub percent: u32,
    pub at_ms: i64,
    pub charging: Option<bool>,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Steps {
    pub count: u32,
    pub day_start_ms: i64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Marker {
    pub at_ms: i64,
    pub edge: SetEdge,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct SleepBand {
    pub from_ms: i64,
    pub to_ms: i64,
    pub stage: SleepStage,
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum SleepStage {
    Awake,
    Light,
    Deep,
    Rem,
}

fn merge_adjacent(bands: impl Iterator<Item = SleepBand>) -> Vec<SleepBand> {
    bands.fold(Vec::new(), |mut out: Vec<SleepBand>, band| {
        match out.last_mut() {
            Some(last) if last.stage == band.stage && last.to_ms >= band.from_ms => {
                last.to_ms = last.to_ms.max(band.to_ms);
            }
            _ => out.push(band),
        }
        out
    })
}

impl SleepStage {
    fn of(level: activity::SleepLevel) -> SleepStage {
        match level {
            activity::SleepLevel::Awake => SleepStage::Awake,
            activity::SleepLevel::Light => SleepStage::Light,
            activity::SleepLevel::Deep => SleepStage::Deep,
            activity::SleepLevel::Rem => SleepStage::Rem,
        }
    }
}

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct SleepScore {
    pub total: u8,
    pub duration: u8,
    pub efficiency: u8,
    pub deep: u8,
    pub rem: u8,
    pub continuity: u8,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Night {
    pub stages: Vec<SleepBand>,
    pub asleep_from_ms: Option<i64>,
    pub asleep_to_ms: Option<i64>,
    pub score: Option<SleepScore>,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct SyncProgress {
    pub history_fraction: Option<f64>,
    pub transfer_received: Option<u32>,
    pub transfer_total: Option<u32>,
    pub records_stored: u64,
    pub streams_done: u32,
    pub streams_total: u32,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq, Eq)]
pub struct DeviceIdentity {
    pub name: String,
    pub firmware: u32,
    pub bootloader: u32,
    pub hardware: Option<u32>,
    pub rescue: Option<u32>,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq, Eq)]
pub struct UserProfile {
    pub birth_secs: i64,
    pub weight_grams: u32,
    pub height_cm: u32,
    pub first_name: String,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Snapshot {
    pub progress: Progress,
    pub device: Option<DeviceIdentity>,
    pub user: Option<UserProfile>,
    pub battery: Option<Battery>,
    pub latest_hr: Option<HrPoint>,
    pub latest_temperature: Option<Temperature>,
    pub steps: Option<Steps>,
    pub active_workout: Option<WorkoutSummary>,
    pub pending_deletes: u32,
    pub sync: SyncProgress,
    pub measuring: bool,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct EcgLead {
    pub name: String,
    pub millivolts: Vec<f64>,
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum EcgRhythm {
    NoAfib,
    Afib,
    Inconclusive,
    PoorRecording,
    RateOutOfRange,
    NoResult,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct EcgSummary {
    pub id: i64,
    pub measured_at_ms: i64,
    pub seconds: f64,
    pub leads: u32,
    pub heart_rate: Option<u32>,
    pub rhythm: Option<EcgRhythm>,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct EcgRecording {
    pub id: i64,
    pub measured_at_ms: i64,
    pub sampling_hz: u32,
    pub leads: Vec<EcgLead>,
    pub heart_rate: Option<u32>,
    pub rhythm: Option<EcgRhythm>,
}

#[uniffi::export(callback_interface)]
pub trait Transport: Send + Sync {
    fn write(&self, frames: Vec<Vec<u8>>);
    fn changed(&self);
    fn reconnect(&self);
}

#[uniffi::export(callback_interface)]
pub trait AncsLink: Send + Sync {
    fn announce(&self, bytes: Vec<u8>);
    fn attributes(&self, bytes: Vec<u8>);
}

#[uniffi::export(callback_interface)]
pub trait Rasterizer: Send + Sync {
    fn glyph(&self, codepoint: u32, width: u8, height: u8) -> Bitmap;
    fn icon(&self, app_id: String, width: u8, height: u8) -> Bitmap;
    fn activity_glyph(&self, activity: u32, width: u8, height: u8) -> Bitmap;
}

#[derive(uniffi::Record, Debug, Clone, PartialEq, Eq)]
pub struct Bitmap {
    pub width: u8,
    pub height: u8,
    pub pixels: Vec<u32>,
}

impl Bitmap {
    fn pack(self) -> Mono {
        if self.pixels.len() != self.width as usize * self.height as usize {
            return Mono::empty();
        }
        Mono::pack(&self.pixels, self.width, self.height)
    }
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotificationCategory {
    Other,
    IncomingCall,
    MissedCall,
    VoiceMail,
    Social,
    Schedule,
    Email,
    News,
    HealthAndFitness,
    BusinessAndFinance,
    Location,
    Entertainment,
}

impl From<NotificationCategory> for ancs::Category {
    fn from(category: NotificationCategory) -> ancs::Category {
        match category {
            NotificationCategory::Other => ancs::Category::Other,
            NotificationCategory::IncomingCall => ancs::Category::IncomingCall,
            NotificationCategory::MissedCall => ancs::Category::MissedCall,
            NotificationCategory::VoiceMail => ancs::Category::VoiceMail,
            NotificationCategory::Social => ancs::Category::Social,
            NotificationCategory::Schedule => ancs::Category::Schedule,
            NotificationCategory::Email => ancs::Category::Email,
            NotificationCategory::News => ancs::Category::News,
            NotificationCategory::HealthAndFitness => ancs::Category::HealthAndFitness,
            NotificationCategory::BusinessAndFinance => ancs::Category::BusinessAndFinance,
            NotificationCategory::Location => ancs::Category::Location,
            NotificationCategory::Entertainment => ancs::Category::Entertainment,
        }
    }
}

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq, Eq)]
pub struct NotificationConfig {
    pub accepted: bool,
    pub displayed: bool,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq, Eq)]
pub struct AncsUuids {
    pub service: String,
    pub notification_source: String,
    pub control_point: String,
    pub data_source: String,
}

#[uniffi::export]
pub fn ancs_uuids() -> AncsUuids {
    AncsUuids {
        service: ancs::SERVICE_UUID.into(),
        notification_source: ancs::NOTIFICATION_SOURCE_UUID.into(),
        control_point: ancs::CONTROL_POINT_UUID.into(),
        data_source: ancs::DATA_SOURCE_UUID.into(),
    }
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
    ancs: Box<dyn AncsLink>,
    rasterizer: Box<dyn Rasterizer>,
    device_id: i64,
    features: Mutex<Vec<(u16, u32, u32)>>,
    notifications: Mutex<NotificationCenter>,
}

#[uniffi::export]
impl WatchService {
    #[uniffi::constructor]
    pub fn new(
        db_path: String,
        mac: String,
        secret: String,
        transport: Box<dyn Transport>,
        ancs: Box<dyn AncsLink>,
        rasterizer: Box<dyn Rasterizer>,
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
            ancs,
            rasterizer,
            device_id,
            features: Mutex::new(DEFAULT_FEATURES.iter().map(|id| (*id, 0, 0)).collect()),
            notifications: Mutex::new(NotificationCenter::new()),
        })
    }

    pub fn on_connected(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.reassembler.reset();
            inner.client.handle(Event::Connected)
        };
        self.dispatch(actions)
    }

    pub fn poll_battery(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.client.poll_battery()
        };
        self.dispatch(actions)
    }

    pub fn tick(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.client.handle(Event::Tick {
                now: wpp::units::UnixMillis(now_ms()),
            })
        };
        self.dispatch(actions)
    }

    pub fn unhandled_objects(&self) -> Vec<String> {
        self.inner
            .lock()
            .unwrap()
            .client
            .take_unhandled()
            .into_iter()
            .map(|(command, type_id, name)| {
                let command_name = wpp::commands::Command(command)
                    .name()
                    .unwrap_or("unknown command");
                format!("{command_name} ({command}) carried {name} ({type_id}), unread")
            })
            .collect()
    }

    pub fn on_bytes(&self, bytes: Vec<u8>, received_at_ms: i64) -> Result<(), WatchError> {
        let mut pending = Vec::new();
        let mut frames = Vec::new();
        let mut undecoded = Vec::new();
        {
            let mut inner = self.inner.lock().unwrap();
            let items = inner.reassembler.push(&bytes);
            for item in items {
                match item {
                    StreamItem::Frame { frame, .. } => {
                        pending.extend(inner.client.handle(Event::Frame {
                            frame: frame.clone(),
                            received_at: UnixMillis(received_at_ms),
                        }));
                        frames.push(frame);
                    }
                    StreamItem::Desync { bytes, .. } => undecoded.push(bytes),
                }
            }
        }
        {
            let store = self.store.lock().unwrap();
            for bytes in &undecoded {
                let command = Frame::declared_command(bytes).unwrap_or(0);
                store.store_undecoded(
                    self.device_id,
                    received_at_ms,
                    command as i64,
                    bytes,
                    Frame::splice_offset(bytes).map(|at| at as i64),
                )?;
            }
        }
        for frame in &frames {
            self.draw(frame);
        }
        self.dispatch(pending)
    }

    pub fn on_disconnected(&self) {
        {
            let mut inner = self.inner.lock().unwrap();
            inner.reassembler.reset();
            inner.client.handle(Event::Disconnected);
        }
        self.transport.changed();
    }

    pub fn sync_now(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.sync_now();
        self.dispatch(actions)
    }

    pub fn factory_reset(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.factory_reset();
        self.dispatch(actions)
    }

    pub fn request_refresh(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            let mut actions = inner.client.force_refresh();
            actions.extend(inner.client.walk_now());
            actions
        };
        self.dispatch(actions)
    }

    pub fn progress(&self) -> Progress {
        let inner = self.inner.lock().unwrap();
        Progress::of(inner.client.phase())
    }

    pub fn snapshot(&self) -> Result<Snapshot, WatchError> {
        let store = self.store.lock().unwrap();
        let (phase, pending, progress, measuring) = {
            let inner = self.inner.lock().unwrap();
            let now = now_ms();
            let transfer = inner.client.transfer_progress();
            (
                Progress::of(inner.client.phase()),
                inner.client.pending_deletes() as u32,
                SyncProgress {
                    history_fraction: inner.client.walk_span().and_then(|(from, at)| {
                        let span = now / 1000 - from.0;
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
                inner.client.measuring().is_some(),
            )
        };
        let progress_phase = phase;
        Ok(Snapshot {
            progress: progress_phase,
            user: store.watch_user(self.device_id)?.map(|user| UserProfile {
                birth_secs: user.birth as i64,
                weight_grams: user.weight,
                height_cm: user.height,
                first_name: user.first_name,
            }),
            device: store
                .identity(self.device_id)?
                .map(|identity| DeviceIdentity {
                    name: identity.name,
                    firmware: identity.firmware,
                    bootloader: identity.bootloader,
                    hardware: identity.hardware,
                    rescue: identity.rescue,
                }),
            battery: store.latest(self.device_id, 6)?.map(|(at, value)| Battery {
                percent: value as u32,
                at_ms: at,
                charging: store
                    .latest(self.device_id, 8)
                    .ok()
                    .flatten()
                    .filter(|(state_at, _)| now_ms() - state_at < CHARGE_STATE_FRESH_MS)
                    .map(|(_, state)| state == CHARGING_STATE),
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
                .map(|active| WorkoutSummary {
                    id: active.id,
                    started_at_ms: active.started_at.0 * 1000,
                    ended_at_ms: None,
                    subcategory: active.subcategory as i32,
                    activity: activity_name(active.subcategory as u32),
                }),
            pending_deletes: pending,
            sync: progress,
            measuring,
        })
    }

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
                origin: origin_of(source),
            })
            .collect())
    }

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
                origin: origin_of(source),
            })
            .collect())
    }

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

    pub fn delete_workout(&self, id: i64) -> Result<(), WatchError> {
        let store = self.store.lock().unwrap();
        store.delete_workout(self.device_id, id)?;
        drop(store);
        self.transport.changed();
        Ok(())
    }

    pub fn activity_totals(
        &self,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<ActivityTotals, WatchError> {
        let store = self.store.lock().unwrap();
        let minutes = store.activity_minutes(
            self.device_id,
            from_ms.div_euclid(1000),
            to_ms.div_euclid(1000),
        )?;
        let summed = activity::totals(&minutes);
        Ok(ActivityTotals {
            steps: summed.steps,
            distance_metres: summed.distance.0,
            ascent_metres: summed.ascent.0,
            calories: summed.calories.0,
        })
    }

    pub fn detected_activities(
        &self,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<Vec<DetectedActivity>, WatchError> {
        let store = self.store.lock().unwrap();
        let minutes = store.activity_minutes(
            self.device_id,
            from_ms.div_euclid(1000),
            to_ms.div_euclid(1000),
        )?;
        Ok(activity::detect(&minutes)
            .into_iter()
            .map(|session| DetectedActivity {
                started_at_ms: session.started_at.to_millis().0,
                ended_at_ms: session.ended_at.to_millis().0,
                subcategory: session.subcategory as i32,
                activity: activity_name(session.subcategory as u32),
                steps: session.steps,
                distance_metres: session.distance.0,
                calories: session.calories.0,
            })
            .collect())
    }

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

    pub fn set_screens(&self, ids: Vec<u8>) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.set_screens(&ids);
        self.dispatch(actions)
    }

    pub fn set_user(
        &self,
        birth_secs: i64,
        weight_grams: u32,
        height_cm: u32,
    ) -> Result<(), WatchError> {
        let held = self
            .store
            .lock()
            .unwrap()
            .watch_user(self.device_id)?
            .ok_or_else(|| WatchError::Protocol {
                reason: "the watch has not reported a profile to edit yet".to_string(),
            })?;
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .set_user(&wpp::client::UserProfile {
                birth: birth_secs as i32,
                weight: weight_grams,
                height: height_cm,
                ..held
            });
        self.dispatch(actions)
    }

    pub fn start_workout(&self, activity: u32) -> Result<(), WatchError> {
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .start_workout(activity as i16, UnixTime(now_ms() / 1000));
        self.dispatch(actions)
    }

    pub fn stop_workout(&self) -> Result<(), WatchError> {
        let Some(active) = self.store.lock().unwrap().active_workout(self.device_id)? else {
            return Ok(());
        };
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .stop_workout(active.started_at, UnixTime(now_ms() / 1000));
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

    pub fn set_zone(&self, gmt_offset: i32, next_change: Option<DstChange>) {
        self.inner.lock().unwrap().client.set_zone(
            gmt_offset,
            next_change.map(|c| wpp::client::DstChange {
                at: UnixMillis(c.at_ms).to_seconds(),
                gmt_offset: c.gmt_offset_seconds,
            }),
        );
    }


    pub fn request_device_config(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.request_device_config();
        self.dispatch(actions)
    }

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
        let formats = self.inner.lock().unwrap().client.image_formats().to_vec();
        if formats.is_empty() {
            return Err(WatchError::Protocol {
                reason: "the watch has not said what size glyphs it wants yet".to_string(),
            });
        }
        let screens: Vec<wpp::client::WorkoutScreen> = ids
            .iter()
            .map(|id| {
                let (face_mode, flag) = ACTIVITY_FACES
                    .iter()
                    .find(|(known, _, _)| known == id)
                    .map(|(_, face, flag)| (*face, *flag))
                    .unwrap_or((1, 1));
                let glyphs = formats
                    .iter()
                    .map(|format| {
                        let drawn =
                            self.rasterizer
                                .activity_glyph(*id, format.width, format.height);
                        (
                            format.kind,
                            wpp::image::Mono::pack(&drawn.pixels, drawn.width, drawn.height),
                        )
                    })
                    .collect();
                wpp::client::WorkoutScreen {
                    id: *id,
                    name: activity_name(*id),
                    face_mode,
                    flag,
                    glyphs,
                }
            })
            .collect();
        let actions = self.inner.lock().unwrap().client.set_activities(&screens);
        self.dispatch(actions)
    }

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

    pub fn notification_config(&self) -> Option<NotificationConfig> {
        self.inner
            .lock()
            .unwrap()
            .client
            .notifications()
            .map(|c| NotificationConfig {
                accepted: c.accepted,
                displayed: c.displayed,
            })
    }

    pub fn set_notifications(&self, enabled: bool) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.set_notifications(enabled);
        self.dispatch(actions)
    }

    pub fn prefer_notifications(&self, enabled: bool) {
        self.inner
            .lock()
            .unwrap()
            .client
            .prefer_notifications(enabled);
    }

    pub fn post_notification(
        &self,
        app_id: String,
        title: String,
        subtitle: String,
        message: String,
        category: NotificationCategory,
    ) -> u32 {
        let (id, announcement) = self.notifications.lock().unwrap().post(
            app_id,
            title,
            subtitle,
            message,
            category.into(),
        );
        self.ancs.announce(announcement.to_vec());
        id.0
    }

    pub fn dismiss_notification(&self, id: u32) {
        let announcement = self
            .notifications
            .lock()
            .unwrap()
            .dismiss(NotificationId(id));
        if let Some(announcement) = announcement {
            self.ancs.announce(announcement.to_vec());
        }
    }

    pub fn on_ancs_write(&self, bytes: Vec<u8>, max_payload: u32) -> Result<(), WatchError> {
        if max_payload == 0 {
            return Err(WatchError::Protocol {
                reason: "a data source fragment has to carry something".into(),
            });
        }
        let request =
            wpp::ancs::ControlPoint::parse(&bytes).map_err(|err| WatchError::Protocol {
                reason: err.to_string(),
            })?;
        let response = {
            let center = self.notifications.lock().unwrap();
            let Some(notification) = center.get(request.id) else {
                return Ok(());
            };
            request.response(notification)
        };
        for fragment in wpp::ancs::fragments(&response, max_payload as usize) {
            self.ancs.attributes(fragment);
        }
        Ok(())
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

    pub fn night(&self, from_ms: i64, to_ms: i64) -> Result<Night, WatchError> {
        let store = self.store.lock().unwrap();
        let minutes = store.activity_minutes(self.device_id, from_ms / 1000, to_ms / 1000)?;

        let levels: Vec<(i64, i64, SleepStage)> = minutes
            .iter()
            .filter_map(|minute| {
                let level = activity::SleepLevel::from_wire(minute.sleep_level?)?;
                Some((
                    minute.at.0 * 1000,
                    minute.ended_at().0 * 1000,
                    SleepStage::of(level),
                ))
            })
            .collect();

        let asleep = levels
            .iter()
            .filter(|(_, _, stage)| *stage != SleepStage::Awake)
            .fold(None, |span: Option<(i64, i64)>, (from, to, _)| {
                Some(match span {
                    None => (*from, *to),
                    Some((lo, hi)) => (lo.min(*from), hi.max(*to)),
                })
            });

        let stages = merge_adjacent(minutes.iter().filter_map(|minute| {
            let (from_ms, to_ms) = (minute.at.0 * 1000, minute.ended_at().0 * 1000);
            let stage = match minute.sleep_level {
                Some(level) => SleepStage::of(activity::SleepLevel::from_wire(level)?),
                None => {
                    let (night_from, night_to) = asleep?;
                    if from_ms < night_from || to_ms > night_to {
                        return None;
                    }
                    SleepStage::Awake
                }
            };
            Some(SleepBand {
                from_ms,
                to_ms,
                stage,
            })
        }));

        let score = wpp::sleep::score(
            &stages
                .iter()
                .map(|band| wpp::sleep::Band {
                    from_ms: band.from_ms,
                    to_ms: band.to_ms,
                    level: match band.stage {
                        SleepStage::Awake => activity::SleepLevel::Awake,
                        SleepStage::Light => activity::SleepLevel::Light,
                        SleepStage::Deep => activity::SleepLevel::Deep,
                        SleepStage::Rem => activity::SleepLevel::Rem,
                    },
                })
                .collect::<Vec<_>>(),
        )
        .map(|s| SleepScore {
            total: s.total,
            duration: s.duration,
            efficiency: s.efficiency,
            deep: s.deep,
            rem: s.rem,
            continuity: s.continuity,
        });

        Ok(Night {
            asleep_from_ms: asleep.map(|(from, _)| from),
            asleep_to_ms: asleep.map(|(_, to)| to),
            score,
            stages,
        })
    }

    pub fn has_staging(&self, from_ms: i64, to_ms: i64) -> Result<bool, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store.has_staging(self.device_id, from_ms / 1000, to_ms / 1000)?)
    }

    pub fn charging(&self, from_ms: i64, to_ms: i64) -> Result<Vec<Marker>, WatchError> {
        let store = self.store.lock().unwrap();
        let mut out = Vec::new();
        for (start, end) in store.charge_periods(self.device_id, from_ms, to_ms)? {
            out.push(Marker {
                at_ms: start,
                edge: SetEdge::Start,
            });
            if let Some(end) = end {
                out.push(Marker {
                    at_ms: end,
                    edge: SetEdge::End,
                });
            }
        }
        Ok(out)
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

    pub fn live_ecg(&self) -> Vec<f64> {
        let inner = self.inner.lock().unwrap();
        inner
            .client
            .live_samples()
            .iter()
            .map(|c| Millivolts::from_counts(*c).0)
            .collect()
    }

    pub fn ecgs(&self) -> Result<Vec<EcgSummary>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .ecgs(self.device_id)?
            .into_iter()
            .map(|(id, measured_at, signal_type, hz, bytes)| {
                let leads = wpp::signal::SignalKind::from_type_id(signal_type as u16)
                    .map(|k| k.leads().len())
                    .unwrap_or(1);
                let per_lead = bytes / 2 / leads.max(1) as i64;
                let verdict = verdict_of(&store, id);
                EcgSummary {
                    id,
                    measured_at_ms: measured_at,
                    seconds: per_lead as f64 / (hz.max(1) as f64),
                    leads: leads as u32,
                    heart_rate: verdict.0,
                    rhythm: verdict.1,
                }
            })
            .collect())
    }

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
        let verdict = verdict_of(&store, id);
        Ok(Some(EcgRecording {
            id,
            measured_at_ms: measured_at,
            sampling_hz: hz as u32,
            leads,
            heart_rate: verdict.0,
            rhythm: verdict.1,
        }))
    }
}

fn verdict_of(store: &wpp_store::Store, id: i64) -> (Option<u32>, Option<EcgRhythm>) {
    let Ok(measures) = store.ecg_measures(id) else {
        return (None, None);
    };
    let reading = |kind: wpp::signal::MeasureType| {
        measures
            .iter()
            .find(|(t, _, _)| *t == kind.0 as i64)
            .map(|(_, value, exponent)| *value as f64 * 10f64.powi(*exponent as i32))
    };
    let rhythm = reading(wpp::signal::MeasureType::AFIB_RESULT).map(|code| {
        match wpp::signal::Rhythm::of(code as i32) {
            wpp::signal::Rhythm::NoAfib => EcgRhythm::NoAfib,
            wpp::signal::Rhythm::Afib => EcgRhythm::Afib,
            wpp::signal::Rhythm::Inconclusive => EcgRhythm::Inconclusive,
            wpp::signal::Rhythm::PoorRecording => EcgRhythm::PoorRecording,
            wpp::signal::Rhythm::RateOutOfRange => EcgRhythm::RateOutOfRange,
            wpp::signal::Rhythm::NoResult => EcgRhythm::NoResult,
        }
    });
    let rate = reading(wpp::signal::MeasureType::HEART_RATE)
        .filter(|bpm| *bpm > 0.0)
        .map(|bpm| bpm.round() as u32);
    (rate, rhythm)
}

const CHARGING_STATE: i64 = 0;

const CHARGE_STATE_FRESH_MS: i64 = 180_000;

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

/// The message carries the whole enabled set, so an id left out is switched
/// off. Omitting 100 and 105 coincided with the activity stream going
/// silent, so they are carried though nothing names them.
const DEFAULT_FEATURES: &[u16] = &[
    3, 5, 9, 10, 11, 14, 17, 19, 20, 27, 53, 71, 88, 100, 105, 113,
];

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

const ACTIVITY_FACES: &[(u32, u8, u16)] = &[
    (1, 2, 0),
    (2, 2, 0),
    (3, 2, 0),
    (4, 1, 1),
    (5, 1, 1),
    (6, 3, 0),
    (7, 1, 1),
    (8, 1, 1),
    (9, 1, 1),
    (10, 3, 1),
    (11, 1, 1),
    (12, 1, 1),
    (13, 1, 1),
    (14, 1, 1),
    (15, 1, 1),
    (16, 1, 1),
    (17, 1, 1),
    (18, 1, 0),
    (19, 1, 1),
    (20, 1, 1),
    (21, 1, 1),
    (22, 1, 1),
    (23, 1, 1),
    (24, 1, 1),
    (25, 1, 1),
    (26, 3, 0),
    (27, 1, 1),
    (28, 1, 1),
    (29, 1, 1),
    (30, 1, 1),
    (31, 1, 1),
    (32, 1, 1),
    (33, 1, 1),
    (34, 3, 0),
    (35, 3, 0),
    (36, 3, 0),
    (187, 3, 0),
    (188, 1, 1),
    (191, 1, 1),
    (192, 1, 1),
    (193, 1, 1),
    (194, 1, 1),
    (195, 1, 1),
    (196, 1, 1),
    (306, 2, 1),
    (307, 1, 0),
];

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
    fn dispatch(&self, actions: Vec<Action>) -> Result<(), WatchError> {
        let mut actions: std::collections::VecDeque<Action> = actions.into();
        let mut changed = false;
        while let Some(action) = actions.pop_front() {
            match action {
                Action::Send(frame) | Action::Delete(frame) => self.write(&frame),
                Action::Finished => changed = true,
                Action::Reconnect => self.transport.reconnect(),
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

    fn write(&self, message: &Frame) {
        self.transport
            .write(message.to_wire().iter().map(Frame::to_bytes).collect());
    }

    fn draw(&self, frame: &Frame) {
        let glyph = GlyphRequest::parse(frame);
        let icon = IconRequest::parse(frame);
        if glyph.is_none() && icon.is_none() {
            return;
        }
        let formats = self.inner.lock().unwrap().client.image_formats().to_vec();

        if let Some(request) = glyph {
            let (width, height) = request.size(&formats);
            let bitmaps: Vec<Mono> = request
                .glyphs
                .iter()
                .map(|g| self.rasterizer.glyph(g.codepoint, width, height).pack())
                .collect();
            self.write(&request.reply(&bitmaps));
            return;
        }
        if let Some(request) = icon {
            let (width, height) = request.size(&formats);
            let drawn = self
                .rasterizer
                .icon(request.app_id.clone(), width, height)
                .pack();
            self.write(&request.reply(&drawn));
        }
    }
}

#[derive(uniffi::Record, Debug, Clone, PartialEq, Eq)]
pub struct KnownWatch {
    pub mac: String,
    pub secret: String,
}

#[derive(uniffi::Enum, Debug, Clone, PartialEq, Eq)]
pub enum PairingProgress {
    Idle,
    Probing,
    Associating,
    Readopting,
    Paired { mac: String, secret: String },
    AlreadyAssociated,
}

impl PairingProgress {
    fn of(state: &PairingState) -> PairingProgress {
        match state {
            PairingState::Idle => PairingProgress::Idle,
            PairingState::Probing => PairingProgress::Probing,
            PairingState::Associating { .. } | PairingState::FinishingSetup { .. } => {
                PairingProgress::Associating
            }
            PairingState::Readopting(_) => PairingProgress::Readopting,
            PairingState::Paired(credentials) => PairingProgress::Paired {
                mac: credentials.mac.clone(),
                secret: credentials.secret.clone(),
            },
            PairingState::AlreadyAssociated => PairingProgress::AlreadyAssociated,
        }
    }
}

#[derive(uniffi::Object)]
pub struct PairingService {
    inner: Mutex<PairingInner>,
    transport: Box<dyn Transport>,
}

struct PairingInner {
    pairing: Pairing,
    reassembler: FrameReassembler,
}

#[uniffi::export]
impl PairingService {
    #[uniffi::constructor]
    pub fn new(
        secret: String,
        account_id: u32,
        known: Vec<KnownWatch>,
        transport: Box<dyn Transport>,
    ) -> Result<Self, WatchError> {
        let known = known
            .into_iter()
            .map(|w| Credentials {
                mac: w.mac,
                secret: w.secret,
            })
            .collect();
        let pairing =
            Pairing::new(secret, account_id, known).map_err(|err| WatchError::Protocol {
                reason: format!("{err:?}"),
            })?;
        Ok(PairingService {
            inner: Mutex::new(PairingInner {
                pairing,
                reassembler: FrameReassembler::new(),
            }),
            transport,
        })
    }

    pub fn on_connected(&self) {
        self.step(|inner| {
            inner.reassembler.reset();
            inner.pairing.on_connected()
        });
    }

    pub fn on_bytes(&self, bytes: Vec<u8>) {
        self.step(|inner| {
            let items = inner.reassembler.push(&bytes);
            items
                .into_iter()
                .filter_map(|item| match item {
                    StreamItem::Frame { frame, .. } => Some(frame),
                    StreamItem::Desync { .. } => None,
                })
                .flat_map(|frame| inner.pairing.on_frame(&frame))
                .collect()
        });
    }

    pub fn on_disconnected(&self) {
        self.step(|inner| {
            inner.reassembler.reset();
            inner.pairing.on_disconnected();
            Vec::new()
        });
    }

    pub fn progress(&self) -> PairingProgress {
        PairingProgress::of(self.inner.lock().unwrap().pairing.state())
    }
}

impl PairingService {
    fn step(&self, act: impl FnOnce(&mut PairingInner) -> Vec<Frame>) {
        let (frames, moved) = {
            let mut inner = self.inner.lock().unwrap();
            let before = inner.pairing.state().clone();
            let frames = act(&mut inner);
            let moved = *inner.pairing.state() != before;
            (frames, moved)
        };
        for message in frames {
            self.transport
                .write(message.to_wire().iter().map(Frame::to_bytes).collect());
        }
        if moved {
            self.transport.changed();
        }
    }
}
