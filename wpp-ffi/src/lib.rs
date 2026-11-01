use std::collections::HashSet;
use std::sync::{Arc, Mutex};

use wpp::activity;
use wpp::ancs::{self, NotificationCenter, NotificationId};
use wpp::capture::{FrameReassembler, StreamItem};
use wpp::client::{
    Action, Client, Credentials, Event, Feature, FeatureId, FeatureSchedule, Phase, SampleKind,
    Source,
};
use wpp::image::{GlyphRequest, IconRequest, Mono};
use wpp::pairing::{Pairing, PairingState};
use wpp::track::{self, Track};
use wpp::signal::STORED_MEASURE_ATTRIB_CONFIRMED;
use wpp::sport::{self, SportId};
use wpp::units::{Bpm, Millivolts, UnixMillis, UnixTime};
use wpp::watch_config::{
    default_features, screen_name, CATEGORIES, HEALTH_FEATURES, KNOWN_SCREENS,
    RESPIRATORY_SCAN_MONITORING, SCAN_WINDOW,
};
use wpp::Frame;
use wpp_store::{report, DeviceId, EcgId, SetBoundary, Store, Trim, WorkoutId};

uniffi::setup_scaffolding!();

fn origin_of(source: Source) -> Origin {
    match source {
        Source::Live => Origin::Live,
        Source::Stored => Origin::Stored,
    }
}

#[derive(Debug, thiserror::Error, uniffi::Error)]
pub enum WatchError {
    #[error("storage: {reason}")]
    Storage { reason: String },
    #[error("protocol: {reason}")]
    Protocol { reason: String },
    #[error("refused: {reason}")]
    Refused { reason: String },
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
    fn kind(self) -> SampleKind {
        match self {
            Metric::HeartRate => SampleKind::HeartRate,
            Metric::Temperature => SampleKind::CoreTemperature,
            Metric::HrvSdnn => SampleKind::HrvSdnn,
            Metric::HrvRmssd => SampleKind::HrvRmssd,
            Metric::RespiratoryRate => SampleKind::RespiratoryRate,
            Metric::Battery => SampleKind::BatteryPercent,
            Metric::Steps => SampleKind::Steps,
            Metric::Spo2 => SampleKind::Spo2,
            Metric::Ascent => SampleKind::Ascent,
            Metric::Calories => SampleKind::Calories,
            Metric::Distance => SampleKind::Distance,
            Metric::TrackedDuration => SampleKind::TrackedDuration,
        }
    }

    fn scale(self) -> f64 {
        match self {
            Metric::Temperature => 1000.0,
            Metric::Ascent | Metric::Distance | Metric::Calories => 100.0,
            Metric::TrackedDuration => 3600.0,
            Metric::Spo2 => 10.0,
            _ => 1.0,
        }
    }

    fn reading(self, stored: i64) -> f64 {
        stored as f64 / self.scale()
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

/// Whether an activity is worth a receiver at all. Nothing is recorded for one
/// that stays put, so an indoor session leaves no route to explain.
#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum Travel {
    Ground,
    InPlace,
}

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq, Eq)]
pub struct LocationFix {
    pub at_ms: i64,
    pub lat_e7: i32,
    pub lon_e7: i32,
    pub altitude_cm: Option<i32>,
    pub accuracy_cm: Option<i32>,
    pub speed_mm_s: Option<i32>,
    pub bearing_cdeg: Option<i32>,
}

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct TrackSummary {
    pub distance_metres: f64,
    /// Seconds under way, which is what an average speed means anything over.
    pub moving_secs: i64,
    pub average_speed_m_s: Option<f64>,
    /// The middle of the spread of speeds, for colouring the line by it.
    pub slow_m_s: Option<f64>,
    pub fast_m_s: Option<f64>,
    pub fixes: u32,
}

/// Where the map is looking, carried by the caller between gestures so the
/// projection stays on this side of the boundary.
#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct MapView {
    pub centre_lat: f64,
    pub centre_lon: f64,
    pub zoom: f64,
    pub width_px: f64,
    pub height_px: f64,
}

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct MapTile {
    pub z: u32,
    pub x: u32,
    pub y: u32,
    pub left_px: f64,
    pub top_px: f64,
    pub size_px: f64,
}

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct MapPoint {
    pub at_ms: i64,
    pub x_px: f64,
    pub y_px: f64,
    pub speed_m_s: Option<f64>,
}

