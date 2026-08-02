//! The Kotlin-facing surface.
//!
//! Deliberately narrow: flat records rather than domain types, and one call
//! per screenful rather than per sample. The Store -> Stored -> Delete
//! ordering that protects watch data is enforced in here, so no caller can
//! delete a measurement that has not been committed.

use std::sync::Mutex;

use wpp::activity;
use wpp::ancs::{self, NotificationCenter, NotificationId};
use wpp::capture::{FrameReassembler, StreamItem};
use wpp::client::{Action, Category, Client, Credentials, Event, Phase};
use wpp::image::{GlyphRequest, IconRequest, Mono};
use wpp::units::{Celsius, Millivolts, UnixMillis};
use wpp::Frame;
use wpp_store::Store;

uniffi::setup_scaffolding!();

/// A night at roughly one point per horizontal pixel.
const NIGHT_POINTS: u32 = 1200;

fn origin_of(source: i64) -> Origin {
    if source == 1 {
        Origin::Live
    } else {
        Origin::Stored
    }
}

/// Series the watch keeps separately, each walked with its own watermark.
/// Category 0 is the body stream carrying heart rate and 255 the per-minute
/// activity stream, both of which have their own command; the rest are the
/// VasistasType values the official app asks for.
/// Taken from the end, so the order walked is: body (heart rate), activity,
/// 10 (core temperature), 11 (HRV), 12 (respiratory rate), 6, then 8, 9, 5 —
/// which carry SpO2 and AHI in bulk and would otherwise starve the rest.
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
    // Not named `message`: uniffi maps this onto a Kotlin exception, where
    // that field collides with Throwable.
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
    Spo2,
    /// Climb and the other running totals beside the steps, all reset at local
    /// midnight. Metres, kilocalories, metres, hours once scaled.
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

    /// Divisor from the stored wire value to the displayed unit.
    fn scale(self) -> f64 {
        match self {
            Metric::Temperature => 1000.0,
            // Centimetres, and hundredths of a kilocalorie.
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
    /// The watch refused the association secret.
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

/// A stretch of walking or running found in the activity stream.
///
/// Not a [`WorkoutSummary`]: the watch reports those itself and they are what
/// someone started deliberately. These are derived from step cadence here on
/// the phone, the same division the official app makes, and they are only as
/// good as that inference.
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

/// The next daylight-saving change the phone's time zone knows about. Absent
/// for a zone that has none ahead of it.
#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct DstChange {
    pub at_ms: i64,
    pub gmt_offset_seconds: i32,
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
    /// Whether a charger is attached, or `None` when the last state reading is
    /// too old to answer for the present.
    ///
    /// The distinction matters: reporting "not charging" from a stale reading
    /// and reporting "we do not know" look the same on screen, but claiming
    /// "charging" from one would assert the very thing being checked.
    pub charging: Option<bool>,
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

/// A stretch the watch staged, from `activity_minute.sleep_level`.
///
/// The watch's own classifier, not an inference: a window it did not stage
/// produces no band rather than a guess.
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

/// One band per run of the same stage. The watch dates a window per record, so
/// a stretch of walking arrives as a dozen one-minute bands that are one thing.
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

/// A night out of 100, with the parts that made it so a total can be argued
/// with. See `wpp::sleep` for what each one measures.
#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct SleepScore {
    pub total: u8,
    pub duration: u8,
    pub efficiency: u8,
    pub deep: u8,
    pub rem: u8,
    pub continuity: u8,
}

/// One night's screen: the two series it draws, the periods it shades, and the
/// numbers it puts at the top.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct Night {
    pub hr: Vec<Point>,
    /// What the watch staged, in order, and the only source of the sleep
    /// period. Empty for a night it did not stage.
    pub stages: Vec<SleepBand>,
    /// Off the wrist, so a hole in the series is explained rather than drawn as
    /// missing data.
    pub charging: Vec<Marker>,
    pub asleep_from_ms: Option<i64>,
    pub asleep_to_ms: Option<i64>,
    pub lowest_hr: Option<f64>,
    /// Absent for a night with no sleep staged in it.
    pub score: Option<SleepScore>,
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
    /// True while the watch is taking an ECG and streaming the waveform.
    pub measuring: bool,
}

/// One lead of a recording, already converted to millivolts.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct EcgLead {
    pub name: String,
    pub millivolts: Vec<f64>,
}

