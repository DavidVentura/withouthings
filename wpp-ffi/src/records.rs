use wpp::activity;
use wpp::ancs;
use wpp::client::{Phase, SampleKind, Source};
use wpp::image::Mono;
use wpp::sport::{self, SportId};
use wpp::units::UnixMillis;
use wpp_store::report;

pub(crate) fn origin_of(source: Source) -> Origin {
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
    #[error("not ready: {reason}")]
    NotReady { reason: String },
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
}

impl Metric {
    pub(crate) fn kind(self) -> SampleKind {
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
        }
    }

    pub(crate) fn reading(self, stored: i64) -> f64 {
        self.kind().reading(stored)
    }
}

#[derive(uniffi::Enum, Debug, Clone, Copy, PartialEq, Eq)]
pub enum GpsStatus {
    Absent,
    Present,
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
    pub(crate) fn of(phase: Phase) -> Progress {
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
    pub(crate) fn wire(self) -> u8 {
        match self {
            WearPosition::NotSet => 0,
            WearPosition::Hip => 1,
            WearPosition::LeftWrist => 2,
            WearPosition::RightWrist => 3,
        }
    }

    pub(crate) fn of(value: u8) -> Option<WearPosition> {
        match value {
            0 => Some(WearPosition::NotSet),
            1 => Some(WearPosition::Hip),
            2 => Some(WearPosition::LeftWrist),
            3 => Some(WearPosition::RightWrist),
            _ => None,
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
    pub(crate) fn of(level: activity::SleepLevel) -> SleepStage {
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
    pub(crate) fn pack(self) -> Result<Mono, WatchError> {
        if self.pixels.len() != self.width as usize * self.height as usize {
            return Err(WatchError::Refused {
                reason: format!(
                    "a {}x{} bitmap cannot hold {} pixels",
                    self.width,
                    self.height,
                    self.pixels.len()
                ),
            });
        }
        Ok(Mono::pack(&self.pixels, self.width, self.height))
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

pub(crate) fn ppg_rhythm(code: i64) -> Option<PpgRhythm> {
    let rhythm = wpp::signal::PpgRhythm::of(i32::try_from(code).ok()?)?;
    Some(match rhythm {
        wpp::signal::PpgRhythm::Undefined => PpgRhythm::Undefined,
        wpp::signal::PpgRhythm::SinusRhythm => PpgRhythm::SinusRhythm,
        wpp::signal::PpgRhythm::Afib => PpgRhythm::Afib,
        wpp::signal::PpgRhythm::Other => PpgRhythm::Other,
        wpp::signal::PpgRhythm::Noise => PpgRhythm::Noise,
    })
}

pub(crate) fn ecg_summary(summary: &report::EcgSummary) -> EcgSummary {
    EcgSummary {
        id: summary.id.0,
        measured_at_ms: summary.measured_at.0,
        seconds: summary.seconds,
        leads: summary.leads,
        heart_rate: summary.verdict.rate.map(|bpm| u32::from(bpm.0)),
        rhythm: summary.verdict.rhythm.map(ecg_rhythm),
    }
}

pub(crate) fn afib_reading(reading: report::AfibReading) -> AfibReading {
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

pub(crate) fn rhythm_analysis(rhythm: &wpp::rhythm::Rhythm) -> RhythmAnalysis {
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

pub(crate) fn ecg_analysis(reading: &wpp::rhythm::EcgReading) -> EcgAnalysis {
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

pub(crate) fn ecg_rhythm(rhythm: wpp::signal::Rhythm) -> EcgRhythm {
    match rhythm {
        wpp::signal::Rhythm::NoAfib => EcgRhythm::NoAfib,
        wpp::signal::Rhythm::Afib => EcgRhythm::Afib,
        wpp::signal::Rhythm::Inconclusive => EcgRhythm::Inconclusive,
        wpp::signal::Rhythm::PoorRecording => EcgRhythm::PoorRecording,
        wpp::signal::Rhythm::RateOutOfRange => EcgRhythm::RateOutOfRange,
        wpp::signal::Rhythm::NoResult => EcgRhythm::NoResult,
    }
}

pub(crate) fn travel(id: SportId) -> Travel {
    match sport::limits(id) {
        Some(_) => Travel::Ground,
        None => Travel::InPlace,
    }
}

pub(crate) fn workout_summary(workout: report::Workout) -> WorkoutSummary {
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

pub(crate) fn sleep_score(score: wpp::sleep::Score) -> SleepScore {
    SleepScore {
        total: score.total,
        duration: score.duration,
        efficiency: score.efficiency,
        deep: score.deep,
        rem: score.rem,
        continuity: score.continuity,
    }
}

pub(crate) fn night_record(night: wpp::sleep::Night) -> Night {
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

impl LocationFix {
    pub(crate) fn held(&self) -> wpp::track::Fix {
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