/// One unbroken run of the route. A stretch with no fixes is a break in the
/// line rather than a straight leg across whatever was not recorded.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct MapPath {
    pub points: Vec<MapPoint>,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct MapFrame {
    pub tiles: Vec<MapTile>,
    pub path: Vec<MapPath>,
    pub metres_per_pixel: f64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct WorkoutSummary {
    pub id: i64,
    pub started_at_ms: i64,
    pub ended_at_ms: Option<i64>,
    pub subcategory: i32,
    pub activity: String,
    pub on_foot: bool,
    pub travel: Travel,
    /// Estimated from heart rate: the watch reports calories only for the
    /// motion it counts as steps, which a workout on the spot never is.
    pub calories: Option<f64>,
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
    pub on_foot: bool,
    pub travel: Travel,
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

/// A scan the watch runs for one night and then stops, reported as the unix
/// second its window ends.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct ArmedScan {
    pub ends_at: i64,
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

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct SleepSpan {
    pub from_ms: i64,
    pub to_ms: i64,
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum SleepStage {
    Awake,
    Light,
    Deep,
    Rem,
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

/// Heart rate over a session with the stretches the sensor lost the wrist
/// left out, so a figure never stands on readings the wearer's heart did not
/// make.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct SessionHeart {
    pub median_bpm: u16,
    pub p95_bpm: u16,
    pub measured_ms: i64,
    pub dropout_ms: i64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct WorkoutTrend {
    pub workout: WorkoutSummary,
    pub heart: Option<SessionHeart>,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct AwakeHeart {
    pub median_bpm: u16,
    pub readings: u32,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Note {
    pub id: i64,
    pub at_ms: i64,
    pub text: String,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct NightHeart {
    pub median_bpm: u16,
    pub resting_bpm: u16,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct NightTrend {
    pub asleep_from_ms: i64,
    pub asleep_to_ms: i64,
    pub asleep_ms: i64,
    pub score: Option<SleepScore>,
    pub heart: Option<NightHeart>,
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
pub struct SpiFlashProgress {
    pub received: u32,
    pub total: u32,
    pub done: bool,
    pub error: Option<i32>,
}

impl From<wpp::spiflash::SpiFlashProgress> for SpiFlashProgress {
    fn from(progress: wpp::spiflash::SpiFlashProgress) -> Self {
        SpiFlashProgress {
            received: progress.received as u32,
            total: progress.total as u32,
            done: progress.done,
            error: progress.error,
        }
    }
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

/// What the watch's PPG classifier concluded about an episode it kept.
#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum PpgRhythm {
    Undefined,
    SinusRhythm,
    Afib,
    Other,
    Noise,
}

/// A background reading the watch's PPG classifier called AFib. The watch
/// alerts on a confirmed run of them, not on each.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct AfibEpisode {
    pub id: i64,
    pub measured_at_ms: i64,
    pub seconds: u32,
    pub rhythm: Option<PpgRhythm>,
    /// Whether this is the reading that raised the alert; absent on readings
    /// kept before the watch's flag was.
    pub alerted: Option<bool>,
}

/// One background reading, with the intervals between the beats it found
/// once split beats are taken back together.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct AfibReading {
    pub id: i64,
    pub measured_at_ms: i64,
    pub heart_rate: Option<u32>,
    pub intervals_ms: Vec<u32>,
    pub analysis: PulseAnalysis,
    pub asleep: bool,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct WavePoint {
    pub at_ms: u32,
    pub ms: f64,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct BreathingWave {
    pub curve: Vec<WavePoint>,
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum RhythmPattern {
    Steady,
    Breathing,
    Variable,
    Irregular,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct RhythmAnalysis {
    pub pattern: RhythmPattern,
    pub wave: Option<BreathingWave>,
}

#[derive(uniffi::Enum, Debug, Clone, PartialEq)]
pub enum PulseAnalysis {
    NoWatchRate,
    TooFew,
    Disagrees,
    Shifted,
    Assessed { rhythm: RhythmAnalysis },
}

#[derive(uniffi::Enum, Debug, Clone, PartialEq)]
pub enum EcgAnalysis {
    TooFew,
    Noisy,
    Shifted,
    Assessed {
        rhythm: RhythmAnalysis,
        p_waves: u32,
        early_beats: u32,
    },
}

/// A confirmed reading and the readings the watch counted towards it, oldest
/// first and ending with the confirmed one. Empty recordings (0 s, no beats)
/// count towards the run like any other stored reading, but are left out of
/// `readings` and only counted in `empty`.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct AfibAlert {
    pub id: i64,
    pub measured_at_ms: i64,
    pub run: u32,
    pub window_hours: u32,
    pub counted: u32,
    pub empty: u32,
    pub readings: Vec<AfibReading>,
    pub ecg_after: Option<EcgSummary>,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct EcgRecording {
    pub id: i64,
    pub measured_at_ms: i64,
    pub sampling_hz: u32,
    pub leads: Vec<EcgLead>,
    pub heart_rate: Option<u32>,
    pub rhythm: Option<EcgRhythm>,
    pub analysis: EcgAnalysis,
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

/// How far back a live distance is held still. Long enough that the smoother
/// has stopped moving those fixes, short enough that a fix costs a fixed
/// window rather than the session so far.
const SETTLE_MS: i64 = 60_000;

#[derive(uniffi::Object)]
pub struct WatchService {
    inner: Mutex<Inner>,
    store: Mutex<Store>,
    transport: Box<dyn Transport>,
    ancs: Box<dyn AncsLink>,
    rasterizer: Box<dyn Rasterizer>,
    device_id: DeviceId,
    notifications: Mutex<NotificationCenter>,
    live_distance: Mutex<Option<LiveDistance>>,
    unreadable_rows: Mutex<UnreadableRows>,
}

// The lists are re-read on every screen, so a bad row is told once rather than
// on every read of it.
#[derive(Default)]
struct UnreadableRows {
    seen: HashSet<String>,
    pending: Vec<String>,
}

impl UnreadableRows {
    fn note(&mut self, problem: &impl std::fmt::Display) {
        let line = problem.to_string();
        if self.seen.insert(line.clone()) {
            self.pending.push(line);
        }
    }
}

/// What the watch has already been told, so a fix costs a window rather than
/// the whole session.
struct LiveDistance {
    workout_started_at: UnixTime,
    settled_metres: f64,
    settled_through_ms: i64,
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
        let mut store = Store::open(&db_path)?;
        let device_id = store.device(&mac)?;
        let watermarks = store.watermarks(device_id, &CATEGORIES)?;
        let features = store.features_or_seed(device_id, &default_features())?;
        let client = Client::new(Credentials { mac, secret }, watermarks, features);
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
            notifications: Mutex::new(NotificationCenter::new()),
            live_distance: Mutex::new(None),
            unreadable_rows: Mutex::default(),
        })
    }

    pub fn on_connected(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.reassembler.reset();
            inner.client.handle(Event::Connected { now: UnixMillis(now_ms()) })
        };
        self.dispatch(actions)
    }

    pub fn poll_battery(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.client.poll_battery(UnixMillis(now_ms()))
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
        let unhandled = self.inner.lock().unwrap().client.take_unhandled();
        let unreadable = std::mem::take(&mut self.unreadable_rows.lock().unwrap().pending);
        unhandled
            .into_iter()
            .map(|(command, type_id, name)| {
                let command_name = wpp::commands::Command(command)
                    .name()
                    .unwrap_or("unknown command");
                format!("{command_name} ({command}) carried {name} ({type_id}), unread")
            })
            .chain(unreadable)
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
        // Outside any transaction, and the client has already consumed these
        // frames, so a failure here must not cost it the replies they earned.
        let kept = {
            let store = self.store.lock().unwrap();
            undecoded.iter().try_for_each(|bytes| {
                let command = Frame::declared_command(bytes).unwrap_or(0);
                store.store_undecoded(
                    self.device_id,
                    UnixMillis(received_at_ms),
                    command as i64,
                    bytes,
                    Frame::splice_offset(bytes).map(|at| at as i64),
                )
            })
        };
        let drawn = frames.iter().try_for_each(|frame| self.draw(frame));
        self.dispatch(pending)?;
        kept?;
        drawn
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
        let actions = self.inner.lock().unwrap().client.sync_now(UnixMillis(now_ms()));
        self.dispatch(actions)
    }

    pub fn factory_reset(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.factory_reset(UnixMillis(now_ms()));
        self.dispatch(actions)
    }

    pub fn request_refresh(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            let now = UnixMillis(now_ms());
            let mut actions = inner.client.force_refresh(now);
            actions.extend(inner.client.walk_now(now));
            actions
        };
        self.dispatch(actions)
    }

    pub fn progress(&self) -> Progress {
        let inner = self.inner.lock().unwrap();
        Progress::of(inner.client.phase())
    }

    pub fn snapshot(&self) -> Result<Snapshot, WatchError> {
        let now = UnixMillis(now_ms());
        let store = self.store.lock().unwrap();
        let (phase, pending, progress, measuring) = {
            let inner = self.inner.lock().unwrap();
            let transfer = inner.client.transfer_progress();
            (
                Progress::of(inner.client.phase()),
                inner.client.pending_deletes() as u32,
                SyncProgress {
                    history_fraction: inner.client.walk_span().and_then(|(from, at)| {
                        let span = now.to_seconds().0 - from.0;
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
        let wearer = store.watch_user(self.device_id)?;
        Ok(Snapshot {
            progress: progress_phase,
            user: wearer.as_ref().map(|user| UserProfile {
                birth_secs: user.birth as i64,
                weight_grams: user.weight,
                height_cm: user.height,
                first_name: user.first_name.clone(),
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
            battery: report::battery(&store, self.device_id, now)?.map(|level| Battery {
                percent: level.percent,
                at_ms: level.at.0,
                charging: level.charging,
            }),
            latest_hr: store
                .latest(self.device_id, SampleKind::HeartRate)?
                .map(|point| HrPoint {
                    at_ms: point.at.0,
                    bpm: point.value as u16,
                    origin: Origin::Stored,
                }),
            latest_temperature: store
                .latest(self.device_id, SampleKind::CoreTemperature)?
                .map(|point| Temperature {
                    celsius: Metric::Temperature.reading(point.value),
                    at_ms: point.at.0,
                }),
            steps: store
                .latest(self.device_id, SampleKind::Steps)?
                .map(|point| Steps {
                    count: point.value as u32,
                    day_start_ms: point.at.0,
                }),
            active_workout: report::active_workout(&store, self.device_id, now)?
                .map(workout_summary),
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
            .series(
                self.device_id,
                SampleKind::HeartRate,
                UnixMillis(from_ms),
                UnixMillis(to_ms),
                max_points,
            )?
            .into_iter()
            .map(|point| HrPoint {
                at_ms: point.at.0,
                bpm: point.value as u16,
                origin: origin_of(point.source),
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
        let found = store.series(
            self.device_id,
            metric.kind(),
            UnixMillis(from_ms),
            UnixMillis(to_ms),
            max_points,
        )?;
        if metric != Metric::Calories {
            return Ok(found
                .iter()
                .map(|point| Point {
                    at_ms: point.at.0,
                    value: metric.reading(point.value),
                    origin: origin_of(point.source),
                })
                .collect());
        }
        Ok(report::calorie_series(&store, self.device_id, &found, UnixMillis(now_ms()))?
            .into_iter()
            .map(|(reading, source)| Point {
                at_ms: reading.at.0,
                value: reading.total.0,
                origin: origin_of(source),
            })
            .collect())
    }

    pub fn windowed_max(
        &self,
        metric: Metric,
        edges_ms: Vec<i64>,
    ) -> Result<Vec<Option<f64>>, WatchError> {
        let edges = edges_of(edges_ms)?;
        let store = self.store.lock().unwrap();
        if metric != Metric::Calories {
            return Ok(store
                .windowed_max(self.device_id, metric.kind(), &edges)?
                .into_iter()
                .map(|found| found.map(|value| metric.reading(value)))
                .collect());
        }
        Ok(report::calories_windowed_max(&store, self.device_id, &edges, UnixMillis(now_ms()))?
            .into_iter()
            .map(|found| found.map(|total| total.0))
            .collect())
    }

    pub fn latest_value(&self, metric: Metric) -> Result<Option<Point>, WatchError> {
        let store = self.store.lock().unwrap();
        if metric == Metric::Calories {
            let latest = report::latest_calories(&store, self.device_id, UnixMillis(now_ms()))?;
            return Ok(latest.map(|reading| Point {
                at_ms: reading.at.0,
                value: reading.total.0,
                origin: Origin::Stored,
            }));
        }
        Ok(store.latest(self.device_id, metric.kind())?.map(|latest| Point {
            at_ms: latest.at.0,
            value: metric.reading(latest.value),
            origin: Origin::Stored,
        }))
    }

    pub fn extent(&self, metric: Metric) -> Result<Option<Extent>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .extent(self.device_id, metric.kind())?
            .map(|(from, to)| Extent {
                from_ms: from.0,
                to_ms: to.0,
            }))
    }

    pub fn workouts(&self, limit: u32) -> Result<Vec<WorkoutSummary>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(report::workouts(&store, self.device_id, limit, UnixMillis(now_ms()))?
            .into_iter()
            .map(workout_summary)
            .collect())
    }

    /// Every session the span touches, one still running included.
    pub fn workouts_between(
        &self,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<Vec<WorkoutSummary>, WatchError> {
        let store = self.store.lock().unwrap();
        let found = report::workouts_between(
            &store,
            self.device_id,
            UnixMillis(from_ms),
            UnixMillis(to_ms),
            UnixMillis(now_ms()),
        )?;
        Ok(found.into_iter().map(workout_summary).collect())
    }

    /// Hours of each window the watch measured a heart rate in, which is the
    /// hours it was on a wrist.
    pub fn worn_hours(&self, edges_ms: Vec<i64>) -> Result<Vec<u32>, WatchError> {
        let edges = edges_of(edges_ms)?;
        let store = self.store.lock().unwrap();
        Ok(store.windowed_hours(self.device_id, SampleKind::HeartRate, &edges)?)
    }

    pub fn delete_workout(&self, id: i64) -> Result<(), WatchError> {
        let store = self.store.lock().unwrap();
        store.delete_workout(self.device_id, WorkoutId(id))?;
        drop(store);
        self.transport.changed();
        Ok(())
    }

    /// Answers with the session as it now stands: the figures over it are read
    /// back out of the samples the shorter span covers, not carried over.
    pub fn trim_workout(&self, id: i64, ended_at_ms: i64) -> Result<WorkoutSummary, WatchError> {
        let store = self.store.lock().unwrap();
        let ended_at = UnixMillis(ended_at_ms).to_seconds();
        if store.trim_workout(self.device_id, WorkoutId(id), ended_at)? == Trim::OutsideSession {
            return Err(WatchError::Refused {
                reason: format!("{ended_at_ms} is not inside session {id}"),
            });
        }
        let now = UnixMillis(now_ms());
        let Some(trimmed) = report::workout(&store, self.device_id, WorkoutId(id), now)? else {
            return Err(WatchError::Refused {
                reason: format!("no session {id}"),
            });
        };
        drop(store);
        self.transport.changed();
        Ok(workout_summary(trimmed))
    }

    pub fn activity_totals(&self, from_ms: i64, to_ms: i64) -> Result<ActivityTotals, WatchError> {
        let store = self.store.lock().unwrap();
        let minutes = store.activity_minutes(
            self.device_id,
            UnixMillis(from_ms).to_seconds(),
            UnixMillis(to_ms).to_seconds(),
        )?;
        let summed = activity::totals(&minutes);
        Ok(ActivityTotals {
            steps: summed.steps,
            distance_metres: summed.distance.0,
            ascent_metres: summed.ascent.0,
            calories: summed.calories.0,
        })
    }

    /// Metres climbed in each window the watch reports one for. It gives a
    /// climb per minute and never a descent, so there is no height above
    /// anything to be had from it — only how steeply the ground rose.
    pub fn ascent_series(&self, from_ms: i64, to_ms: i64) -> Result<Vec<Point>, WatchError> {
        let store = self.store.lock().unwrap();
        let climbed = report::ascents(&store, self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))?;
        drop(store);

        Ok(climbed
            .into_iter()
            .map(|(at, ascent)| Point {
                at_ms: at.0,
                value: ascent.0,
                origin: Origin::Stored,
            })
            .collect())
    }

    pub fn detected_activities(
        &self,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<Vec<DetectedActivity>, WatchError> {
        let store = self.store.lock().unwrap();
        let found = report::detected_activities(
            &store,
            self.device_id,
            UnixMillis(from_ms),
            UnixMillis(to_ms),
        )?;
        Ok(found
            .into_iter()
            .map(|session| {
                let kind = SportId(session.subcategory as u32);
                DetectedActivity {
                    started_at_ms: session.started_at.to_millis().0,
                    ended_at_ms: session.ended_at.to_millis().0,
                    subcategory: session.subcategory as i32,
                    activity: sport::name(kind),
                    on_foot: sport::on_foot(kind),
                    travel: travel(kind),
                    steps: session.steps,
                    distance_metres: session.distance.0,
                    calories: session.calories.0,
                }
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

    /// The route so far as the watch's own screens want it: distance in the
    /// centimetres they scale to kilometres, speed already converted to the
    /// kilometres an hour they print unaltered. Nothing is sent until there is
    /// a distance, which is what the screens gate on.
    pub fn push_route_to_watch(&self) -> Result<(), WatchError> {
        let Some(active) = self.store.lock().unwrap().active_workout(self.device_id)? else {
            return Ok(());
        };
        let started_ms = active.started_at.0 * 1000;
        let mut held = self.live_distance.lock().unwrap();
        let carried = held
            .take()
            .filter(|live| live.workout_started_at == active.started_at);
        let from_ms = carried
            .as_ref()
            .map_or(started_ms, |live| live.settled_through_ms);
        let settled_metres = carried.as_ref().map_or(0.0, |live| live.settled_metres);

        let Some(track) = self.track(from_ms, now_ms(), active.subcategory as i32)? else {
            return Ok(());
        };
        let split = track.track.distance_split(UnixMillis(now_ms() - SETTLE_MS));
        *held = Some(LiveDistance {
            workout_started_at: active.started_at,
            settled_metres: settled_metres + split.settled.0,
            settled_through_ms: split.settled_through.map_or(from_ms, |through| through.0),
        });
        drop(held);

        let distance_cm =
            ((settled_metres + split.settled.0 + split.tail.0) * 100.0).round() as i32;
        if distance_cm == 0 {
            return Ok(());
        }
        let speed_kph = track
            .track
            .speed_series()
            .last()
            .map(|(_, speed)| (speed.0 * 3.6).round() as i32);
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .workout_live_data(wpp::client::LiveRoute {
                pace: None,
                distance: Some(distance_cm),
                speed: speed_kph,
            });
        self.dispatch(actions)
    }

    pub fn refresh_workout_status(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.workout_status();
        self.dispatch(actions)
    }

    pub fn set_gps_status(&self, present: bool) -> Result<(), WatchError> {
        let status = if present {
            wpp::client::GpsStatus::Present
        } else {
            wpp::client::GpsStatus::Absent
        };
        let actions = self.inner.lock().unwrap().client.gps_status(status);
        self.dispatch(actions)
    }

    /// A field left as None is left out of the frame. The watch acknowledges
    /// nothing here, so a success is only that the frame went out.
    pub fn push_live_route(
        &self,
        speed: Option<i32>,
        distance: Option<i32>,
        pace: Option<i32>,
    ) -> Result<(), WatchError> {
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .workout_live_data(wpp::client::LiveRoute {
                pace,
                distance,
                speed,
            });
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

    pub fn set_zone(
        &self,
        gmt_offset: i32,
        next_change: Option<DstChange>,
    ) -> Result<(), WatchError> {
        let zone = wpp::client::Zone {
            gmt_offset,
            next_change: next_change.map(|c| wpp::client::DstChange {
                at: UnixMillis(c.at_ms).to_seconds(),
                gmt_offset: c.gmt_offset_seconds,
            }),
        };
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .set_zone(zone, UnixMillis(now_ms()));
        self.dispatch(actions)
    }

    pub fn phone_clock_changed(&self) -> Result<(), WatchError> {
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .phone_clock_changed(UnixMillis(now_ms()));
        self.dispatch(actions)
    }

    pub fn request_device_config(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.request_device_config();
        self.dispatch(actions)
    }

    /// Debug: starts a read of `len` bytes of external SPI flash at `addr`. The
    /// bytes arrive over the link asynchronously, so the caller polls
    /// [`WatchService::spi_flash_progress`] and takes them with
    /// [`WatchService::spi_flash_take`]. The watch reads in 16-byte units, so
    /// `len` should be a multiple of 16.
    pub fn spi_flash_read(&self, addr: u32, len: u32) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.spi_flash_read(addr, len);
        self.dispatch(actions)
    }

    pub fn spi_flash_progress(&self) -> Option<SpiFlashProgress> {
        self.inner
            .lock()
            .unwrap()
            .client
            .spi_flash_progress()
            .map(SpiFlashProgress::from)
    }

    pub fn spi_flash_take(&self) -> Option<Vec<u8>> {
        self.inner.lock().unwrap().client.spi_flash_take()
    }

    pub fn spi_flash_reset(&self) {
        self.inner.lock().unwrap().client.spi_flash_reset();
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
                name: sport::name(SportId(*id)),
                enabled: true,
            })
            .collect();
        for known in sport::ACTIVITIES {
            if !current.contains(&known.id.0) {
                activities.push(Activity {
                    id: known.id.0,
                    name: known.name.to_string(),
                    enabled: false,
                });
            }
        }
        activities
    }

    /// What the watch's menu holds, or None until it has said. [`Self::activities`]
    /// lists every sport either way, so it cannot tell an empty menu from an
    /// unread one, and saving an unread one would overwrite the watch's.
    pub fn activity_menu(&self) -> Option<Vec<u32>> {
        self.inner.lock().unwrap().client.activities()
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
                let face = sport::face(SportId(*id));
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
                    name: sport::name(SportId(*id)),
                    face_mode: face.mode,
                    flag: face.flag,
                    glyphs,
                }
            })
            .collect();
        let actions = self.inner.lock().unwrap().client.set_activities(&screens);
        self.dispatch(actions)
    }

    pub fn health_features(&self) -> Vec<HealthFeature> {
        let enabled = self.enabled_features();
        HEALTH_FEATURES
            .iter()
            .map(|(id, name, description)| HealthFeature {
                id: *id,
                name: name.to_string(),
                description: description.to_string(),
                enabled: enabled.iter().any(|f| f.id == FeatureId(*id)),
            })
            .collect()
    }

    pub fn set_health_feature(&self, id: u16, enabled: bool) -> Result<(), WatchError> {
        let id = FeatureId(id);
        let mut features = self.enabled_features();
        features.retain(|f| f.id != id);
        if enabled {
            features.push(Feature {
                id,
                schedule: FeatureSchedule::Permanent,
            });
        }
        self.commit_features(features)
    }

    /// Reports the respiratory scan's window if one is still running. The scan
    /// is never left on: it is armed a night at a time, so a caller that wants
    /// another one calls [`WatchService::arm_respiratory_scan`] again.
    pub fn respiratory_scan(&self) -> Option<ArmedScan> {
        self.enabled_features()
            .iter()
            .find(|f| f.id == RESPIRATORY_SCAN_MONITORING)
            .and_then(|f| match f.schedule {
                FeatureSchedule::Window { end, .. } => Some(ArmedScan { ends_at: end.0 }),
                FeatureSchedule::Permanent => None,
            })
    }

    pub fn arm_respiratory_scan(&self) -> Result<(), WatchError> {
        let start = now_seconds();
        let mut features = self.enabled_features();
        features.retain(|f| f.id != RESPIRATORY_SCAN_MONITORING);
        features.push(Feature {
            id: RESPIRATORY_SCAN_MONITORING,
            schedule: FeatureSchedule::Window {
                start,
                end: start + SCAN_WINDOW,
            },
        });
        self.commit_features(features)
    }

    pub fn cancel_respiratory_scan(&self) -> Result<(), WatchError> {
        let mut features = self.enabled_features();
        features.retain(|f| f.id != RESPIRATORY_SCAN_MONITORING);
        self.commit_features(features)
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
            UnixMillis(at_ms),
            match edge {
                SetEdge::Start => SetBoundary::Start,
                SetEdge::End => SetBoundary::End,
            },
        )?;
        drop(store);
        self.transport.changed();
        Ok(())
    }

    pub fn record_fixes(&self, fixes: Vec<LocationFix>) -> Result<(), WatchError> {
        if fixes.is_empty() {
            return Ok(());
        }
        let held: Vec<wpp::track::Fix> = fixes.iter().map(|fix| fix.held()).collect();
        let mut store = self.store.lock().unwrap();
        store.record_fixes(self.device_id, &held)?;
        drop(store);
        self.transport.changed();
        Ok(())
    }

    /// When the running session is one that covers ground, the moment it
    /// started. What the phone's receiver is switched on and off by, and cheap
    /// enough to ask on every change: one indexed row.
    pub fn route_recording_since(&self) -> Result<Option<i64>, WatchError> {
        let store = self.store.lock().unwrap();
        let Some(active) = store.active_workout(self.device_id)? else {
            return Ok(None);
        };
        match travel(SportId(active.subcategory as u32)) {
            Travel::Ground => Ok(Some(active.started_at.0 * 1000)),
            Travel::InPlace => Ok(None),
        }
    }

    /// None where the activity covers no ground, and where it does but nothing
    /// was recorded: an empty route and no route are the same to every caller.
    pub fn track(
        &self,
        from_ms: i64,
        to_ms: i64,
        subcategory: i32,
    ) -> Result<Option<Arc<RouteTrack>>, WatchError> {
        let Some(limits) = sport::limits(SportId(subcategory as u32)) else {
            return Ok(None);
        };
        let store = self.store.lock().unwrap();
        let fixes = store.track_points(self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))?;
        drop(store);

        let cleaned = track::clean(&fixes, limits);
        if cleaned.is_empty() {
            return Ok(None);
        }
        Ok(Some(Arc::new(RouteTrack { track: cleaned })))
    }

    pub fn night(&self, from_ms: i64, to_ms: i64) -> Result<Night, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(night_record(report::night(
            &store,
            self.device_id,
            UnixMillis(from_ms),
            UnixMillis(to_ms),
        )?))
    }

    /// One entry for each window the watch staged any sleep in; a window it
    /// did not is left out rather than reported as a night of none.
    pub fn night_trend(&self, windows: Vec<SleepSpan>) -> Result<Vec<NightTrend>, WatchError> {
        let windows: Vec<std::ops::Range<UnixMillis>> = windows
            .iter()
            .map(|window| UnixMillis(window.from_ms)..UnixMillis(window.to_ms))
            .collect();
        let store = self.store.lock().unwrap();
        Ok(report::night_trend(&store, self.device_id, &windows)?
            .into_iter()
            .map(|night| NightTrend {
                asleep_from_ms: night.asleep.start.0,
                asleep_to_ms: night.asleep.end.0,
                asleep_ms: night.asleep_for.as_millis(),
                score: night.score.map(sleep_score),
                heart: night.heart.map(|heart| NightHeart {
                    median_bpm: heart.median.0,
                    resting_bpm: heart.resting.0,
                }),
            })
            .collect())
    }

    /// When the first session of any kind was recorded: the watch keeps no
    /// workouts from before it was paired, so there is no knowing what was
    /// done before then.
    pub fn first_workout_at_ms(&self) -> Result<Option<i64>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .first_workout(self.device_id)?
            .map(|at| at.to_millis().0))
    }

    /// Finished sessions of one activity, oldest first.
    pub fn workout_trend(
        &self,
        subcategory: i32,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<Vec<WorkoutTrend>, WatchError> {
        let store = self.store.lock().unwrap();
        let trend = report::workout_trend(
            &store,
            self.device_id,
            SportId(subcategory as u32),
            UnixMillis(from_ms),
            UnixMillis(to_ms),
        )?;
        Ok(trend
            .into_iter()
            .map(|entry| WorkoutTrend {
                workout: workout_summary(entry.workout),
                heart: entry.effort.map(|effort| SessionHeart {
                    median_bpm: effort.median.0,
                    p95_bpm: effort.p95.0,
                    measured_ms: effort.measured_ms,
                    dropout_ms: effort.dropout_ms,
                }),
            })
            .collect())
    }

    /// A few minutes awake in the middle of a night leave the body on its
    /// sleeping baseline, so a stretch survives them as one span.
    pub fn sleep_spans(&self, from_ms: i64, to_ms: i64) -> Result<Vec<SleepSpan>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(report::asleep_spans(&store, self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))?
            .into_iter()
            .map(|span| SleepSpan {
                from_ms: span.start.0,
                to_ms: span.end.0,
            })
            .collect())
    }

    /// The median waking rate of each window, readings taken asleep, in a
    /// session or while recovering from one left out. None for a window with
    /// nothing left.
    /// Milliseconds in each heart rate zone over the sessions that started in
    /// each window, every kind of session together. Only sessions are
    /// counted: outside one the watch reads a rate every ten minutes, too
    /// seldom to say how long any of it lasted.
    pub fn zone_time(
        &self,
        edges_ms: Vec<i64>,
        floors_bpm: Vec<u16>,
    ) -> Result<Vec<Vec<i64>>, WatchError> {
        let edges = edges_of(edges_ms)?;
        let floors: Vec<Bpm> = floors_bpm.into_iter().map(Bpm).collect();
        let store = self.store.lock().unwrap();
        Ok(report::zone_time(&store, self.device_id, &edges, &floors)?
            .into_iter()
            .map(|window| window.into_iter().map(|spent| spent.as_millis()).collect())
            .collect())
    }

    pub fn add_note(&self, at_ms: i64, text: String) -> Result<i64, WatchError> {
        let text = text.trim();
        if text.is_empty() {
            return Err(WatchError::Refused {
                reason: "a note needs some text".to_string(),
            });
        }
        let store = self.store.lock().unwrap();
        Ok(store.add_note(UnixMillis(at_ms), text)?)
    }

    pub fn notes(&self) -> Result<Vec<Note>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .notes()?
            .into_iter()
            .map(|note| Note {
                id: note.id,
                at_ms: note.at.0,
                text: note.text,
            })
            .collect())
    }

    pub fn delete_note(&self, id: i64) -> Result<(), WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store.delete_note(id)?)
    }

    pub fn awake_heart(&self, edges_ms: Vec<i64>) -> Result<Vec<Option<AwakeHeart>>, WatchError> {
        let edges = edges_of(edges_ms)?;
        let store = self.store.lock().unwrap();
        Ok(report::awake_heart(&store, self.device_id, &edges)?
            .into_iter()
            .map(|window| {
                window.map(|rest| AwakeHeart {
                    median_bpm: rest.median.0,
                    readings: rest.readings,
                })
            })
            .collect())
    }

    pub fn has_staging(&self, from_ms: i64, to_ms: i64) -> Result<bool, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store.has_staging(
            self.device_id,
            UnixMillis(from_ms).to_seconds(),
            UnixMillis(to_ms).to_seconds(),
        )?)
    }

    pub fn charging(&self, from_ms: i64, to_ms: i64) -> Result<Vec<Marker>, WatchError> {
        let store = self.store.lock().unwrap();
        let mut out = Vec::new();
        for period in store.charge_periods(self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))? {
            out.push(Marker {
                at_ms: period.from.0,
                edge: SetEdge::Start,
            });
            if let Some(end) = period.to {
                out.push(Marker {
                    at_ms: end.0,
                    edge: SetEdge::End,
                });
            }
        }
        Ok(out)
    }

    pub fn markers(&self, from_ms: i64, to_ms: i64) -> Result<Vec<Marker>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .markers(self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))?
            .into_iter()
            .map(|marker| Marker {
                at_ms: marker.at.0,
                edge: match marker.boundary {
                    SetBoundary::Start => SetEdge::Start,
                    SetBoundary::End => SetEdge::End,
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
        let listed = report::ecg_summaries(&store, self.device_id)?;
        self.note_unreadable(&listed.unreadable);
        Ok(listed.summaries.iter().map(ecg_summary).collect())
    }

    pub fn afib_episodes(&self) -> Result<Vec<AfibEpisode>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .afib_episodes(self.device_id)?
            .into_iter()
            .map(|row| AfibEpisode {
                id: row.id,
                measured_at_ms: row.measured_at.0,
                seconds: row.duration.as_secs() as u32,
                rhythm: row.verdict.and_then(ppg_rhythm),
                alerted: row.attrib.map(|a| a == STORED_MEASURE_ATTRIB_CONFIRMED),
            })
            .collect())
    }

    pub fn afib_alert(&self, id: i64) -> Result<Option<AfibAlert>, WatchError> {
        use wpp::signal::PpgAfibRule;
        let store = self.store.lock().unwrap();
        let Some(alert) = report::afib_alert(&store, self.device_id, id)? else {
            return Ok(None);
        };
        self.note_unreadable(&alert.unreadable_readings);
        self.note_unreadable(&alert.unreadable_ecgs);
        Ok(Some(AfibAlert {
            id,
            measured_at_ms: alert.measured_at.0,
            run: PpgAfibRule::RUN as u32,
            window_hours: (PpgAfibRule::WINDOW_SECS / 3600) as u32,
            counted: alert.counted,
            empty: alert.empty,
            readings: alert.readings.into_iter().map(afib_reading).collect(),
            ecg_after: alert.ecg_after.as_ref().map(ecg_summary),
        }))
    }

    pub fn ecg(&self, id: i64) -> Result<Option<EcgRecording>, WatchError> {
        let store = self.store.lock().unwrap();
        let Some(detail) = report::ecg(&store, EcgId(id))? else {
            return Ok(None);
        };
        let recording = &detail.stored.recording;
        let leads = recording
            .leads()
            .iter()
            .enumerate()
            .map(|(i, column)| EcgLead {
                name: column
                    .lead
                    .map_or_else(|| format!("CH{i}"), |lead| lead.name().to_string()),
                millivolts: column
                    .samples
                    .iter()
                    .map(|c| Millivolts::from_counts(*c).0)
                    .collect(),
            })
            .collect();
        Ok(Some(EcgRecording {
            id,
            measured_at_ms: detail.stored.measured_at.0,
            sampling_hz: u32::from(recording.rate.get()),
            leads,
            heart_rate: detail.verdict.rate.map(|bpm| u32::from(bpm.0)),
            rhythm: detail.verdict.rhythm.map(ecg_rhythm),
            analysis: ecg_analysis(&detail.reading),
        }))
    }
}

fn ppg_rhythm(code: i64) -> Option<PpgRhythm> {
    let rhythm = wpp::signal::PpgRhythm::of(i32::try_from(code).ok()?)?;
    Some(match rhythm {
        wpp::signal::PpgRhythm::Undefined => PpgRhythm::Undefined,
        wpp::signal::PpgRhythm::SinusRhythm => PpgRhythm::SinusRhythm,
        wpp::signal::PpgRhythm::Afib => PpgRhythm::Afib,
        wpp::signal::PpgRhythm::Other => PpgRhythm::Other,
        wpp::signal::PpgRhythm::Noise => PpgRhythm::Noise,
    })
}

fn ecg_summary(summary: &report::EcgSummary) -> EcgSummary {
    EcgSummary {
        id: summary.id.0,
        measured_at_ms: summary.measured_at.0,
        seconds: summary.seconds,
        leads: summary.leads,
        heart_rate: summary.verdict.rate.map(|bpm| u32::from(bpm.0)),
        rhythm: summary.verdict.rhythm.map(ecg_rhythm),
    }
}

fn afib_reading(reading: report::AfibReading) -> AfibReading {
    use wpp::rhythm::{PulseCheck, PulseVerdict};
    AfibReading {
        id: reading.id,
        measured_at_ms: reading.measured_at.0,
        heart_rate: reading.pulse.heart_rate.map(|bpm| u32::from(bpm.0)),
        intervals_ms: reading
            .pulse
            .intervals
            .iter()
            .map(|interval| interval.as_millis() as u32)
            .collect(),
        analysis: match reading.pulse.check {
            PulseCheck::NoWatchRate => PulseAnalysis::NoWatchRate,
            PulseCheck::Read(PulseVerdict::TooFew { .. }) => PulseAnalysis::TooFew,
            PulseCheck::Read(PulseVerdict::Disagrees { .. }) => PulseAnalysis::Disagrees,
            PulseCheck::Read(PulseVerdict::Shifted(_)) => PulseAnalysis::Shifted,
            PulseCheck::Read(PulseVerdict::Assessed(rhythm)) => PulseAnalysis::Assessed {
                rhythm: rhythm_analysis(&rhythm),
            },
        },
        asleep: reading.asleep,
    }
}

fn rhythm_analysis(rhythm: &wpp::rhythm::Rhythm) -> RhythmAnalysis {
    use wpp::rhythm::Pattern;
    RhythmAnalysis {
        pattern: match rhythm.pattern {
            Pattern::Steady => RhythmPattern::Steady,
            Pattern::Breathing => RhythmPattern::Breathing,
            Pattern::Variable => RhythmPattern::Variable,
            Pattern::Irregular => RhythmPattern::Irregular,
        },
        wave: rhythm.wave.as_ref().map(|wave| BreathingWave {
            curve: wave
                .curve
                .iter()
                .map(|(at, ms)| WavePoint {
                    at_ms: at.0.round() as u32,
                    ms: ms.0,
                })
                .collect(),
        }),
    }
}

fn ecg_analysis(reading: &wpp::rhythm::EcgReading) -> EcgAnalysis {
    use wpp::rhythm::EcgVerdict;
    match &reading.verdict {
        EcgVerdict::TooFew { .. } => EcgAnalysis::TooFew,
        EcgVerdict::Noisy => EcgAnalysis::Noisy,
        EcgVerdict::Shifted(_) => EcgAnalysis::Shifted,
        EcgVerdict::Assessed(rhythm) => EcgAnalysis::Assessed {
            rhythm: rhythm_analysis(rhythm),
            p_waves: reading.beats.iter().filter(|b| b.p_wave).count() as u32,
            early_beats: rhythm.early_beats as u32,
        },
    }
}

fn ecg_rhythm(rhythm: wpp::signal::Rhythm) -> EcgRhythm {
    match rhythm {
        wpp::signal::Rhythm::NoAfib => EcgRhythm::NoAfib,
        wpp::signal::Rhythm::Afib => EcgRhythm::Afib,
        wpp::signal::Rhythm::Inconclusive => EcgRhythm::Inconclusive,
        wpp::signal::Rhythm::PoorRecording => EcgRhythm::PoorRecording,
        wpp::signal::Rhythm::RateOutOfRange => EcgRhythm::RateOutOfRange,
        wpp::signal::Rhythm::NoResult => EcgRhythm::NoResult,
    }
}

fn travel(id: SportId) -> Travel {
    match sport::limits(id) {
        Some(_) => Travel::Ground,
        None => Travel::InPlace,
    }
}

fn workout_summary(workout: report::Workout) -> WorkoutSummary {
    let row = workout.row;
    let kind = row.sport();
    WorkoutSummary {
        id: row.id.0,
        started_at_ms: row.started_at.to_millis().0,
        ended_at_ms: row.ended_at.map(|end| end.to_millis().0),
        subcategory: row.subcategory as i32,
        activity: sport::name(kind),
        on_foot: sport::on_foot(kind),
        travel: travel(kind),
        calories: workout.calories.map(|burned| burned.0),
    }
}

fn sleep_score(score: wpp::sleep::Score) -> SleepScore {
    SleepScore {
        total: score.total,
        duration: score.duration,
        efficiency: score.efficiency,
        deep: score.deep,
        rem: score.rem,
        continuity: score.continuity,
    }
}

fn night_record(night: wpp::sleep::Night) -> Night {
    Night {
        stages: night
            .stages
            .iter()
            .map(|band| SleepBand {
                from_ms: band.from.0,
                to_ms: band.to.0,
                stage: SleepStage::of(band.level),
            })
            .collect(),
        asleep_from_ms: night.asleep.as_ref().map(|span| span.start.0),
        asleep_to_ms: night.asleep.as_ref().map(|span| span.end.0),
        score: night.score.map(sleep_score),
    }
}

/// None for edges out of order, which only a broken caller can send.
fn edges_of(edges_ms: Vec<i64>) -> Result<wpp::units::Edges, WatchError> {
    wpp::units::Edges::new(edges_ms.into_iter().map(UnixMillis).collect()).ok_or_else(|| {
        WatchError::Refused {
            reason: "window edges must be in time order".to_string(),
        }
    })
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn now_seconds() -> UnixTime {
    UnixMillis(now_ms()).to_seconds()
}

impl WatchService {
    fn note_unreadable(&self, rows: &[impl std::fmt::Display]) {
        let mut unreadable = self.unreadable_rows.lock().unwrap();
        for row in rows {
            unreadable.note(row);
        }
    }

    fn enabled_features(&self) -> Vec<Feature> {
        self.inner.lock().unwrap().client.features(now_seconds())
    }

    /// The store is the only record of the set, so it is written before the
    /// watch: a write that never reaches the watch is re-asserted on the next
    /// connection, where the reverse would lose the change entirely.
    fn commit_features(&self, features: Vec<Feature>) -> Result<(), WatchError> {
        self.store
            .lock()
            .unwrap()
            .set_features(self.device_id, &features)?;
        let actions = self.inner.lock().unwrap().client.set_features(features, UnixMillis(now_ms()));
        self.dispatch(actions)
    }

    fn dispatch(&self, actions: Vec<Action>) -> Result<(), WatchError> {
        let mut actions: std::collections::VecDeque<Action> = actions.into();
        let mut changed = false;
        let mut failure: Option<WatchError> = None;
        let mut unkept = false;
        while let Some(action) = actions.pop_front() {
            match action {
                Action::Send(frame) | Action::Delete(frame) => {
                    if let Err(error) = self.write(&frame) {
                        failure.get_or_insert(error);
                    }
                }
                Action::Finished => changed = true,
                Action::Reconnect => self.transport.reconnect(),
                // A token acknowledges every record before it, so nothing later
                // may be acknowledged past one that was not kept.
                Action::Store { .. } if unkept => {}
                Action::Store { token, records } => {
                    if let Err(error) = self.store.lock().unwrap().store(self.device_id, &records) {
                        failure.get_or_insert(error.into());
                        unkept = true;
                        let refetch = self.inner.lock().unwrap().client.handle(Event::StoreFailed);
                        actions.extend(refetch);
                        continue;
                    }
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

        let kept = self.inner.lock().unwrap().client.take_kept_watermarks();
        if !kept.is_empty() {
            self.store
                .lock()
                .unwrap()
                .set_watermarks(self.device_id, &kept)?;
        }

        if changed {
            self.transport.changed();
        }
        failure.map_or(Ok(()), Err)
    }

    fn write(&self, message: &Frame) -> Result<(), WatchError> {
        let wire = message.to_wire().map_err(|error| WatchError::Protocol {
            reason: error.to_string(),
        })?;
        self.transport.write(wire.iter().map(Frame::to_bytes).collect());
        Ok(())
    }

    fn draw(&self, frame: &Frame) -> Result<(), WatchError> {
        let glyph = GlyphRequest::parse(frame);
        let icon = IconRequest::parse(frame);
        if glyph.is_none() && icon.is_none() {
            return Ok(());
        }
        let formats = self.inner.lock().unwrap().client.image_formats().to_vec();

        if let Some(request) = glyph {
            let (width, height) = request.size(&formats);
            let bitmaps: Vec<Mono> = request
                .glyphs
                .iter()
                .map(|g| self.rasterizer.glyph(g.codepoint, width, height).pack())
                .collect();
            return self.write(&request.reply(&bitmaps));
        }
        if let Some(request) = icon {
            let (width, height) = request.size(&formats);
            let drawn = self
                .rasterizer
                .icon(request.app_id.clone(), width, height)
                .pack();
            return self.write(&request.reply(&drawn));
        }
        Ok(())
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

impl LocationFix {
    fn held(&self) -> wpp::track::Fix {
        wpp::track::Fix {
            at: UnixMillis(self.at_ms),
            lat_e7: self.lat_e7,
            lon_e7: self.lon_e7,
            altitude_cm: self.altitude_cm,
            accuracy_cm: self.accuracy_cm,
            speed_mm_s: self.speed_mm_s,
            bearing_cdeg: self.bearing_cdeg,
        }
    }
}

/// A route read once and asked many questions. Cleaning and projecting on
/// every call would re-read the whole session for each frame of a pinch.
#[derive(uniffi::Object)]
pub struct RouteTrack {
    track: Track,
}

#[uniffi::export]
impl RouteTrack {
    pub fn summary(&self) -> TrackSummary {
        let band = self.track.speed_band();
        TrackSummary {
            distance_metres: self.track.distance().0,
            moving_secs: self.track.moving_secs(),
            average_speed_m_s: self.track.average_speed().map(|speed| speed.0),
            slow_m_s: band.map(|(slow, _)| slow.0),
            fast_m_s: band.map(|(_, fast)| fast.0),
            fixes: self.track.fixes().count() as u32,
        }
    }

    pub fn speed_series(&self) -> Vec<Point> {
        self.track
            .speed_series()
            .into_iter()
            .map(|(at, speed)| Point {
                at_ms: at.0,
                value: speed.0,
                origin: Origin::Stored,
            })
            .collect()
    }

    pub fn fit(&self, width_px: f64, height_px: f64, padding_px: f64) -> MapView {
        of_view(track::fit(&self.track, width_px, height_px, padding_px))
    }

    /// Where the body was at a moment, for a cursor. Deliberately not read off
    /// `frame`: the drawn path is simplified, and a stop is exactly what
    /// simplification removes.
    pub fn cursor(&self, view: MapView, at_ms: i64) -> Option<MapPoint> {
        track::cursor(&self.track, view.held(), UnixMillis(at_ms)).map(|point| MapPoint {
            at_ms: point.at.0,
            x_px: point.x_px,
            y_px: point.y_px,
            speed_m_s: point.speed.map(|speed| speed.0),
        })
    }

    pub fn frame(&self, view: MapView) -> MapFrame {
        let drawn = track::frame(&self.track, view.held());
        MapFrame {
            tiles: drawn
                .tiles
                .into_iter()
                .map(|placed| MapTile {
                    z: placed.tile.z,
                    x: placed.tile.x,
                    y: placed.tile.y,
                    left_px: placed.left_px,
                    top_px: placed.top_px,
                    size_px: placed.size_px,
                })
                .collect(),
            path: drawn
                .path
                .into_iter()
                .map(|points| MapPath {
                    points: points
                        .into_iter()
                        .map(|point| MapPoint {
                            at_ms: point.at.0,
                            x_px: point.x_px,
                            y_px: point.y_px,
                            speed_m_s: point.speed.map(|speed| speed.0),
                        })
                        .collect(),
                })
                .collect(),
            metres_per_pixel: drawn.metres_per_pixel,
        }
    }
}

#[uniffi::export]
pub fn pan_map(view: MapView, dx_px: f64, dy_px: f64) -> MapView {
    of_view(track::pan(view.held(), dx_px, dy_px))
}

#[uniffi::export]
pub fn zoom_map(view: MapView, factor: f64, at_x_px: f64, at_y_px: f64) -> MapView {
    of_view(track::zoom_about(view.held(), factor, at_x_px, at_y_px))
}

impl MapView {
    fn held(&self) -> track::View {
        track::View {
            centre: track::Position {
                lat: self.centre_lat,
                lon: self.centre_lon,
            },
            zoom: self.zoom,
            width_px: self.width_px,
            height_px: self.height_px,
        }
    }
}

fn of_view(view: track::View) -> MapView {
    MapView {
        centre_lat: view.centre.lat,
        centre_lon: view.centre.lon,
        zoom: view.zoom,
        width_px: view.width_px,
        height_px: view.height_px,
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
            let wire = message.to_wire().expect("pairing objects fit a frame");
            self.transport.write(wire.iter().map(Frame::to_bytes).collect());
        }
        if moved {
            self.transport.changed();
        }
    }
}