/// A recording as listed, without carrying its samples across the boundary.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct EcgSummary {
    pub id: i64,
    pub measured_at_ms: i64,
    pub seconds: f64,
    pub leads: u32,
    /// Median rate over the recording, from its own R waves. Absent when too
    /// few beats were found to be worth quoting.
    pub heart_rate: Option<u32>,
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
    /// Drop the link and establish it again.
    fn reconnect(&self);
}

/// The phone's ANCS server, which the host runs and this crate drives.
///
/// Separate from [`Transport`] because it is a different link in the opposite
/// direction: here the phone is the GATT server and the watch the client.
#[uniffi::export(callback_interface)]
pub trait AncsLink: Send + Sync {
    /// Notify the Notification Source characteristic. Always eight bytes.
    fn announce(&self, bytes: Vec<u8>);
    /// Notify the Data Source characteristic. One call per fragment, in order.
    fn attributes(&self, bytes: Vec<u8>);
}

/// Drawing, which needs a font and a canvas and so belongs to the host.
///
/// Both calls must return a bitmap of exactly the size asked for, or an empty
/// one when there is nothing to draw — an unknown app, or a codepoint the
/// host's fonts do not cover.
#[uniffi::export(callback_interface)]
pub trait Rasterizer: Send + Sync {
    /// One character, white on transparent.
    fn glyph(&self, codepoint: u32, width: u8, height: u8) -> Bitmap;
    /// The icon for an installed app, by package name.
    fn icon(&self, app_id: String, width: u8, height: u8) -> Bitmap;
}

/// A rendered bitmap on its way to the watch, in ARGB8888 and row-major —
/// what `Bitmap.getPixels` hands over.
///
/// It is reduced to one bit per pixel on this side rather than the host's, so
/// the threshold and the packing stay with the rest of the protocol.
#[derive(uniffi::Record, Debug, Clone, PartialEq, Eq)]
pub struct Bitmap {
    pub width: u8,
    pub height: u8,
    pub pixels: Vec<u32>,
}

impl Bitmap {
    /// Reject a bitmap whose pixels do not match its declared size instead of
    /// packing past the end of it.
    fn pack(self) -> Mono {
        if self.pixels.len() != self.width as usize * self.height as usize {
            return Mono::empty();
        }
        Mono::pack(&self.pixels, self.width, self.height)
    }
}

/// Where a notification lands on the watch. `AncsConfig.type`.
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

/// What the watch says about phone notifications.
#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq, Eq)]
pub struct NotificationConfig {
    /// The watch will act as a notification client.
    pub accepted: bool,
    /// It will put what it hears on the screen.
    pub displayed: bool,
}

/// The UUIDs of the ANCS server the host has to stand up, so the two sides
/// cannot drift apart.
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
    /// Enabled features as (id, start, end). Write-only on the wire, so this is
    /// the only record of them.
    features: Mutex<Vec<(u16, u32, u32)>>,
    /// Notifications the watch has been told about and can still ask about.
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
            // A fresh link starts a fresh byte stream.
            inner.reassembler.reset();
            inner.client.handle(Event::Connected)
        };
        self.dispatch(actions)
    }

    /// Ask only for the battery. Cheap enough to run while the charging state
    /// is on screen, where a reading minutes old is worse than none.
    pub fn poll_battery(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.client.poll_battery()
        };
        self.dispatch(actions)
    }

    /// The host's clock, on a timer. This is the client's only way to notice
    /// that time has passed without the watch saying anything.
    pub fn tick(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.client.handle(Event::Tick {
                now: wpp::units::UnixMillis(now_ms()),
            })
        };
        self.dispatch(actions)
    }

    /// Object types the watch sent that nothing read, one line each, and forget
    /// them. Empty is the expected answer; anything here is data being thrown
    /// away, which is how ten nights of sleep staging went missing.
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

    /// Feed one GATT notification. Frames span several of these.
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
                    // Whatever it held is lost to this build, and the only way
                    // to find out what that was is to keep the bytes.
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
        // Drawing calls back into the host, which cannot happen while the
        // client is locked: the host is free to call back in on that thread.
        for frame in &frames {
            self.draw(frame);
        }
        self.dispatch(pending)
    }

    pub fn on_disconnected(&self) {
        {
            let mut inner = self.inner.lock().unwrap();
            inner.reassembler.reset();
            // Discarded, not dispatched: there is no link left to send on.
            inner.client.handle(Event::Disconnected);
        }
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

    /// How far the link has got, without the readings that go with it.
    ///
    /// [`Self::snapshot`] answers the same question but reads the database to
    /// do it. The host asks this one for every notification the watch sends,
    /// which during a sync is thousands in a row on the thread delivering
    /// them, so it touches nothing but the client's own state.
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
                inner.client.measuring().is_some(),
            )
        };
        let progress_phase = phase;
        Ok(Snapshot {
            progress: progress_phase,
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
                .map(|(id, start, sub)| WorkoutSummary {
                    id,
                    started_at_ms: start * 1000,
                    ended_at_ms: None,
                    subcategory: sub as i32,
                    activity: activity_name(sub as u32),
                }),
            pending_deletes: pending,
            sync: progress,
            measuring,
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

    /// Walks and runs found in the activity stream over a window, oldest
    /// first.
    ///
    /// Derived on every call rather than stored: the segmentation is an
    /// inference over the records, and a later sync filling in the middle of a
    /// walk has to be able to change its mind about where that walk ended.
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

    /// Set the watch's clock from the phone's. The caller supplies the time and
    /// the zone: the watch's clock is the phone's clock, and only the phone has
    /// a time zone database to read the offsets out of.
    pub fn set_time(
        &self,
        at_ms: i64,
        gmt_offset_seconds: i32,
        next_change: Option<DstChange>,
    ) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.set_time(
            UnixMillis(at_ms).to_seconds(),
            gmt_offset_seconds,
            next_change.map(|change| wpp::client::DstChange {
                at: UnixMillis(change.at_ms).to_seconds(),
                gmt_offset: change.gmt_offset_seconds,
            }),
        );
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

    /// What the watch last said about phone notifications, once it has said.
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

    /// Turn the watch's half of phone notifications on or off.
    ///
    /// The host's ANCS server is the other half and is started separately;
    /// with this on and no server running, the watch finds nothing to talk to.
    pub fn set_notifications(&self, enabled: bool) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.set_notifications(enabled);
        self.dispatch(actions)
    }

    /// The setting the host holds, applied to the watch whenever it reports
    /// something else. Safe before a link exists, and needed after a watch
    /// reboot puts notifications back on by itself.
    pub fn prefer_notifications(&self, enabled: bool) {
        self.inner
            .lock()
            .unwrap()
            .client
            .prefer_notifications(enabled);
    }

    /// Announce a notification. The returned id dismisses it, and is what the
    /// watch quotes back when it asks what the notification says.
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

    /// Take a notification off the watch. Silent if it was never posted.
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

    /// Feed one Control Point write from the watch.
    ///
    /// `max_payload` is what a single Data Source notification can carry: the
    /// negotiated ATT MTU less its three-byte header. A long message is split
    /// across several, so getting this wrong truncates messages rather than
    /// failing outright.
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
            // An id never posted, or already dismissed. The watch asking about
            // one is normal after a restart, and there is nothing to say.
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

    /// Everything one night's screen draws, in a single call.
    ///
    /// The window wants to start in the evening rather than at the sleep it is
    /// looking for: the detection takes its levels from what it is given, so a
    /// window holding only sleep has nothing to compare the sleep against.
    pub fn night(&self, from_ms: i64, to_ms: i64) -> Result<Night, WatchError> {
        let store = self.store.lock().unwrap();
        let hr: Vec<Point> = store
            .series(self.device_id, 1, from_ms, to_ms, NIGHT_POINTS)?
            .into_iter()
            .map(|(at, value, source)| Point {
                at_ms: at,
                value: value as f64,
                origin: origin_of(source),
            })
            .collect();
        let minutes = store.activity_minutes(self.device_id, from_ms / 1000, to_ms / 1000)?;

        // A window the watch staged but whose level this build does not know is
        // dropped rather than guessed at: the level field is five bits wide and
        // only four values have ever been seen.
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

        // The watch's own staging, and nothing else. A night it did not stage
        // has no sleep period: inferring one from heart rate scored lying awake
        // and still as sleep, which is worse than saying nothing.
        let asleep = levels
            .iter()
            .filter(|(_, _, stage)| *stage != SleepStage::Awake)
            .fold(None, |span: Option<(i64, i64)>, (from, to, _)| {
                Some(match span {
                    None => (*from, *to),
                    Some((lo, hi)) => (lo.min(*from), hi.max(*to)),
                })
            });

        // Getting up switches the watch from writing staged records to writing
        // activity ones, so a window inside the night carrying steps and no
        // level is time out of bed — not time the watch failed to measure. Left
        // as a hole it reads as missing data, which is what it is not.
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

        let charging = store
            .charge_periods(self.device_id, from_ms, to_ms)?
            .into_iter()
            .flat_map(|(start, end)| {
                [Some(start), end]
                    .into_iter()
                    .flatten()
                    .zip([SetEdge::Start, SetEdge::End])
                    .map(|(at_ms, edge)| Marker { at_ms, edge })
                    .collect::<Vec<Marker>>()
            })
            .collect();

        // Scored on the bands as drawn, gap-fills included: time out of bed is
        // time not asleep, and leaving it out would score a broken night as if
        // it had been unbroken.
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
            lowest_hr: hr
                .iter()
                .filter(|p| {
                    asleep
                        .map(|(from, to)| p.at_ms >= from && p.at_ms <= to)
                        .unwrap_or(false)
                })
                .map(|p| p.value)
                .fold(None, |low: Option<f64>, v| {
                    Some(low.map_or(v, |l| l.min(v)))
                }),
            hr,
            stages,
            charging,
        })
    }

    /// Charging periods over a window, as the same start/end markers a chart
    /// shades. An open period runs to the end of the window.
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

    /// A whole recording in one call; crossing per-sample would be absurd at
    /// 300 Hz across two leads.
    /// Every recording held, newest first.
    /// The waveform of the measurement in progress, oldest sample first.
    ///
    /// Live samples are never stored on the watch, so this is the only place
    /// they exist until the recording finishes and transfers.
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
                // Two bytes a sample, shared out between the leads.
                let per_lead = bytes / 2 / leads.max(1) as i64;
                EcgSummary {
                    id,
                    measured_at_ms: measured_at,
                    seconds: per_lead as f64 / (hz.max(1) as f64),
                    leads: leads as u32,
                    heart_rate: rate_of(&store, id, hz as u16, leads),
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
        Ok(Some(EcgRecording {
            id,
            measured_at_ms: measured_at,
            sampling_hz: hz as u32,
            leads,
        }))
    }
}

/// Rate read out of a recording's own waveform.
///
/// Measured on the filtered channel where there is one: the raw lead carries
/// baseline wander that the detector would count as beats.
fn rate_of(store: &wpp_store::Store, id: i64, hz: u16, leads: usize) -> Option<u32> {
    let (_, signal_type, _, _, samples) = store.ecg(id).ok().flatten()?;
    let names: Vec<&'static str> = wpp::signal::SignalKind::from_type_id(signal_type as u16)
        .map(|k| k.leads().iter().map(|l| l.name()).collect())
        .unwrap_or_default();
    let channel = names
        .iter()
        .position(|n| n.ends_with("FILTERED"))
        .unwrap_or(0);
    let lane: Vec<i16> = samples
        .chunks_exact(2)
        .skip(channel)
        .step_by(leads.max(1))
        .map(|p| i16::from_le_bytes([p[0], p[1]]))
        .collect();
    wpp::analysis::detect_r_peaks(&lane, hz)
        .heart_rate()
        .map(|bpm| bpm.0 as u32)
}

/// `BatteryStatus.battery_state` when a charger is attached.
const CHARGING_STATE: i64 = 0;

/// How recent a state reading has to be to describe the present. The watch is
/// polled every few minutes in the background, and bringing the app to the
/// front forces one, so anything older than this means nobody has looked.
const CHARGE_STATE_FRESH_MS: i64 = 180_000;

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
/// The set the watch had enabled when it was first read, including the four
/// ids this APK version has no constant for.
///
/// There is no read side: the message carries the whole enabled set, so an id
/// left out is switched off. Omitting 100 and 105 — which nothing in the app
/// names, and which no user-facing setting corresponds to — coincided with the
/// activity stream going silent, so they are carried whether or not we can say
/// what they do.
const DEFAULT_FEATURES: &[u16] = &[
    3, 5, 9, 10, 11, 14, 17, 19, 20, 27, 53, 71, 88, 100, 105, 113,
];

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

    fn write(&self, frame: &Frame) {
        self.transport.write(frame.to_bytes());
    }

    /// Answer the two requests the watch makes for pictures.
    ///
    /// Neither goes through [`Client`]: they need a font and a canvas, and the
    /// reply depends on nothing the sync state machine knows.
    fn draw(&self, frame: &Frame) {
        let glyph = GlyphRequest::parse(frame);
        let icon = IconRequest::parse(frame);
        if glyph.is_none() && icon.is_none() {
            return;
        }
        // Copied out rather than held: rasterising calls back into the host.
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
