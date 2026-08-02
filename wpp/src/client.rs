//! The sync conversation, as a pure state machine.
//!
//! [`Client::handle`] takes an [`Event`] and returns [`Action`]s. It performs
//! no I/O and holds no clock, so the same code drives a live BLE link and a
//! replayed capture, and the tests below are the protocol spec.
//!
//! Deleting a stored measurement is irreversible on the watch, so
//! [`Action::Delete`] is only ever produced after the host reports the data
//! durable via [`Event::Stored`].

use crate::activity::Minute;
use crate::debug_dump::DebugDump;
use crate::frame::Channel;
use crate::objects::{
    AncsStatus, AppProbe, AppProbeOsVersion, FeatureTagsDeprecated, Id, InfoType, MeasureCategory,
    MeasureLiveAppStatus, NotificationsDisplayState, Null, ProbeChallenge, ProbeChallengeResponse,
    StoredSignalMeta, TimeSet, TrackerWearPos, VasistasType, Version, WamScreensList,
    WamVasistasGet, WorkoutScreenList,
};
use crate::signal::{Signal, SignalCollector};
use crate::units::{UnixMillis, UnixTime};
use crate::{Command, Frame, WppObject};

/// `TYPE_CMDERROR_ERR_DEVBUSY`: the watch is busy, not refusing.
const ERR_DEVBUSY: i32 = -2;

/// Does this error frame refuse the probe, rather than something asked
/// alongside it? `Cmderror.cmd` names the command that was rejected.
fn rejects_probe(frame: &Frame) -> bool {
    frame.objects.iter().any(|o| {
        matches!(o, WppObject::Cmderror(e)
            if e.cmd == Command::CMD_PROBE.0 || e.cmd == Command::CMD_PROBE_CHALLENGE.0)
    })
}
const MAX_BUSY_RETRIES: u32 = 20;
/// The watch asks for a sync every few seconds, and each one ends with a
/// refresh. The daily totals do not change fast enough to be worth a request
/// at that rate.
const MIN_REFRESH_INTERVAL_MS: i64 = 300_000;
/// The watch asks for a sync every few seconds. Walking every stream that
/// often keeps its radio busy for no gain — it buffers, so a later walk
/// collects exactly the same records in one pass.
const MIN_WALK_INTERVAL_MS: i64 = 900_000;
/// A drain empties the watch's buffer, so a second one straight after has
/// nothing to collect. The watch asks for a sync once a minute regardless, and
/// answering every one of those with a transfer would cost more than the
/// asking does.
const MIN_DUMP_INTERVAL_MS: i64 = 900_000;
/// Silence after we have asked for something. A reply can be slow; this long
/// after being asked, the watch is not going to answer.
const SILENCE_TIMEOUT_MS: i64 = 90_000;
/// `WAM_SCREEN_MAX_NUMBER`: the screen list is this many slots, zero-padded.
const SCREEN_SLOTS: usize = 24;
/// The quick-launch menu holds this many activities, zero-padded.
const ACTIVITY_SLOTS: usize = 8;

/// Shared secret established at association, and the watch's address.
///
/// The secret is the 32-character key sent as `AccountKey`/`AdvKey` by
/// `CMD_ASSOCIATION_KEYS_SET`.
#[derive(Debug, Clone)]
pub struct Credentials {
    pub mac: String,
    pub secret: String,
}

impl Credentials {
    /// `SHA1(challenge || mac_ascii || secret_ascii)`, as the app computes it.
    pub fn answer(&self, challenge: &[u8]) -> Vec<u8> {
        let mut input = Vec::with_capacity(challenge.len() + self.mac.len() + self.secret.len());
        input.extend_from_slice(challenge);
        input.extend_from_slice(self.mac.as_bytes());
        input.extend_from_slice(self.secret.as_bytes());
        sha1(&input).to_vec()
    }
}

/// The next change of local offset from UTC. The watch is told about it so its
/// clock follows a daylight-saving change with nothing connected to it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DstChange {
    pub at: UnixTime,
    pub gmt_offset: i32,
}

/// The watch's side of phone notifications, as it reports it.
///
/// Two independent switches come back from one request: whether the watch will
/// talk to the phone's ANCS server, and whether it puts what it hears on the
/// screen.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct NotificationConfig {
    /// `AncsStatus.status`: the watch will act as a notification client.
    pub accepted: bool,
    /// `NotificationsDisplayState.status`: it will show them.
    pub displayed: bool,
}

/// Which historical series to walk. The watch keeps them separately and each
/// needs its own watermark.
///
/// Category 0 is the body stream, fetched with `CMD_BODY_VASISTAS_GET` and no
/// type selector; it is the one carrying heart rate. Every other value is a
/// `VasistasType` fetched with `CMD_VASISTAS_GET`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Category(pub u8);

impl Category {
    pub const BODY: Category = Category(0);
    /// The per-minute activity stream, fetched with `CMD_WAM_VASISTAS_GET`,
    /// which like the body stream takes no type selector. Numbered outside the
    /// range the watch uses for `VasistasType` so that it can keep a watermark
    /// of its own without colliding with a typed one.
    pub const ACTIVITY: Category = Category(255);
}

#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    /// The link is up and notifications are subscribed.
    Connected,
    /// The link is gone. Everything scoped to it has to be let go here, or it
    /// outlives the connection it described and poisons the next one.
    Disconnected,
    /// A decoded frame and when the host received it. Live pushes carry no
    /// timestamp of their own, so this is the only time they get.
    Frame {
        frame: Frame,
        received_at: UnixMillis,
    },
    /// Everything handed over by [`Action::Store`] up to `token` is durable.
    Stored { token: u64 },
    /// The host's clock, delivered on a timer whether or not anything arrived.
    ///
    /// Without it the client's only sense of time comes from inbound frames,
    /// so it cannot tell a quiet watch from a stopped one, and every interval
    /// it enforces freezes exactly when the link goes wrong.
    Tick { now: UnixMillis },
}

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Send(Frame),
    /// Persist these records, then report back with [`Event::Stored`].
    Store {
        token: u64,
        records: Vec<Record>,
    },
    /// Safe to remove from the watch: the data behind it is committed.
    Delete(Frame),
    /// The sync finished; the link can be dropped.
    Finished,
    /// The link is up but the watch has stopped answering. Tear it down and
    /// start again; nothing else will resolve it.
    Reconnect,
}

/// What a sync produced, in wire units.
#[derive(Debug, Clone, PartialEq)]
pub enum Record {
    Sample {
        measured_at: UnixMillis,
        kind: SampleKind,
        value: i64,
        quality: Option<i64>,
        source: Source,
    },
    WorkoutStarted {
        started_at: UnixTime,
        subcategory: i16,
    },
    WorkoutEnded {
        started_at: UnixTime,
        ended_at: UnixTime,
        paused_secs: i64,
    },
    /// One window of the per-minute activity stream. Kept whole rather than
    /// split into samples: the counters describe a window, not an instant, and
    /// the session detection reads them together.
    Activity(Minute),
    Ecg(Box<Signal>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleKind {
    HeartRate,
    CoreTemperature,
    HrvSdnn,
    HrvRmssd,
    RespiratoryRate,
    BatteryPercent,
    Steps,
    /// `BatteryStatus.battery_state`: 0 charging, 1 low, 2 ok, 3 critical.
    /// Stored as its own series because a charge is only visible as a change
    /// in it, and nothing on the wire announces one.
    BatteryState,
    /// Cell voltage. It moves as soon as a charger is attached, well before
    /// the percentage the gauge reports catches up.
    BatteryMillivolts,
    /// Unused: the watch serves staging on the activity stream, one level per
    /// window, so it lives in `activity_minute.sleep_level` where the window's
    /// duration is kept with it. `sample_kind` row 10 is left in place because
    /// dropping it would need a migration for nothing.
    SleepLevel,
    /// Blood oxygen, with `VasistasSpo2.quality` alongside it. Carried by the
    /// bulk streams the walk reads last.
    Spo2,
    /// Climb, centimetres on the wire despite the object being called
    /// `Stairs` — it matches the activity stream's `ascent` exactly, summed
    /// over the day, and is a height rather than a count of floors.
    Ascent,
    /// The rest of the day's running totals, in the same frame as the steps.
    /// Calories are hundredths of a kilocalorie and cover resting as well as
    /// earned; distance is centimetres; the duration is plain seconds and runs
    /// from local midnight.
    Calories,
    Distance,
    TrackedDuration,
}

impl SampleKind {
    pub fn id(self) -> i64 {
        match self {
            SampleKind::HeartRate => 1,
            SampleKind::CoreTemperature => 2,
            SampleKind::HrvSdnn => 3,
            SampleKind::HrvRmssd => 4,
            SampleKind::RespiratoryRate => 5,
            SampleKind::BatteryPercent => 6,
            SampleKind::Steps => 7,
            SampleKind::BatteryState => 8,
            SampleKind::BatteryMillivolts => 9,
            SampleKind::SleepLevel => 10,
            SampleKind::Spo2 => 11,
            SampleKind::Ascent => 12,
            SampleKind::Calories => 13,
            SampleKind::Distance => 14,
            SampleKind::TrackedDuration => 15,
        }
    }
}

/// Which ingest path a sample arrived by; the two differ in resolution and
/// must not overwrite each other.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// Historical series pulled during a sync.
    Stored,
    /// 1 Hz notifications during a live workout or measurement.
    Live,
}

impl Source {
    pub fn id(self) -> i64 {
        match self {
            Source::Stored => 0,
            Source::Live => 1,
        }
    }
}

/// Where the conversation has got to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Idle,
    /// Probe sent, waiting for the watch's challenge.
    Probing,
    /// Our answer sent, waiting for the watch to accept it.
    Authenticating,
    /// Walking one category's history forward.
    Syncing,
    Finished,
    /// The watch refused the challenge answer. The association secret is wrong
    /// or no longer the one the watch holds.
    NotAuthenticated,
}

pub struct Client {
    credentials: Credentials,
    phase: Phase,
    /// Categories still to walk this pass, and where each has been read up to.
    queue: Vec<(Category, UnixTime)>,
    current: Option<(Category, UnixTime)>,
    /// Streams finished this pass, kept so their watermarks survive and the
    /// walk can be run again without being reconstructed from storage.
    done: Vec<(Category, UnixTime)>,
    /// Timestamp of the newest record seen in the batch being collected.
    /// Newest record seen in the batch being collected, for the stream being
    /// walked. Held per stream: compared by `max` across streams, one that is
    /// up to date drags a stream that is behind past everything between them.
    batch_high_water: Option<(Category, UnixTime)>,
    signals: SignalCollector,
    next_token: u64,
    /// Deletes held back until the matching Store is confirmed durable.
    pending_deletes: Vec<(u64, Frame)>,
    /// Watermark the current category started from, for progress reporting.
    walk_started_from: Option<UnixTime>,
    busy_retries: u32,
    stream_total: u32,
    /// Screens the watch reports it is showing, in its own order.
    screens: Option<Vec<u8>>,
    /// Activities in the watch's quick-launch menu, in order.
    activities: Option<Vec<u32>>,
    /// Where the watch says it is worn.
    wear_position: Option<u8>,
    /// Whether the watch says it will accept phone notifications, and whether
    /// it will show them. Both come back from one request.
    notifications: Option<NotificationConfig>,
    /// The image sizes the watch declares it can hold, by type. Empty until
    /// it has said, which it does with the workout screen list.
    image_formats: Vec<crate::image::ImageFormat>,
    records_emitted: u64,
    /// Object types that decoded but no collector consumed, as
    /// (command, type id, type name), first sighting only. Reported so a stream
    /// the watch serves cannot go unnoticed the way its sleep staging did.
    unhandled: Vec<(u16, u16, &'static str)>,
    app_probe: AppProbe,
    /// Latest frame timestamp seen. The client holds no clock of its own, so
    /// this is the only sense of "now" it has.
    now: Option<UnixMillis>,
    /// When the watch was last heard from, and when we last asked it for
    /// something. Silence only means anything once we have spoken into it.
    last_heard: Option<UnixMillis>,
    last_spoke: Option<UnixMillis>,
    /// The measurement the watch is taking, if any, by category.
    measuring: Option<i16>,
    /// Live waveform for the measurement in progress, in wire counts.
    live_samples: Vec<i16>,
    last_refresh: Option<UnixMillis>,
    last_walk: Option<UnixMillis>,
    /// The watch's diagnostic buffer, and when it was last emptied.
    dump: DebugDump,
    last_dump: Option<UnixMillis>,
    /// Whether the watch should accept phone notifications, if the host has
    /// said. `None` leaves whatever the watch already holds.
    wanted_notifications: Option<bool>,
}

impl Client {
    pub fn new(credentials: Credentials, watermarks: Vec<(Category, UnixTime)>) -> Client {
        let stream_total = watermarks.len() as u32;
        Client {
            credentials,
            phase: Phase::Idle,
            queue: watermarks,
            current: None,
            done: Vec::new(),
            batch_high_water: None,
            signals: SignalCollector::new(),
            next_token: 1,
            pending_deletes: Vec::new(),
            walk_started_from: None,
            busy_retries: 0,
            stream_total,
            screens: None,
            activities: None,
            wear_position: None,
            notifications: None,
            image_formats: Vec::new(),
            records_emitted: 0,
            unhandled: Vec::new(),
            now: None,
            last_heard: None,
            last_spoke: None,
            measuring: None,
            live_samples: Vec::new(),
            last_refresh: None,
            last_walk: None,
            dump: DebugDump::new(),
            last_dump: None,
            wanted_notifications: None,
            app_probe: AppProbe {
                os: 1,
                app: 1,
                version: 8070101,
            },
        }
    }

    /// Where each category has been read up to. Persist this only after the
    /// corresponding records are durable, so a crash re-reads rather than skips.
    pub fn watermarks(&self) -> Vec<(Category, UnixTime)> {
        let mut all = self.queue.clone();
        all.extend(self.done.iter().copied());
        if let Some(current) = self.current {
            all.push(current);
        }
        all.sort_by_key(|(c, _)| *c);
        all
    }

    /// Walk every stream again from where it left off.
    ///
    /// A finished sync is only current as of the moment it finished; the watch
    /// keeps sampling, so the walk has to be repeated to stay up to date.
    pub fn sync_now(&mut self) -> Vec<Action> {
        if let (Some(now), Some(last)) = (self.now, self.last_walk) {
            if now.0 - last.0 < MIN_WALK_INTERVAL_MS {
                return Vec::new();
            }
        }
        let actions = self.walk_now();
        self.noted(actions)
    }

    /// Take the watch's diagnostic buffer, if this is the moment for it.
    ///
    /// Only the transfer lets the watch drop what it is holding, and until it
    /// does it goes on asking to sync every minute. The walk and a measurement
    /// each own the link while they run, so a drain waits for them.
    fn drain_dump(&mut self) -> Vec<Action> {
        if self.phase != Phase::Finished || self.measuring.is_some() || self.dump.running() {
            return Vec::new();
        }
        if let (Some(now), Some(last)) = (self.now, self.last_dump) {
            if now.0 - last.0 < MIN_DUMP_INTERVAL_MS {
                return Vec::new();
            }
        }
        self.last_dump = self.now;
        self.dump.start().into_iter().map(Action::Send).collect()
    }

    /// The same walk, ignoring the rate limit.
    pub fn walk_now(&mut self) -> Vec<Action> {
        // A measurement needs the link. Walking the history through one makes
        // the watch abandon the recording and start again — it asks for a sync
        // anyway, so the request has to be declined here.
        if self.measuring.is_some() {
            return Vec::new();
        }
        // The dump owns the link the same way, and it holds the watch's cursor
        // into a transfer that a walk started underneath it would abandon.
        if self.dump.running() {
            return Vec::new();
        }
        if self.phase != Phase::Finished && self.phase != Phase::Syncing {
            return Vec::new();
        }
        if self.current.is_some() {
            return Vec::new();
        }
        // Reversed so the queue pops in the same priority order as the first
        // pass, body stream first.
        self.queue
            .extend(std::mem::take(&mut self.done).into_iter().rev());
        if self.queue.is_empty() {
            return Vec::new();
        }
        self.phase = Phase::Syncing;
        self.last_walk = self.now;
        let actions = self.request_next();
        self.noted(actions)
    }

    /// The measurement category the watch is taking, if one is running.
    pub fn measuring(&self) -> Option<i16> {
        self.measuring
    }

    /// Live waveform accumulated for the measurement in progress.
    pub fn live_samples(&self) -> &[i16] {
        &self.live_samples
    }

    pub fn phase(&self) -> Phase {
        self.phase
    }

    /// The category being walked, and the point it has been read up to.
    pub fn current(&self) -> Option<(Category, UnixTime)> {
        self.current
    }

    /// Non-zero means data has been read from the watch but not yet committed.
    pub fn pending_deletes(&self) -> usize {
        self.pending_deletes.len()
    }

    /// How far the history walk has come: where it started and where it is.
    /// The caller supplies the end point, since the client holds no clock.
    pub fn walk_span(&self) -> Option<(UnixTime, UnixTime)> {
        Some((self.walk_started_from?, self.current?.1))
    }

    /// Bytes received and expected for a signal transfer in progress.
    pub fn transfer_progress(&self) -> Option<(usize, usize)> {
        self.signals.transfer_progress()
    }

    /// Records handed over for storage since this client was created.
    pub fn records_emitted(&self) -> u64 {
        self.records_emitted
    }

    /// Position in the walk, as (streams finished, streams total). A fraction
    /// within one stream restarts at every stream boundary, so on its own it
    /// looks like the sync keeps starting over.
    pub fn stream_position(&self) -> (u32, u32) {
        let remaining = self.queue.len() + usize::from(self.current.is_some());
        (
            (self.stream_total as usize - remaining) as u32,
            self.stream_total,
        )
    }

    pub fn handle(&mut self, event: Event) -> Vec<Action> {
        let actions = match event {
            Event::Connected => self.on_connected(),
            Event::Disconnected => self.on_disconnected(),
            Event::Frame { frame, received_at } => self.on_frame(frame, received_at),
            Event::Stored { token } => self.on_stored(token),
            Event::Tick { now } => self.on_tick(now),
        };
        self.noted(actions)
    }

    /// Record that a request went out, so silence afterwards can be judged.
    /// Every path that can produce a send has to pass through here — the sends
    /// that matter most are the timer-driven ones, which no event accompanies.
    fn noted(&mut self, actions: Vec<Action>) -> Vec<Action> {
        if actions.iter().any(|a| matches!(a, Action::Send(_))) {
            self.last_spoke = self.now;
        }
        actions
    }

    fn on_tick(&mut self, now: UnixMillis) -> Vec<Action> {
        self.now = Some(now);
        let Some(heard) = self.last_heard else {
            return Vec::new();
        };
        // Silence only means something while the watch owes an answer. Idle at
        // Phase::Finished it says nothing for minutes by design, and treating
        // that as a dead link would reconnect all day.
        //
        // A handshake or a walk in progress owes one continuously: every reply
        // is followed by another until the stream ends. That is why the wait is
        // measured from the last thing heard rather than from the request —
        // measuring from the request meant a walk that died after its first few
        // frames left `heard` ahead of `spoke`, which read as a healthy link
        // forever.
        let awaiting_reply = matches!(
            self.phase,
            Phase::Probing | Phase::Authenticating | Phase::Syncing
        ) || self.last_spoke.is_some_and(|spoke| spoke > heard);
        // From whichever came last. Timing from the request alone forgives a
        // walk that answered and then died; timing from the reply alone starts
        // the clock before the request that is being waited on was even sent.
        let quiet_since =
            self.last_spoke
                .map_or(heard, |spoke| if spoke.0 > heard.0 { spoke } else { heard });
        if awaiting_reply && now.0 - quiet_since.0 > SILENCE_TIMEOUT_MS {
            // Reconnecting re-runs the handshake, so the walk starts over from
            // its watermarks rather than waiting on a reply that never came.
            return vec![Action::Reconnect];
        }
        Vec::new()
    }

    /// Let go of everything that belonged to the link that just died.
    ///
    /// One place, so that state added later cannot quietly forget to be reset:
    /// both wedges found so far were per-connection state that outlived its
    /// connection because each new field had to opt in individually.
    fn on_disconnected(&mut self) -> Vec<Action> {
        self.phase = Phase::Idle;
        self.busy_retries = 0;
        self.last_heard = None;
        self.last_spoke = None;
        // A recording ends only with a stop from the watch, which cannot arrive
        // for a link that no longer exists. Left set, it refuses every request
        // this client is ever asked to make again.
        self.measuring = None;
        // Whatever stream was in flight was never answered. Putting it back
        // keeps the pass complete; dropping it left one stream unread until
        // some later pass happened to pick it up.
        if let Some(interrupted) = self.current.take() {
            self.queue.push(interrupted);
        }
        self.batch_high_water = None;
        self.walk_started_from = None;
        self.signals.reset();
        self.dump.reset();
        Vec::new()
    }

    fn on_connected(&mut self) -> Vec<Action> {
        // A link can come up without the old one reporting that it went down.
        self.on_disconnected();
        self.phase = Phase::Probing;
        self.last_heard = self.now;
        vec![Action::Send(Frame::new(
            Command::CMD_PROBE,
            vec![
                WppObject::AppProbe(self.app_probe.clone()),
                WppObject::AppProbeOsVersion(AppProbeOsVersion { os_version: 35 }),
            ],
        ))]
    }

    fn on_stored(&mut self, token: u64) -> Vec<Action> {
        let mut actions: Vec<Action> = Vec::new();
        self.pending_deletes.retain(|(pending, frame)| {
            if *pending <= token {
                actions.push(Action::Delete(frame.clone()));
                false
            } else {
                true
            }
        });
        actions
    }

    fn store(&mut self, records: Vec<Record>) -> Option<Action> {
        if records.is_empty() {
            return None;
        }
        let token = self.next_token;
        self.next_token += 1;
        self.records_emitted += records.len() as u64;
        Some(Action::Store { token, records })
    }

    fn on_frame(&mut self, frame: Frame, received_at: UnixMillis) -> Vec<Action> {
        let mut actions = Vec::new();
        let mut records = Vec::new();
        self.now = Some(received_at);
        self.last_heard = Some(received_at);

        // Decoding does not depend on who asked for the data; the phase below
        // only decides what to send next. This also lets a captured session be
        // replayed through the same code.
        self.collect_passive(&frame, received_at, &mut records);
        self.collect_history(&frame, &mut records);
        self.live_samples.extend(self.signals.take_live());
        actions.extend(self.dump.on_frame(&frame).into_iter().map(Action::Send));

        match frame.command.opcode() {
            // The watch asks whether anything is showing the waveform, and only
            // streams if told yes. Nothing else turns it on.
            c if c == Command::CMD_MEASURE_START.0 => {
                let category = frame
                    .objects
                    .iter()
                    .find_map(|o| match o {
                        WppObject::MeasureCategory(m) => Some(m.value),
                        _ => None,
                    })
                    .unwrap_or(0);
                self.measuring = Some(category);
                self.live_samples.clear();
                actions.push(Action::Send(Frame::new(
                    Command::CMD_MEASURE_START.with_channel(Channel::SlaveRequest),
                    vec![
                        WppObject::MeasureCategory(MeasureCategory { value: category }),
                        WppObject::MeasureLiveAppStatus(MeasureLiveAppStatus {
                            app_live_screen_displayed: 1,
                        }),
                    ],
                )));
            }
            // Answered here rather than with the walk below, because the walk
            // is rate limited and the phase decides whether it runs at all: an
            // unanswered request is repeated for as long as the link is up, and
            // the watch raises its connection rate while one is outstanding.
            // The acknowledgement is what ends it, not the sync it asks for.
            c if c == Command::CMD_SYNC_REQUEST.0 => {
                actions.push(Action::Send(Frame::new(
                    Command::CMD_SYNC_REQUEST.with_channel(Channel::SlaveRequest),
                    Vec::new(),
                )));
            }
            // The watch forgets this over a reboot and it is asked for on
            // every pass, so putting the host's choice back whenever the two
            // disagree is what makes it stick. Left to drift it is not a
            // cosmetic setting: with notifications on, the watch halves its
            // connection latency and never restores it.
            c if c == Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_GET.0 => {
                if let (Some(wanted), Some(config)) =
                    (self.wanted_notifications, self.notifications)
                {
                    if config.accepted != wanted {
                        actions.extend(self.set_notifications(wanted));
                    }
                }
            }
            c if c == Command::CMD_MEASURE_STOP.0 => {
                self.measuring = None;
                // The stop carries the identity of what was just recorded. The
                // watch repeats it until acknowledged, and the recording is
                // only transferred if it is then asked for by that identity.
                actions.push(Action::Send(Frame::new(
                    Command::CMD_MEASURE_STOP.with_channel(Channel::SlaveRequest),
                    vec![WppObject::Null(Null {})],
                )));
                if let Some(measure) = frame.objects.iter().find_map(|o| match o {
                    WppObject::StoredMeasureMeta(m) => Some(m.clone()),
                    _ => None,
                }) {
                    actions.push(Action::Send(Frame::new(
                        Command::CMD_STORED_MEASURE_SIGNAL_GET,
                        vec![WppObject::StoredMeasureMeta(measure)],
                    )));
                }
            }
            _ => {}
        }

        match (self.phase, frame.command.opcode()) {
            (Phase::Probing, c) if c == Command::CMD_PROBE_CHALLENGE.0 => {
                if let Some(challenge) = frame.objects.iter().find_map(|o| match o {
                    WppObject::ProbeChallenge(c) => Some(c.clone()),
                    _ => None,
                }) {
                    self.phase = Phase::Authenticating;
                    actions.push(Action::Send(Frame::new(
                        Command::CMD_PROBE_CHALLENGE,
                        vec![
                            WppObject::ProbeChallengeResponse(ProbeChallengeResponse {
                                answer: self.credentials.answer(&challenge.challenge),
                            }),
                            // Our own challenge; the watch answers it in kind.
                            WppObject::ProbeChallenge(ProbeChallenge {
                                mac: self.credentials.mac.clone(),
                                challenge: vec![0; 16],
                            }),
                        ],
                    )));
                }
            }
            // A rejected probe is terminal: without the right secret nothing
            // else will be answered, and waiting forever looks identical to a
            // watch that is merely slow. It has to be the probe that was
            // rejected, though — everything else asked before the handshake
            // finishes draws ERR_NOT_AUTH by design.
            (Phase::Probing | Phase::Authenticating, c)
                if c == Command::CMD_ERROR.0 && rejects_probe(&frame) =>
            {
                self.phase = Phase::NotAuthenticated;
            }
            // The watch does not always challenge. Reconnecting quickly, it
            // answers the probe outright with a `ProbeReply` and no
            // `ProbeChallenge`, having decided the association still stands.
            // Waiting for a challenge that is never coming leaves the client
            // probing forever while the watch, which considers the link up,
            // asks it to sync every two seconds and is ignored.
            (Phase::Probing | Phase::Authenticating, c) if c == Command::CMD_PROBE.0 => {
                self.phase = Phase::Syncing;
                // A workout that began while nothing was connected is only
                // discoverable by asking: CMD_WORKOUT_START is pushed once and
                // not replayed. The official app asks on every connect too.
                actions.push(Action::Send(Frame::new(
                    Command::CMD_WORKOUT_STATUS,
                    Vec::new(),
                )));
                actions.extend(self.request_next());
            }
            (Phase::Finished, c) if c == Command::CMD_SYNC_REQUEST.0 => {
                // The watch asks for a sync when it has data to hand over.
                // History first: the walk is what the request is usually about,
                // and it owns the link while it runs. The diagnostic buffer is
                // the other thing it can be about, and it is worth taking only
                // when the walk has nothing to do.
                let walk = self.sync_now();
                if walk.is_empty() {
                    actions.extend(self.drain_dump());
                } else {
                    actions.extend(walk);
                }
            }
            (Phase::Syncing, c) if c == Command::CMD_ERROR.0 => {
                let busy = frame
                    .objects
                    .iter()
                    .any(|o| matches!(o, WppObject::Cmderror(e) if e.err == ERR_DEVBUSY));
                // The watch rejects requests it is too busy to serve; the data
                // is still there, so ask again rather than skipping the window.
                if busy && self.busy_retries < MAX_BUSY_RETRIES {
                    self.busy_retries += 1;
                    actions.extend(self.request_current());
                }
            }
            (Phase::Syncing, _) => {
                self.busy_retries = 0;
                let empty = frame
                    .objects
                    .iter()
                    .any(|o| matches!(o, WppObject::Null(_)));
                if empty {
                    // Nothing left in this category; keep its watermark and
                    // move on.
                    if let Some(finished) = self.current.take() {
                        self.done.push(finished);
                    }
                    actions.extend(self.request_next());
                } else if let Some((seen, high)) = self.batch_high_water.take() {
                    if let Some((category, _)) = self.current {
                        // Resume one second past the newest record so the next
                        // request does not return it again — but only on the
                        // strength of this stream's own records.
                        if seen == category {
                            self.current = Some((category, UnixTime(high.0 + 1)));
                        }
                    }
                    actions.extend(self.request_current());
                }
            }
            _ => {}
        }

        for signal in self.take_signals() {
            let delete = delete_frame(&signal);
            records.push(Record::Ecg(Box::new(signal)));
            if let Some(action) = self.store(std::mem::take(&mut records)) {
                if let Action::Store { token, .. } = action {
                    self.pending_deletes.push((token, delete));
                }
                actions.push(action);
            }
        }
        if let Some(action) = self.store(records) {
            actions.push(action);
        }
        if self.phase == Phase::Finished {
            actions.push(Action::Finished);
        }
        actions
    }

    fn take_signals(&mut self) -> Vec<Signal> {
        self.signals
            .take_completed()
            .into_iter()
            .filter(|s| s.is_complete())
            .collect()
    }

    fn collect_passive(
        &mut self,
        frame: &Frame,
        received_at: UnixMillis,
        records: &mut Vec<Record>,
    ) {
        // Only meaningful on the workout screen list reply; the same object
        // rides in every request the watch makes for a picture.
        let declared = crate::image::ImageFormat::declared(frame);
        if !declared.is_empty() {
            self.image_formats = declared;
        }

        for object in &frame.objects {
            self.signals.observe(object);
            match object {
                WppObject::WorkoutScreenList(list) => {
                    self.activities = Some(
                        list.screen_nb
                            .iter()
                            .copied()
                            .filter(|id| *id != 0)
                            .collect(),
                    );
                }
                WppObject::TrackerWearPos(pos) => {
                    self.wear_position = Some(pos.value);
                }
                WppObject::AncsStatus(status) => {
                    let config = self.notifications.get_or_insert_with(Default::default);
                    config.accepted = status.status != 0;
                }
                WppObject::NotificationsDisplayState(state) => {
                    let config = self.notifications.get_or_insert_with(Default::default);
                    config.displayed = state.status == NotificationsDisplayState::STATUS_ENABLED;
                }
                WppObject::WamScreensList(list) => {
                    // Fixed 24 slots, zero-padded; 0 means an empty slot.
                    self.screens = Some(
                        list.screen_numbers
                            .iter()
                            .copied()
                            .filter(|id| *id != 0)
                            .collect(),
                    );
                }
                // The watch reports running totals for the day. Stamping one
                // with the day would keep a single row that only ever shows the
                // latest figure; stamping it with the observation keeps the
                // accumulation through the day. All four arrive together.
                WppObject::Steps(steps) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::Steps,
                        value: steps.value as i64,
                        quality: None,
                        source: Source::Live,
                    });
                }
                WppObject::Stairs(stairs) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::Ascent,
                        value: stairs.value as i64,
                        quality: None,
                        source: Source::Live,
                    });
                }
                WppObject::Calories(calories) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::Calories,
                        value: calories.value as i64,
                        quality: None,
                        source: Source::Live,
                    });
                }
                WppObject::Distance(distance) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::Distance,
                        value: distance.value as i64,
                        quality: None,
                        source: Source::Live,
                    });
                }
                WppObject::Duration(duration) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::TrackedDuration,
                        value: duration.value as i64,
                        quality: None,
                        source: Source::Live,
                    });
                }
                WppObject::BatteryStatus(battery) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::BatteryPercent,
                        value: battery.battery_percent as i64,
                        quality: None,
                        source: Source::Live,
                    });
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::BatteryState,
                        value: battery.battery_state as i64,
                        quality: None,
                        source: Source::Live,
                    });
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::BatteryMillivolts,
                        value: battery.battery_mv as i64,
                        quality: None,
                        source: Source::Live,
                    });
                }
                WppObject::LiveHr(live) if live.hr > 0 => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::HeartRate,
                        value: live.hr as i64,
                        quality: None,
                        source: Source::Live,
                    });
                }
                WppObject::StartTime(start)
                    if frame.command.opcode() == Command::CMD_WORKOUT_STATUS.0
                        && frame
                            .objects
                            .iter()
                            .any(|o| matches!(o, WppObject::Status(s) if s.value == 1)) =>
                {
                    let subcategory = frame
                        .objects
                        .iter()
                        .find_map(|o| match o {
                            WppObject::ActivitySubcategory(a) => Some(a.value),
                            _ => None,
                        })
                        .unwrap_or(0);
                    records.push(Record::WorkoutStarted {
                        started_at: UnixTime(start.value as i64),
                        subcategory,
                    });
                }
                WppObject::StartTime(start)
                    if frame.command.opcode() == Command::CMD_WORKOUT_START.0 =>
                {
                    let subcategory = frame
                        .objects
                        .iter()
                        .find_map(|o| match o {
                            WppObject::ActivitySubcategory(a) => Some(a.value),
                            _ => None,
                        })
                        .unwrap_or(0);
                    records.push(Record::WorkoutStarted {
                        started_at: UnixTime(start.value as i64),
                        subcategory,
                    });
                }
                WppObject::EndTime(end) => {
                    let started = frame.objects.iter().find_map(|o| match o {
                        WppObject::StartTime(s) => Some(s.value),
                        _ => None,
                    });
                    let paused = frame
                        .objects
                        .iter()
                        .find_map(|o| match o {
                            WppObject::PauseState(p) => Some(p.sum as i64),
                            _ => None,
                        })
                        .unwrap_or(0);
                    if let Some(started) = started {
                        records.push(Record::WorkoutEnded {
                            started_at: UnixTime(started as i64),
                            ended_at: UnixTime(end.value as i64),
                            paused_secs: paused,
                        });
                    }
                }
                _ => {}
            }
        }
    }

    /// Historical samples are grouped: a `WamVasistasHead` timestamp followed
    /// by the values measured in that window.
    fn collect_history(&mut self, frame: &Frame, records: &mut Vec<Record>) {
        // Only a vasistas reply is history. The daily totals carry a
        // `WamVasistasHead` too — local midnight, the day they belong to — and
        // letting that reach the watermark drags the walk back to the start of
        // the day, so every refresh re-reads everything since.
        let opcode = frame.command.opcode();
        if opcode == Command::CMD_WAM_VASISTAS_GET.0 {
            return self.collect_activity(frame, records);
        }
        if opcode != Command::CMD_VASISTAS_GET.0 && opcode != Command::CMD_BODY_VASISTAS_GET.0 {
            return;
        }
        let mut at: Option<UnixTime> = None;
        for object in &frame.objects {
            match object {
                WppObject::WamVasistasHead(head) => {
                    let time = UnixTime(head.utc as i64);
                    at = Some(time);
                    self.note_head(time);
                }
                WppObject::VasistasHeartrate(hr) if hr.heartrate > 0 => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::HeartRate,
                            value: hr.heartrate as i64,
                            quality: Some(hr.quality as i64),
                            source: Source::Stored,
                        });
                    }
                }
                WppObject::VasistasCbt(cbt) => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::CoreTemperature,
                            value: cbt.temperature as i64,
                            quality: None,
                            source: Source::Stored,
                        });
                    }
                }
                // `error` is non-zero on readings the watch could not resolve,
                // which are the majority of what the bulk streams carry.
                WppObject::VasistasSpo2(spo2) if spo2.error == 0 && spo2.spo2 > 0 => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::Spo2,
                            value: spo2.spo2 as i64,
                            quality: Some(spo2.quality as i64),
                            source: Source::Stored,
                        });
                    }
                }
                WppObject::VasistasHrv(hrv) => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::HrvSdnn,
                            value: hrv.sdnn as i64,
                            quality: Some(hrv.quality as i64),
                            source: Source::Stored,
                        });
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::HrvRmssd,
                            value: hrv.rmssd as i64,
                            quality: Some(hrv.quality as i64),
                            source: Source::Stored,
                        });
                    }
                }
                WppObject::VasistasRr(rr) if rr.rr > 0 => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::RespiratoryRate,
                            value: rr.rr as i64,
                            quality: None,
                            source: Source::Stored,
                        });
                    }
                }
                // Zero is the watch saying it did not measure, not a reading of
                // nothing; the arms above take everything else.
                WppObject::VasistasHeartrate(_) | WppObject::VasistasRr(_) => {}
                // The request echoed back around the records, and the empty
                // reply that closes a category.
                WppObject::WamVasistasGet(_) | WppObject::VasistasType(_) | WppObject::Null(_) => {}
                // Which measurements the body stream is set to carry and which
                // it could. Capability bits rather than a reading, and the
                // meaning of the individual bits is unknown, so there is
                // nothing to store that could later be read back honestly.
                WppObject::VasistasFlags(_) => {}
                other => self.note_unhandled(frame, other),
            }
        }
    }

    /// The activity stream, which unlike the sample streams puts several
    /// counters under one head and has to be collected a window at a time.
    fn collect_activity(&mut self, frame: &Frame, records: &mut Vec<Record>) {
        let mut open: Option<Minute> = None;
        for object in &frame.objects {
            if let WppObject::WamVasistasHead(head) = object {
                let at = UnixTime(head.utc as i64);
                self.note_head(at);
                records.extend(open.replace(Minute::opened(at)).map(Record::Activity));
                continue;
            }
            let Some(minute) = open.as_mut() else {
                continue;
            };
            match object {
                // A window without one claims no span, and the detection
                // then ignores it rather than guessing a minute.
                WppObject::WamVasistasDuration(d) => minute.duration_secs = d.duration as i64,
                WppObject::WamVasistasAwake(a) => {
                    minute.steps = Some(a.steps as i64);
                    minute.distance = Some(a.distance as i64);
                    minute.ascent = Some(a.ascent as i64);
                    minute.descent = Some(a.descent as i64);
                }
                WppObject::WamVasistasMetCalEarned(m) => {
                    minute.calories = Some(m.calories as i64);
                    minute.met = Some(m.met as i64);
                }
                WppObject::WamVasistasWalk(w) => minute.walk_level = Some(w.level as i64),
                WppObject::WamVasistasRun(r) => minute.run_level = Some(r.level as i64),
                // Useless without the official app's classifier, and gone
                // from the watch's buffer within the day.
                WppObject::VasistasActiRecoV1V2(r) => {
                    minute.reco_v1 = Some(r.reco_v1 as i64);
                    minute.reco_v2 = Some(r.reco_v2 as i64);
                }
                // A window is staged or it is awake; the watch sends one body
                // or the other under the same head.
                WppObject::WamVasistasSleep(s) => minute.sleep_level = Some(s.level as i64),
                // The request echoed back around the records, and the empty
                // reply that closes a walk.
                WppObject::WamVasistasGet(_) | WppObject::VasistasType(_) | WppObject::Null(_) => {}
                other => self.note_unhandled(frame, other),
            }
        }
        records.extend(open.map(Record::Activity));
    }

    /// An object that decoded but no collector consumed.
    ///
    /// A silent catch-all here cost ten nights of staging: the watch had been
    /// sending `WamVasistasSleep` on the activity stream all along and the arm
    /// that stored it sat in the branch for a different command. Anything this
    /// records is either a type worth handling or one worth naming as ignored.
    fn note_unhandled(&mut self, frame: &Frame, object: &WppObject) {
        let seen = (frame.command.opcode(), object.type_id(), object.type_name());
        if !self.unhandled.contains(&seen) {
            self.unhandled.push(seen);
        }
    }

    fn note_head(&mut self, at: UnixTime) {
        let Some((category, _)) = self.current else {
            return;
        };
        self.batch_high_water = Some(match self.batch_high_water {
            Some((seen, high)) if seen == category => (category, UnixTime(high.0.max(at.0))),
            _ => (category, at),
        });
    }

    /// Object types the watch sent that nothing consumed, as
    /// (command, type id, type name), and forget them.
    ///
    /// Empty is the expected answer. Anything here is data the watch is serving
    /// and the client is discarding.
    pub fn take_unhandled(&mut self) -> Vec<(u16, u16, &'static str)> {
        std::mem::take(&mut self.unhandled)
    }

    /// The screens the watch is currently showing, in order, once it has said.
    pub fn screens(&self) -> Option<Vec<u8>> {
        self.screens.clone()
    }

    /// Ask the watch which screens it shows.
    pub fn request_screens(&self) -> Vec<Action> {
        vec![Action::Send(Frame::new(
            Command::CMD_WAM_SCREENS_LIST_GET,
            Vec::new(),
        ))]
    }

    /// Replace the watch's screen list. Order is the order it cycles them in.
    ///
    /// The list is a fixed number of slots; a short one silently drops screens,
    /// so it is padded back out to full length with empty slots.
    pub fn set_screens(&self, ids: &[u8]) -> Vec<Action> {
        let mut slots = ids.to_vec();
        slots.truncate(SCREEN_SLOTS);
        slots.resize(SCREEN_SLOTS, 0);
        vec![
            Action::Send(Frame::new(
                Command::CMD_WAM_SCREENS_LIST,
                vec![WppObject::WamScreensList(WamScreensList {
                    screen_numbers: slots,
                })],
            )),
            // Read back rather than assume: the watch may reject or reorder.
            Action::Send(Frame::new(Command::CMD_WAM_SCREENS_LIST_GET, Vec::new())),
        ]
    }

    pub fn activities(&self) -> Option<Vec<u32>> {
        self.activities.clone()
    }

    pub fn wear_position(&self) -> Option<u8> {
        self.wear_position
    }

    /// Read the quick-launch activity menu, where the watch is worn, and
    /// whether it takes phone notifications.
    pub fn request_device_config(&self) -> Vec<Action> {
        vec![
            Action::Send(Frame::new(Command::CMD_WORKOUT_SCREEN_LIST_GET, Vec::new())),
            Action::Send(Frame::new(Command::CMD_GET_TRACKER_WEAR_POS, Vec::new())),
            Action::Send(Frame::new(
                Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_GET,
                Vec::new(),
            )),
        ]
    }

    /// Replace the quick-launch activity menu, in the order given.
    pub fn set_activities(&self, ids: &[u32]) -> Vec<Action> {
        let mut slots = ids.to_vec();
        slots.truncate(ACTIVITY_SLOTS);
        slots.resize(ACTIVITY_SLOTS, 0);
        vec![
            Action::Send(Frame::new(
                Command::CMD_WORKOUT_SCREEN_SET,
                vec![WppObject::WorkoutScreenList(WorkoutScreenList {
                    screen_nb: slots,
                })],
            )),
            Action::Send(Frame::new(Command::CMD_WORKOUT_SCREEN_LIST_GET, Vec::new())),
        ]
    }

    /// What the watch last said about phone notifications, once it has said.
    pub fn notifications(&self) -> Option<NotificationConfig> {
        self.notifications
    }

    /// The image sizes the watch says it can hold. Empty until it has said.
    pub fn image_formats(&self) -> &[crate::image::ImageFormat] {
        &self.image_formats
    }

    /// What the host wants, without saying so yet.
    ///
    /// The watch is only told once it has reported what it currently holds, so
    /// a preference can be set before there is a link to send it over.
    pub fn prefer_notifications(&mut self, enabled: bool) {
        self.wanted_notifications = Some(enabled);
    }

    /// Turn phone notifications on or off at the watch.
    ///
    /// This is only the watch's half. It governs whether the watch will go
    /// looking for the phone's ANCS server at all; the server itself is the
    /// host's to run, and with it switched on and no server listening the
    /// watch simply finds nothing.
    ///
    /// Switching it off is what keeps the link at the slower connection
    /// parameters: the watch asks for latency 15 only in order to run ANCS
    /// discovery, and never asks for the slower ones back. It costs every
    /// phone notification to have it.
    pub fn set_notifications(&mut self, enabled: bool) -> Vec<Action> {
        self.wanted_notifications = Some(enabled);
        vec![
            Action::Send(Frame::new(
                Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_SET,
                vec![WppObject::AncsStatus(AncsStatus {
                    status: u8::from(enabled),
                })],
            )),
            Action::Send(Frame::new(
                Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_GET,
                Vec::new(),
            )),
        ]
    }

    pub fn set_wear_position(&self, position: u8) -> Vec<Action> {
        vec![
            Action::Send(Frame::new(
                Command::CMD_SET_TRACKER_WEAR_POS,
                vec![WppObject::TrackerWearPos(TrackerWearPos {
                    value: position,
                })],
            )),
            Action::Send(Frame::new(Command::CMD_GET_TRACKER_WEAR_POS, Vec::new())),
        ]
    }

    /// Set the watch's clock. `now` is UTC; `gmt_offset` is what the watch adds
    /// to it to display local time.
    ///
    /// The watch answers with `TimeSetReply.drift`, the seconds it was out by.
    pub fn set_time(
        &self,
        now: UnixTime,
        gmt_offset: i32,
        next_change: Option<DstChange>,
    ) -> Vec<Action> {
        // A zone with no transition ahead of it still fills both fields: a
        // change to the offset it already has is a change to nothing.
        let change = next_change.unwrap_or(DstChange {
            at: UnixTime(0),
            gmt_offset,
        });
        vec![Action::Send(Frame::new(
            Command::CMD_TIME_SET,
            vec![WppObject::TimeSet(TimeSet {
                utc: now.0 as u32,
                gmt_offset,
                dst_change_time: change.at.0 as u32,
                next_gmt_offset: change.gmt_offset,
            })],
        ))]
    }

    /// Replace the set of enabled health features.
    ///
    /// The watch has no read side for this, and the message carries the whole
    /// set: anything left out is switched off. Callers must send every feature
    /// they want kept, including ones they do not understand.
    pub fn set_features(&self, features: &[(u16, u32, u32)]) -> Vec<Action> {
        let mut objects: Vec<WppObject> = vec![WppObject::Id(Id { value: 0 })];
        objects.extend(features.iter().map(|(id, start, end)| {
            WppObject::FeatureTagsDeprecated(FeatureTagsDeprecated {
                id: *id,
                start_time: *start,
                end_time: *end,
            })
        }));
        objects.push(WppObject::Null(Null {}));
        vec![Action::Send(Frame::new(
            Command::CMD_FEATURE_TAGS_SET_DEPRECATED_V2,
            objects,
        ))]
    }

    /// Daily totals and battery are not pushed; they have to be asked for.
    ///
    /// Asking before the handshake finishes does not merely fail: the watch
    /// answers ERR_NOT_AUTH, which is indistinguishable on the wire from a
    /// refused probe.
    pub fn refresh(&mut self) -> Vec<Action> {
        if matches!(
            self.phase,
            Phase::Idle | Phase::Probing | Phase::Authenticating
        ) {
            return Vec::new();
        }
        if let (Some(now), Some(last)) = (self.now, self.last_refresh) {
            if now.0 - last.0 < MIN_REFRESH_INTERVAL_MS {
                return Vec::new();
            }
        }
        let actions = self.force_refresh();
        self.noted(actions)
    }

    /// Just the battery, for while someone is watching the charging state.
    ///
    /// Deliberately not the daily totals: this runs every few seconds when the
    /// app is in front, and the totals neither change that fast nor matter to
    /// the question being asked.
    pub fn poll_battery(&mut self) -> Vec<Action> {
        if self.measuring.is_some()
            || matches!(
                self.phase,
                Phase::Idle | Phase::Probing | Phase::Authenticating
            )
        {
            return Vec::new();
        }
        let actions = vec![Action::Send(Frame::new(
            Command::CMD_BATTERY_STATUS,
            Vec::new(),
        ))];
        self.noted(actions)
    }

    /// The same request, ignoring the rate limit: someone asked for it.
    pub fn force_refresh(&mut self) -> Vec<Action> {
        if self.measuring.is_some() {
            return Vec::new();
        }
        if matches!(
            self.phase,
            Phase::Idle | Phase::Probing | Phase::Authenticating
        ) {
            return Vec::new();
        }
        self.last_refresh = self.now;
        self.noted(vec![
            Action::Send(Frame::new(
                Command::CMD_DISPLAYED_INFO_GET,
                vec![WppObject::InfoType(InfoType { value: 4 })],
            )),
            Action::Send(Frame::new(Command::CMD_BATTERY_STATUS, Vec::new())),
        ])
    }

    fn request_next(&mut self) -> Vec<Action> {
        match self.queue.pop() {
            Some(next) => {
                self.walk_started_from = Some(next.1);
                self.current = Some(next);
                self.request_current()
            }
            None => {
                self.phase = Phase::Finished;
                // Asking for the daily totals while the walk is running gets
                // ERR_DEVBUSY, and a busy reply to them is never retried. They
                // are also freshest here, once the history is in.
                let mut actions = vec![Action::Send(Frame::new(Command::CMD_SYNC_OK, Vec::new()))];
                actions.extend(self.refresh());
                actions
            }
        }
    }

    fn request_current(&mut self) -> Vec<Action> {
        let Some((category, from)) = self.current else {
            return Vec::new();
        };
        let window = WppObject::WamVasistasGet(WamVasistasGet {
            utc_start: from.0 as u32,
            max: 0,
        });
        let frame = if category == Category::BODY {
            Frame::new(Command::CMD_BODY_VASISTAS_GET, vec![window])
        } else if category == Category::ACTIVITY {
            // Without it the watch serves the records without their activity
            // recognition; the official app asks for 3 and gets
            // `VasistasActiRecoV1V2` on every waking window.
            Frame::new(
                Command::CMD_WAM_VASISTAS_GET,
                vec![
                    window,
                    WppObject::Version(Version {
                        value: Version::VALUE_ACTI_RECO_V3,
                    }),
                ],
            )
        } else {
            Frame::new(
                Command::CMD_VASISTAS_GET,
                vec![
                    window,
                    WppObject::VasistasType(VasistasType {
                        value: category.0 as i32,
                    }),
                ],
            )
        };
        vec![Action::Send(frame)]
    }
}

fn delete_frame(signal: &Signal) -> Frame {
    Frame::new(
        Command::CMD_STORED_MEASURE_SIGNAL_DEL,
        vec![
            WppObject::Id(Id { value: 1 }),
            WppObject::StoredSignalMeta(StoredSignalMeta {
                ..signal.meta.clone()
            }),
        ],
    )
}

/// SHA-1, so the crate keeps no dependencies. Only ever fed a challenge.
fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let bit_len = (data.len() as u64) * 8;
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bit_len.to_be_bytes());

    for block in message.chunks_exact(64) {
        let mut w = [0u32; 80];
        for (i, word) in block.chunks_exact(4).enumerate() {
            w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, word) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*word);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    let mut out = [0u8; 20];
    for (i, word) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::Null;

    fn credentials() -> Credentials {
        Credentials {
            mac: "a4:7e:fa:44:d6:10".to_string(),
            secret: "gUf8Np69A4GvJxjY1XOcIHKQm2HcPZnO".to_string(),
        }
    }

    #[test]
    fn sha1_matches_known_vectors() {
        assert_eq!(
            sha1(b"abc").to_vec(),
            hex(b"a9993e364706816aba3e25717850c26c9cd0d89d")
        );
        assert_eq!(
            sha1(b"").to_vec(),
            hex(b"da39a3ee5e6b4b0d3255bfef95601890afd80709")
        );
    }

    fn hex(s: &[u8]) -> Vec<u8> {
        s.chunks(2)
            .map(|p| u8::from_str_radix(std::str::from_utf8(p).unwrap(), 16).unwrap())
            .collect()
    }

    /// The exchange captured from the real watch.
    #[test]
    fn the_challenge_answer_matches_the_capture() {
        let challenge = [
            244, 197, 79, 127, 24, 111, 82, 130, 216, 87, 5, 54, 35, 63, 193, 35,
        ];
        assert_eq!(
            credentials().answer(&challenge),
            vec![
                84, 20, 165, 52, 232, 6, 253, 184, 77, 32, 105, 86, 199, 96, 220, 232, 42, 76, 25,
                32
            ]
        );
    }

    #[test]
    fn connecting_probes_then_answers_the_challenge() {
        let mut client = Client::new(credentials(), vec![(Category(8), UnixTime(1000))]);
        let actions = client.handle(Event::Connected);
        assert!(matches!(actions[0], Action::Send(ref f) if f.command == Command::CMD_PROBE));

        let challenge = Frame::new(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![1; 16],
            })],
        );
        let actions = client.handle(Event::Frame {
            frame: challenge,
            received_at: UnixMillis(0),
        });
        let Action::Send(reply) = &actions[0] else {
            panic!("expected a reply, got {actions:?}")
        };
        assert_eq!(reply.command, Command::CMD_PROBE_CHALLENGE);
        assert!(reply
            .objects
            .iter()
            .any(|o| matches!(o, WppObject::ProbeChallengeResponse(_))));
    }

    fn authenticated() -> Client {
        let mut client = Client::new(credentials(), vec![(Category(8), UnixTime(1000))]);
        client.handle(Event::Connected);
        client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(
                Command::CMD_PROBE_CHALLENGE,
                vec![WppObject::ProbeChallenge(ProbeChallenge {
                    mac: "a4:7e:fa:44:d6:10".to_string(),
                    challenge: vec![1; 16],
                })],
            ),
        });
        client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(Command::CMD_PROBE, Vec::new()),
        });
        client
    }

    #[test]
    fn a_null_reply_ends_the_category_and_finishes_the_sync() {
        let mut client = authenticated();
        let actions = client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]),
        });
        assert!(actions
            .iter()
            .any(|a| matches!(a, Action::Send(f) if f.command == Command::CMD_SYNC_OK)));
    }

    /// The watermark must only move past data the host has committed.
    #[test]
    fn the_watermark_advances_past_the_newest_record_seen() {
        use crate::objects::{VasistasHeartrate, WamVasistasHead};
        let mut client = authenticated();
        client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(
                Command::CMD_VASISTAS_GET,
                vec![
                    WppObject::WamVasistasHead(WamVasistasHead { utc: 5000 }),
                    WppObject::VasistasHeartrate(VasistasHeartrate {
                        heartrate: 62,
                        quality: 4,
                        temperature: 0,
                    }),
                ],
            ),
        });
        assert_eq!(client.watermarks(), vec![(Category(8), UnixTime(5001))]);
    }

    #[test]
    fn stored_samples_carry_their_window_timestamp() {
        use crate::objects::{VasistasHeartrate, WamVasistasHead};
        let mut client = authenticated();
        let actions = client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(
                Command::CMD_VASISTAS_GET,
                vec![
                    WppObject::WamVasistasHead(WamVasistasHead { utc: 5000 }),
                    WppObject::VasistasHeartrate(VasistasHeartrate {
                        heartrate: 62,
                        quality: 4,
                        temperature: 0,
                    }),
                ],
            ),
        });
        let stored = actions.iter().find_map(|a| match a {
            Action::Store { records, .. } => Some(records),
            _ => None,
        });
        assert_eq!(
            stored.unwrap()[0],
            Record::Sample {
                measured_at: UnixMillis(5_000_000),
                kind: SampleKind::HeartRate,
                value: 62,
                quality: Some(4),
                source: Source::Stored,
            }
        );
    }

    /// Nothing may be deleted from the watch before it is durable here.
    #[test]
    fn a_delete_waits_for_the_store_to_be_confirmed() {
        use crate::objects::{StoredSignalData, StoredSignalMetaExtend};
        let mut client = authenticated();
        let meta = StoredSignalMeta {
            r#type: 7,
            sampling_freq: 300,
            format: 0,
            size: 2,
            resolution: 14,
            channel: 1,
        };
        let actions = client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(
                Command::CMD_STORED_MEASURE_SIGNAL_GET,
                vec![
                    WppObject::StoredSignalMeta(meta),
                    WppObject::StoredSignalMetaExtend(StoredSignalMetaExtend {
                        duration: 1,
                        total_size: 4,
                        filter_bank: 0,
                    }),
                    WppObject::StoredSignalData(StoredSignalData {
                        samples: vec![1, 0, 2, 0],
                    }),
                ],
            ),
        });
        let token = actions
            .iter()
            .find_map(|a| match a {
                Action::Store { token, .. } => Some(*token),
                _ => None,
            })
            .expect("the ecg should be handed over for storage");
        assert!(
            !actions.iter().any(|a| matches!(a, Action::Delete(_))),
            "must not delete before the store is confirmed"
        );

        let actions = client.handle(Event::Stored { token });
        assert!(matches!(
            actions.as_slice(),
            [Action::Delete(f)] if f.command == Command::CMD_STORED_MEASURE_SIGNAL_DEL
        ));
    }

    /// An ECG spans hundreds of frames, so the transfer is the one part of a
    /// sync that can report exact progress rather than an estimate.
    #[test]
    fn a_signal_transfer_reports_bytes_against_the_declared_total() {
        use crate::objects::{StoredSignalData, StoredSignalMetaExtend};
        let mut client = authenticated();
        assert_eq!(client.transfer_progress(), None);

        let meta = StoredSignalMeta {
            r#type: 7,
            sampling_freq: 300,
            format: 0,
            size: 2,
            resolution: 14,
            channel: 1,
        };
        client.handle(frame(
            Command::CMD_STORED_MEASURE_SIGNAL_GET,
            vec![
                WppObject::StoredSignalMeta(meta),
                WppObject::StoredSignalMetaExtend(StoredSignalMetaExtend {
                    duration: 30,
                    total_size: 100,
                    filter_bank: 0,
                }),
                WppObject::StoredSignalData(StoredSignalData {
                    samples: vec![0; 40],
                }),
            ],
        ));
        assert_eq!(client.transfer_progress(), Some((40, 100)));

        client.handle(frame(
            Command::CMD_STORED_MEASURE_SIGNAL_GET,
            vec![WppObject::StoredSignalData(StoredSignalData {
                samples: vec![0; 20],
            })],
        ));
        assert_eq!(client.transfer_progress(), Some((60, 100)));
    }

    #[test]
    fn the_history_walk_reports_where_it_started_and_where_it_is() {
        use crate::objects::{VasistasHeartrate, WamVasistasHead};
        let mut client = authenticated();
        assert_eq!(client.walk_span(), Some((UnixTime(1000), UnixTime(1000))));

        client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5000 }),
                WppObject::VasistasHeartrate(VasistasHeartrate {
                    heartrate: 62,
                    quality: 4,
                    temperature: 0,
                }),
            ],
        ));
        assert_eq!(client.walk_span(), Some((UnixTime(1000), UnixTime(5001))));
        assert_eq!(client.records_emitted(), 1);
    }

    fn frame(command: Command, objects: Vec<WppObject>) -> Event {
        Event::Frame {
            frame: Frame::new(command, objects),
            received_at: UnixMillis(0),
        }
    }

    fn sent(actions: &[Action]) -> Vec<Command> {
        actions
            .iter()
            .filter_map(|a| match a {
                Action::Send(f) => Some(f.command),
                _ => None,
            })
            .collect()
    }

    fn stored(actions: &[Action]) -> Vec<Record> {
        actions
            .iter()
            .filter_map(|a| match a {
                Action::Store { records, .. } => Some(records.clone()),
                _ => None,
            })
            .flatten()
            .collect()
    }

    /// Heart rate lives behind CMD_BODY_VASISTAS_GET, which takes no type
    /// selector; the typed streams carry SpO2 and AHI instead.
    #[test]
    fn the_body_stream_uses_its_own_command_and_no_type() {
        let mut client = Client::new(credentials(), vec![(Category::BODY, UnixTime(4000))]);
        client.handle(Event::Connected);
        client.handle(frame(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![1; 16],
            })],
        ));
        let actions = client.handle(frame(Command::CMD_PROBE, vec![]));
        let requests: Vec<&Frame> = actions
            .iter()
            .filter_map(|a| match a {
                Action::Send(f) => Some(f),
                _ => None,
            })
            .collect();
        let body = requests
            .iter()
            .find(|f| f.command == Command::CMD_BODY_VASISTAS_GET)
            .expect("body stream requested");
        assert!(
            !body
                .objects
                .iter()
                .any(|o| matches!(o, WppObject::VasistasType(_))),
            "the body stream takes no type selector"
        );
        // The daily totals wait for the walk; asking now draws ERR_DEVBUSY.
        assert!(!requests
            .iter()
            .any(|f| f.command == Command::CMD_BATTERY_STATUS));
    }

    /// The activity stream has its own command and, like the body stream, no
    /// type selector.
    #[test]
    fn the_activity_stream_uses_its_own_command() {
        let mut client = Client::new(credentials(), vec![(Category::ACTIVITY, UnixTime(4000))]);
        client.handle(Event::Connected);
        client.handle(frame(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![1; 16],
            })],
        ));
        let actions = client.handle(frame(Command::CMD_PROBE, vec![]));
        let request = actions
            .iter()
            .find_map(|a| match a {
                Action::Send(f) if f.command == Command::CMD_WAM_VASISTAS_GET => Some(f),
                _ => None,
            })
            .expect("activity stream requested");
        assert_eq!(
            request.objects,
            vec![
                WppObject::WamVasistasGet(WamVasistasGet {
                    utc_start: 4000,
                    max: 0,
                }),
                WppObject::Version(Version {
                    value: Version::VALUE_ACTI_RECO_V3,
                }),
            ],
            "without the version the watch omits the activity recognition"
        );
    }

    /// One window of the activity stream: a head, its duration, and the
    /// counters for it, all of which belong to the same record.
    #[test]
    fn a_window_of_the_activity_stream_becomes_one_record() {
        use crate::objects::{
            VasistasActiRecoV1V2, WamVasistasAwake, WamVasistasDuration, WamVasistasHead,
            WamVasistasMetCalEarned, WamVasistasWalk,
        };
        let mut client = Client::new(credentials(), vec![(Category::ACTIVITY, UnixTime(0))]);
        client.handle(Event::Connected);
        client.handle(frame(Command::CMD_PROBE, vec![]));
        let actions = client.handle(frame(
            Command::CMD_WAM_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 1_784_969_340 }),
                WppObject::WamVasistasDuration(WamVasistasDuration { duration: 60 }),
                WppObject::WamVasistasMetCalEarned(WamVasistasMetCalEarned {
                    calories: 245,
                    met: 290,
                }),
                WppObject::WamVasistasAwake(WamVasistasAwake {
                    steps: 94,
                    distance: 7180,
                    ascent: 0,
                    descent: 0,
                }),
                WppObject::WamVasistasWalk(WamVasistasWalk { level: 2 }),
                WppObject::VasistasActiRecoV1V2(VasistasActiRecoV1V2 {
                    reco_v1: 14002,
                    reco_v2: 5438,
                }),
            ],
        ));
        assert_eq!(
            stored(&actions),
            vec![Record::Activity(Minute {
                duration_secs: 60,
                steps: Some(94),
                distance: Some(7180),
                ascent: Some(0),
                descent: Some(0),
                calories: Some(245),
                met: Some(290),
                walk_level: Some(2),
                reco_v1: Some(14002),
                reco_v2: Some(5438),
                ..Minute::opened(UnixTime(1_784_969_340))
            })]
        );
    }

    /// The stream sends window after window in one frame, and the counters
    /// after a head belong to it and not to the one before.
    #[test]
    fn consecutive_windows_do_not_share_their_counters() {
        use crate::objects::{WamVasistasAwake, WamVasistasDuration, WamVasistasHead};
        let mut client = Client::new(credentials(), vec![(Category::ACTIVITY, UnixTime(0))]);
        client.handle(Event::Connected);
        client.handle(frame(Command::CMD_PROBE, vec![]));
        let actions = client.handle(frame(
            Command::CMD_WAM_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5000 }),
                WppObject::WamVasistasDuration(WamVasistasDuration { duration: 60 }),
                WppObject::WamVasistasAwake(WamVasistasAwake {
                    steps: 94,
                    distance: 7180,
                    ascent: 0,
                    descent: 0,
                }),
                // An idle window: a head and a duration, nothing else.
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5060 }),
                WppObject::WamVasistasDuration(WamVasistasDuration { duration: 960 }),
            ],
        ));
        let records = stored(&actions);
        assert_eq!(records.len(), 2);
        assert_eq!(
            records[1],
            Record::Activity(Minute {
                duration_secs: 960,
                ..Minute::opened(UnixTime(5060))
            })
        );
        assert_eq!(
            client.current(),
            Some((Category::ACTIVITY, UnixTime(5061))),
            "the walk resumes past the newest window"
        );
    }

    #[test]
    fn nothing_is_asked_for_before_the_probe_completes() {
        let mut client = Client::new(credentials(), vec![(Category::BODY, UnixTime(0))]);
        let actions = client.handle(Event::Connected);
        assert_eq!(sent(&actions), vec![Command::CMD_PROBE]);
    }

    /// DEVBUSY means "ask again", not "skip this window".
    #[test]
    fn a_busy_watch_is_asked_again_for_the_same_window() {
        use crate::objects::Cmderror;
        let mut client = authenticated();
        let before = client.current();
        let actions = client.handle(frame(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror { cmd: 2424, err: -2 })],
        ));
        assert_eq!(
            sent(&actions),
            vec![Command::CMD_VASISTAS_GET],
            "the same window is requested again"
        );
        assert_eq!(client.current(), before, "and the watermark does not move");
    }

    /// A finished sync goes stale the moment it finishes; the walk has to be
    /// repeatable without losing where each stream got to.
    #[test]
    fn a_finished_sync_can_be_run_again_from_where_it_left_off() {
        use crate::objects::{Null, VasistasHeartrate, WamVasistasHead};
        let mut client = Client::new(credentials(), vec![(Category::BODY, UnixTime(4000))]);
        client.handle(Event::Connected);
        client.handle(frame(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![1; 16],
            })],
        ));
        client.handle(frame(Command::CMD_PROBE, vec![]));
        client.handle(frame(
            Command::CMD_BODY_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5000 }),
                WppObject::VasistasHeartrate(VasistasHeartrate {
                    heartrate: 62,
                    quality: 4,
                    temperature: 0,
                }),
            ],
        ));
        client.handle(frame(
            Command::CMD_BODY_VASISTAS_GET,
            vec![WppObject::Null(Null {})],
        ));
        assert_eq!(client.phase(), Phase::Finished);
        assert_eq!(
            client.watermarks(),
            vec![(Category::BODY, UnixTime(5001))],
            "a finished stream keeps its watermark"
        );

        let actions = client.sync_now();
        assert_eq!(sent(&actions), vec![Command::CMD_BODY_VASISTAS_GET]);
        assert_eq!(client.current(), Some((Category::BODY, UnixTime(5001))));
    }

    /// The watch asks for a sync when it has something; that is the cue.
    #[test]
    fn a_sync_request_from_the_watch_restarts_the_walk() {
        use crate::objects::{Null, SyncRequest};
        let mut client = authenticated();
        client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![WppObject::Null(Null {})],
        ));
        assert_eq!(client.phase(), Phase::Finished);

        let actions = client.handle(frame(
            Command::CMD_SYNC_REQUEST,
            vec![WppObject::SyncRequest(SyncRequest {
                r#type: 0,
                reserved: 0,
            })],
        ));
        assert!(
            sent(&actions).contains(&Command::CMD_VASISTAS_GET),
            "the walk starts again"
        );
    }

    /// The watch goes on asking for as long as it holds an undelivered dump,
    /// so a request the walk has nothing to answer is the one that means the
    /// diagnostic buffer rather than history.
    #[test]
    fn a_request_the_walk_cannot_answer_takes_the_dump_instead() {
        use crate::objects::{Null, SyncRequest};
        let mut client = authenticated();
        let done = |ms: i64| Event::Frame {
            frame: Frame::new(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]),
            received_at: UnixMillis(ms),
        };
        let ask = |ms: i64| Event::Frame {
            frame: Frame::new(
                Command::CMD_SYNC_REQUEST,
                vec![WppObject::SyncRequest(SyncRequest {
                    r#type: SyncRequest::TYPE_DEBUG_DUMP,
                    reserved: 0,
                })],
            ),
            received_at: UnixMillis(ms),
        };

        client.handle(done(0));
        let walked = client.handle(ask(1_000));
        assert!(
            sent(&walked).contains(&Command::CMD_VASISTAS_GET),
            "a walk that is due still comes first"
        );
        client.handle(done(2_000));

        let drained = client.handle(ask(3_000));
        assert_eq!(
            sent(&drained),
            vec![
                Command::CMD_SYNC_REQUEST.with_channel(Channel::SlaveRequest),
                Command::CMD_DEBUG_SET,
            ],
            "acknowledged, then the drain opens"
        );

        // While it runs, a walk would abandon the transfer half way.
        assert!(client.walk_now().is_empty());
    }

    /// The quick-launch menu is eight slots, zero-padded, like the screen list.
    #[test]
    fn the_activity_menu_is_padded_and_empty_slots_are_not_activities() {
        use crate::objects::WorkoutScreenList;
        let mut client = authenticated();
        client.handle(frame(
            Command::CMD_WORKOUT_SCREEN_LIST_GET,
            vec![WppObject::WorkoutScreenList(WorkoutScreenList {
                screen_nb: vec![16, 2, 36, 28, 0, 0, 0, 0],
            })],
        ));
        assert_eq!(client.activities(), Some(vec![16, 2, 36, 28]));

        let actions = client.set_activities(&[2, 16]);
        let Action::Send(frame) = &actions[0] else {
            panic!()
        };
        let WppObject::WorkoutScreenList(list) = &frame.objects[0] else {
            panic!()
        };
        assert_eq!(list.screen_nb, vec![2, 16, 0, 0, 0, 0, 0, 0]);
    }

    /// The watch loses this over a reboot, and a watch that quietly comes back
    /// with notifications on has also quietly halved its connection latency
    /// for good. The preference is put back whenever the watch reports
    /// otherwise, and left alone when it already agrees.
    #[test]
    fn a_notification_preference_is_restored_whenever_the_watch_has_lost_it() {
        use crate::objects::{AncsStatus, NotificationsDisplayState};
        let reports = |status: u8| {
            frame(
                Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_GET,
                vec![
                    WppObject::AncsStatus(AncsStatus { status }),
                    WppObject::NotificationsDisplayState(NotificationsDisplayState { status: 1 }),
                ],
            )
        };

        let mut client = authenticated();
        assert!(
            sent(&client.handle(reports(1))).is_empty(),
            "with no preference the watch is left as it is"
        );

        client.prefer_notifications(false);
        assert_eq!(
            sent(&client.handle(reports(1))),
            vec![
                Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_SET,
                Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_GET,
            ],
            "a watch that came back with them on is told again"
        );
        assert!(
            sent(&client.handle(reports(0))).is_empty(),
            "and once it agrees, nothing more is sent"
        );
    }

    /// Both switches arrive together and mean different things: a watch can
    /// agree to be a notification client and still not put anything on screen.
    #[test]
    fn the_two_notification_switches_are_read_separately() {
        use crate::objects::{AncsStatus, NotificationsDisplayState};
        let mut client = authenticated();
        assert_eq!(client.notifications(), None);

        client.handle(frame(
            Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_GET,
            vec![
                WppObject::AncsStatus(AncsStatus { status: 1 }),
                WppObject::NotificationsDisplayState(NotificationsDisplayState { status: 0 }),
            ],
        ));
        assert_eq!(
            client.notifications(),
            Some(NotificationConfig {
                accepted: true,
                displayed: false
            })
        );

        let actions = client.set_notifications(true);
        let Action::Send(frame) = &actions[0] else {
            panic!()
        };
        assert_eq!(
            frame.command.opcode(),
            Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_SET.0
        );
        assert_eq!(
            frame.objects,
            vec![WppObject::AncsStatus(AncsStatus { status: 1 })]
        );
    }

    /// Features are write-only and the message carries the whole set, so a
    /// write must name everything that should stay on.
    #[test]
    fn a_feature_write_carries_the_whole_set() {
        let client = authenticated();
        let actions = client.set_features(&[(14, 0, 0), (17, 0, 0), (9, 100, 200)]);
        let Action::Send(frame) = &actions[0] else {
            panic!()
        };
        let ids: Vec<u16> = frame
            .objects
            .iter()
            .filter_map(|o| match o {
                WppObject::FeatureTagsDeprecated(t) => Some(t.id),
                _ => None,
            })
            .collect();
        assert_eq!(ids, vec![14, 17, 9]);
        assert!(matches!(frame.objects.last(), Some(WppObject::Null(_))));
    }

    #[test]
    fn the_wear_position_round_trips() {
        use crate::objects::TrackerWearPos;
        let mut client = authenticated();
        client.handle(frame(
            Command::CMD_GET_TRACKER_WEAR_POS,
            vec![WppObject::TrackerWearPos(TrackerWearPos { value: 2 })],
        ));
        assert_eq!(
            client.wear_position(),
            Some(TrackerWearPos::VALUE_LEFT_WRIST)
        );
    }

    /// The values are the ones the official app sent in the capture, which the
    /// watch answered with a drift of one second.
    #[test]
    fn setting_the_clock_carries_the_offset_and_the_next_change() {
        let client = authenticated();
        let actions = client.set_time(
            UnixTime(1784997934),
            7200,
            Some(DstChange {
                at: UnixTime(1792890000),
                gmt_offset: 3600,
            }),
        );
        let Action::Send(frame) = &actions[0] else {
            panic!("expected a send, got {actions:?}")
        };
        assert_eq!(frame.command, Command::CMD_TIME_SET);
        assert_eq!(
            frame.objects,
            vec![WppObject::TimeSet(TimeSet {
                utc: 1784997934,
                gmt_offset: 7200,
                dst_change_time: 1792890000,
                next_gmt_offset: 3600,
            })]
        );
    }

    /// Somewhere that never changes offset still has to fill both fields.
    #[test]
    fn a_zone_with_no_transition_ahead_announces_the_offset_it_has() {
        let client = authenticated();
        let actions = client.set_time(UnixTime(1784997934), -18000, None);
        let Action::Send(frame) = &actions[0] else {
            panic!("expected a send, got {actions:?}")
        };
        assert_eq!(
            frame.objects,
            vec![WppObject::TimeSet(TimeSet {
                utc: 1784997934,
                gmt_offset: -18000,
                dst_change_time: 0,
                next_gmt_offset: -18000,
            })]
        );
    }

    /// The list is fixed-length and zero-padded; a short write drops screens.
    #[test]
    fn a_screen_list_is_padded_and_empty_slots_are_not_screens() {
        use crate::objects::WamScreensList;
        let mut client = authenticated();
        client.handle(frame(
            Command::CMD_WAM_SCREENS_LIST_GET,
            vec![WppObject::WamScreensList(WamScreensList {
                screen_numbers: vec![
                    6, 16, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                ],
            })],
        ));
        assert_eq!(
            client.screens(),
            Some(vec![6, 16, 4]),
            "empty slots are not screens"
        );

        let actions = client.set_screens(&[4, 6]);
        let Action::Send(frame) = &actions[0] else {
            panic!()
        };
        let WppObject::WamScreensList(list) = &frame.objects[0] else {
            panic!()
        };
        assert_eq!(list.screen_numbers.len(), 24, "padded back to full length");
        assert_eq!(&list.screen_numbers[..2], &[4, 6]);
        assert!(list.screen_numbers[2..].iter().all(|s| *s == 0));
    }

    /// A rejected handshake has to be visible; the alternative is a spinner
    /// that never resolves.
    #[test]
    fn a_refused_challenge_ends_in_not_authenticated() {
        use crate::objects::Cmderror;
        let mut client = Client::new(credentials(), vec![(Category::BODY, UnixTime(0))]);
        client.handle(Event::Connected);
        client.handle(frame(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![1; 16],
            })],
        ));
        assert_eq!(client.phase(), Phase::Authenticating);

        client.handle(frame(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror { cmd: 257, err: -5 })],
        ));
        assert_eq!(client.phase(), Phase::NotAuthenticated);
    }

    /// Reconnecting quickly, the watch skips the challenge and answers the
    /// probe outright. Waiting for one anyway leaves the client probing while
    /// the watch believes the link is up and asks it to sync every two
    /// seconds — which looks exactly like a watch that has gone quiet.
    #[test]
    fn a_probe_answered_without_a_challenge_still_starts_the_sync() {
        use crate::objects::ProbeReply;
        let mut client = Client::new(credentials(), vec![(Category::BODY, UnixTime(0))]);
        client.handle(Event::Connected);
        assert_eq!(client.phase(), Phase::Probing);

        let actions = client.handle(frame(
            Command::CMD_PROBE,
            vec![WppObject::ProbeReply(ProbeReply {
                name: "ScanWatch 2".to_string(),
                mac: "a4:7e:fa:44:d6:10".to_string(),
                ..Default::default()
            })],
        ));
        assert_eq!(client.phase(), Phase::Syncing);
        assert!(
            actions.iter().any(|a| matches!(a, Action::Send(_))),
            "the walk has to start, or nothing is ever asked for"
        );
    }

    /// Everything asked before the handshake finishes is refused with the same
    /// code the watch uses to refuse a probe. Reading those as a refused probe
    /// makes the client ignore the challenge it is waiting for.
    #[test]
    fn not_auth_for_another_command_does_not_abort_the_handshake() {
        use crate::objects::Cmderror;
        let mut client = Client::new(credentials(), vec![(Category::BODY, UnixTime(0))]);
        client.handle(Event::Connected);

        // CMD_BATTERY_STATUS, asked while the probe was still in flight.
        client.handle(frame(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror { cmd: 1284, err: -5 })],
        ));
        assert_eq!(client.phase(), Phase::Probing);

        let actions = client.handle(frame(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![1; 16],
            })],
        ));
        assert_eq!(client.phase(), Phase::Authenticating);
        let Action::Send(reply) = &actions[0] else {
            panic!()
        };
        assert_eq!(reply.command.opcode(), Command::CMD_PROBE_CHALLENGE.0);
    }

    /// The daily totals are not part of the walk and have to be asked for, but
    /// the watch answers ERR_DEVBUSY while a walk is running and nothing
    /// retries them. They belong at the end.
    #[test]
    fn the_daily_totals_are_asked_for_once_the_walk_is_done() {
        use crate::objects::Null;
        let mut client = Client::new(credentials(), vec![(Category::BODY, UnixTime(0))]);
        client.handle(Event::Connected);
        client.handle(frame(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![1; 16],
            })],
        ));

        let started = client.handle(frame(Command::CMD_PROBE, Vec::new()));
        let sent = |actions: &[Action]| -> Vec<u16> {
            actions
                .iter()
                .map(|a| match a {
                    Action::Send(f) => f.command.opcode(),
                    _ => 0,
                })
                .collect()
        };
        assert!(
            !sent(&started).contains(&Command::CMD_DISPLAYED_INFO_GET.0),
            "the walk has the watch busy"
        );

        let done = client.handle(frame(
            Command::CMD_BODY_VASISTAS_GET,
            vec![WppObject::Null(Null {})],
        ));
        assert_eq!(client.phase(), Phase::Finished);
        let commands = sent(&done);
        assert!(commands.contains(&Command::CMD_DISPLAYED_INFO_GET.0));
        assert!(commands.contains(&Command::CMD_BATTERY_STATUS.0));
    }

    /// The watch asks for a sync every few seconds and each one ends with a
    /// refresh, so without a limit the daily totals are re-requested at that
    /// rate and every one of them writes a row.
    #[test]
    fn the_automatic_refresh_is_rate_limited_but_an_explicit_one_is_not() {
        let mut client = authenticated();
        let at = |ms: i64| Event::Frame {
            frame: Frame::new(Command::CMD_SYNC_REQUEST, Vec::new()),
            received_at: UnixMillis(ms),
        };

        client.handle(at(0));
        assert!(!client.refresh().is_empty(), "the first one goes out");
        client.handle(at(MIN_REFRESH_INTERVAL_MS / 2));
        assert!(client.refresh().is_empty(), "half an interval is too soon");
        client.handle(at(MIN_REFRESH_INTERVAL_MS + 1));
        assert!(
            !client.refresh().is_empty(),
            "an interval on it is due again"
        );

        // The button in the UI means "now", whatever the limit says.
        client.handle(at(MIN_REFRESH_INTERVAL_MS + 2));
        assert!(client.refresh().is_empty());
        assert!(!client.force_refresh().is_empty());
    }

    /// A walk of every stream for each of the watch's sync requests is what
    /// held its radio at a 100% duty cycle, and most of the replies were empty.
    #[test]
    fn the_stream_walk_is_rate_limited_but_an_explicit_one_is_not() {
        let mut client = authenticated();
        // Ends the pass in progress, and carries the clock forward with it.
        let done = |ms: i64| Event::Frame {
            frame: Frame::new(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]),
            received_at: UnixMillis(ms),
        };
        // Advances the clock without asking for anything.
        let idle = |ms: i64| Event::Frame {
            frame: Frame::new(Command::CMD_BATTERY_STATUS, Vec::new()),
            received_at: UnixMillis(ms),
        };

        client.handle(done(0));
        assert_eq!(client.phase(), Phase::Finished);
        client.handle(idle(1_000));
        assert!(
            !client.sync_now().is_empty(),
            "the first walk is not limited"
        );

        client.handle(done(2_000));
        assert!(client.sync_now().is_empty(), "seconds later is too soon");

        client.handle(idle(MIN_WALK_INTERVAL_MS + 2_000));
        assert!(!client.sync_now().is_empty(), "an interval on it is due");

        client.handle(done(MIN_WALK_INTERVAL_MS + 3_000));
        assert!(client.sync_now().is_empty());
        assert!(!client.walk_now().is_empty(), "the button still means now");
    }

    /// The rate limit above governs the walk, not the answer. A request left
    /// unanswered is repeated for as long as the link is up, and the watch
    /// halves its connection latency while one is outstanding.
    #[test]
    fn every_sync_request_is_acknowledged_even_when_the_walk_is_not_due() {
        let mut client = authenticated();
        let ask = |ms: i64| Event::Frame {
            frame: Frame::new(Command::CMD_SYNC_REQUEST, Vec::new()),
            received_at: UnixMillis(ms),
        };
        let acked = |actions: &[Action]| {
            actions.iter().any(|a| {
                matches!(a, Action::Send(f)
                    if f.command.opcode() == Command::CMD_SYNC_REQUEST.0
                        && f.command.channel() == Some(Channel::SlaveRequest))
            })
        };

        assert!(acked(&client.handle(ask(0))));
        assert!(
            acked(&client.handle(ask(2_000))),
            "and the one two seconds behind it"
        );
    }

    /// `CMD_WORKOUT_START` is pushed once, to whoever is connected at the time.
    /// Connecting midway through a workout has to recover it by asking, or the
    /// live trace is attributed to nothing.
    #[test]
    fn a_workout_already_running_at_connect_is_picked_up() {
        use crate::objects::{ActivitySubcategory, StartTime, Status};
        let mut client = authenticated();

        let records = |actions: &[Action]| -> Vec<Record> {
            actions
                .iter()
                .find_map(|a| match a {
                    Action::Store { records, .. } => Some(records.clone()),
                    _ => None,
                })
                .unwrap_or_default()
        };

        let running = client.handle(frame(
            Command::CMD_WORKOUT_STATUS,
            vec![
                WppObject::Status(Status { value: 1 }),
                WppObject::ActivitySubcategory(ActivitySubcategory { value: 16 }),
                WppObject::StartTime(StartTime {
                    value: 1_785_000_000,
                }),
            ],
        ));
        assert_eq!(
            records(&running),
            vec![Record::WorkoutStarted {
                started_at: UnixTime(1_785_000_000),
                subcategory: 16,
            }]
        );

        // Status 0 is the ordinary case — nothing is running, and reporting a
        // workout then would invent one on every connect.
        let idle = client.handle(frame(
            Command::CMD_WORKOUT_STATUS,
            vec![
                WppObject::Status(Status { value: 0 }),
                WppObject::StartTime(StartTime {
                    value: 1_785_000_000,
                }),
            ],
        ));
        assert!(records(&idle).is_empty(), "no workout, no record");
    }

    /// The watch streams the waveform only while something says it is being
    /// A walk that dies partway leaves the last thing heard ahead of the last
    /// thing asked, which read as a healthy link and stalled the sync for good.
    #[test]
    fn a_walk_that_stops_replying_is_noticed() {
        let mut client = authenticated();
        assert_eq!(client.phase(), Phase::Syncing);

        // The watch answers the walk request, then goes quiet mid-stream.
        client.handle(Event::Frame {
            received_at: UnixMillis(1_000_000),
            frame: Frame::new(Command::CMD_VASISTAS_GET, Vec::new()),
        });

        let tick = |ms: i64| Event::Tick {
            now: UnixMillis(ms),
        };
        assert!(
            client
                .handle(tick(1_000_000 + SILENCE_TIMEOUT_MS - 1))
                .is_empty(),
            "still within its time to answer"
        );
        assert_eq!(
            client.handle(tick(1_000_000 + SILENCE_TIMEOUT_MS + 1)),
            vec![Action::Reconnect],
            "a stalled walk has to be given up on"
        );
    }

    /// State scoped to a link must not outlive it. Both wedges found so far
    /// were exactly this, so the reset is asserted field by field.
    #[test]
    fn a_disconnect_lets_go_of_everything_the_link_owned() {
        use crate::objects::MeasureCategory;
        let mut client = authenticated();
        client.handle(frame(
            Command::CMD_MEASURE_START,
            vec![WppObject::MeasureCategory(MeasureCategory { value: 1 })],
        ));
        assert_eq!(client.measuring(), Some(1));
        assert!(client.current().is_some(), "mid-walk when the link dies");
        let queued = client.watermarks().len();

        assert!(
            client.handle(Event::Disconnected).is_empty(),
            "nothing can be sent into a link that is gone"
        );

        assert_eq!(client.measuring(), None);
        assert_eq!(client.phase(), Phase::Idle);
        assert!(client.current().is_none());
        assert_eq!(
            client.watermarks().len(),
            queued,
            "the interrupted stream is kept, not dropped"
        );
    }

    /// A measurement interrupted by the link dropping must not disable the
    /// client for the life of the process.
    #[test]
    fn a_reconnect_clears_a_measurement_that_never_stopped() {
        use crate::objects::MeasureCategory;
        let mut client = authenticated();
        client.handle(frame(
            Command::CMD_MEASURE_START,
            vec![WppObject::MeasureCategory(MeasureCategory { value: 1 })],
        ));
        assert_eq!(client.measuring(), Some(1));

        // The link dies here: no CMD_MEASURE_STOP will ever arrive for it.
        client.handle(Event::Connected);
        assert_eq!(client.measuring(), None, "a new link has no measurement");

        // Once the new link has authenticated, the client must be usable
        // again; before this fix every request was refused for good.
        client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(
                Command::CMD_PROBE_CHALLENGE,
                vec![WppObject::ProbeChallenge(ProbeChallenge {
                    mac: "a4:7e:fa:44:d6:10".to_string(),
                    challenge: vec![1; 16],
                })],
            ),
        });
        let resumed = client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(Command::CMD_PROBE, Vec::new()),
        });
        assert!(
            resumed.iter().any(|a| matches!(a, Action::Send(_))),
            "the reconnected client must start asking for things again"
        );
        assert!(client.current().is_some(), "and be walking a stream");
    }

    /// looked at. Without the reply it records in silence and the live trace
    /// is lost, since it is never stored at full rate.
    #[test]
    fn a_measurement_starting_arms_the_live_waveform() {
        use crate::objects::{MeasureCategory, MeasureLiveEcg};
        let mut client = authenticated();

        let actions = client.handle(frame(
            Command::CMD_MEASURE_START,
            vec![WppObject::MeasureCategory(MeasureCategory { value: 1 })],
        ));
        assert_eq!(client.measuring(), Some(1));
        let Some(Action::Send(reply)) = actions
            .iter()
            .find(|a| matches!(a, Action::Send(f) if f.command.opcode() == Command::CMD_MEASURE_START.0))
            .cloned()
        else {
            panic!("the watch must be told the waveform is being shown")
        };
        assert_eq!(
            reply.command.channel(),
            Some(crate::frame::Channel::SlaveRequest)
        );
        assert!(reply.objects.iter().any(|o| matches!(
            o,
            WppObject::MeasureLiveAppStatus(s) if s.app_live_screen_displayed == 1
        )));

        // Two little-endian samples, as the watch sends them.
        client.handle(frame(
            Command::CMD_MEASURE_LIVE_DATA,
            vec![WppObject::MeasureLiveEcg(MeasureLiveEcg {
                samples: vec![0x82, 0xff, 0x10, 0x00],
            })],
        ));
        assert_eq!(client.live_samples(), &[-126, 16]);

        // Nothing may be asked of the watch while it records.
        assert!(client.sync_now().is_empty(), "no walk during a measurement");
        assert!(client.walk_now().is_empty(), "not even an explicit one");
        assert!(client.force_refresh().is_empty(), "nor the daily totals");

        // The stop names what was recorded; without asking for it by that
        // identity the waveform stays on the watch and is eventually dropped.
        use crate::objects::StoredMeasureMeta;
        let stopped = client.handle(frame(
            Command::CMD_MEASURE_STOP,
            vec![
                WppObject::MeasureCategory(MeasureCategory { value: 1 }),
                WppObject::StoredMeasureMeta(StoredMeasureMeta {
                    uid: 1,
                    user_id_cnt: 1,
                    user_id: vec![44128913, 0, 0],
                    attrib: 0,
                    time: 1784999415,
                }),
            ],
        ));
        assert_eq!(client.measuring(), None);
        let sent: Vec<u16> = stopped
            .iter()
            .filter_map(|a| match a {
                Action::Send(f) => Some(f.command.opcode()),
                _ => None,
            })
            .collect();
        assert!(
            sent.contains(&Command::CMD_MEASURE_STOP.0),
            "the watch repeats it unacknowledged"
        );
        assert!(sent.contains(&Command::CMD_STORED_MEASURE_SIGNAL_GET.0));
    }

    /// A link can stay up while the watch stops answering: writes still
    /// succeed and no disconnect is reported, so silence after a request is
    /// the only evidence there is.
    #[test]
    fn silence_after_a_request_asks_for_a_reconnect() {
        let mut client = authenticated();
        let tick = |ms: i64| Event::Tick {
            now: UnixMillis(ms),
        };

        // Nothing asked for yet, so silence means nothing.
        assert!(client.handle(tick(10_000)).is_empty());

        // The walk finishes, so the watch has said its piece.
        client.handle(Event::Frame {
            frame: Frame::new(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]),
            received_at: UnixMillis(20_000),
        });

        // A timer asks for more — the kind of send no event accompanies.
        client.handle(tick(1_000_000));
        assert!(!client.walk_now().is_empty(), "the walk asks for something");

        assert!(
            client
                .handle(tick(1_000_000 + SILENCE_TIMEOUT_MS - 1))
                .is_empty(),
            "a slow reply is not a dead watch"
        );
        assert_eq!(
            client.handle(tick(1_000_000 + SILENCE_TIMEOUT_MS + 1)),
            vec![Action::Reconnect]
        );

        // A reply at any point clears it.
        client.handle(Event::Frame {
            frame: Frame::new(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]),
            received_at: UnixMillis(1_200_000),
        });
        assert!(client.handle(tick(1_300_000)).is_empty(), "it answered");
    }

    /// The daily totals are timestamped with the day they belong to. Treating
    /// that as history rewinds the walk to midnight, and every refresh then
    /// re-reads the whole day.
    #[test]
    fn the_daily_totals_do_not_move_the_walk_backwards() {
        use crate::objects::{Steps, WamVasistasHead};
        let mut client = authenticated();

        // Read up to a point well into the day.
        client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 90_000 }),
                WppObject::VasistasCbt(crate::objects::VasistasCbt {
                    algo: 0,
                    attrib: 0,
                    temperature: 37_000,
                }),
            ],
        ));
        client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![WppObject::Null(Null {})],
        ));
        let reached = client.watermarks();

        // The totals arrive stamped with local midnight.
        client.handle(frame(
            Command::CMD_DISPLAYED_INFO_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 1 }),
                WppObject::Steps(Steps { value: 3169 }),
            ],
        ));
        assert_eq!(client.watermarks(), reached, "midnight must not rewind it");
    }

    /// Each stream is at its own point in time. Letting one stream's newest
    /// record decide where another resumes silently skips whatever lies
    /// between them — a night of sleep records, in the case that found this.
    #[test]
    fn one_stream_does_not_drag_another_forward() {
        use crate::objects::{VasistasCbt, WamVasistasHead};
        let cbt = |t: u32| {
            frame(
                Command::CMD_VASISTAS_GET,
                vec![
                    WppObject::WamVasistasHead(WamVasistasHead { utc: t }),
                    WppObject::VasistasCbt(VasistasCbt {
                        algo: 0,
                        attrib: 0,
                        temperature: 37_000,
                    }),
                ],
            )
        };
        let mut client = Client::new(
            credentials(),
            vec![
                (Category(11), UnixTime(1_000)),
                (Category(6), UnixTime(1_000)),
            ],
        );
        client.handle(Event::Connected);
        client.handle(frame(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![1; 16],
            })],
        ));
        client.handle(frame(Command::CMD_PROBE, Vec::new()));

        // The first stream is current, right up to now.
        client.handle(cbt(900_000));
        client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![WppObject::Null(Null {})],
        ));

        // The second is far behind, and answers from where it actually is.
        let actions = client.handle(cbt(2_000));
        let Some(Action::Send(next)) = actions.into_iter().find(|a| matches!(a, Action::Send(_)))
        else {
            panic!("it must ask for more of this stream")
        };
        let WppObject::WamVasistasGet(ask) = &next.objects[0] else {
            panic!()
        };
        assert_eq!(
            ask.utc_start, 2_001,
            "resume just past this stream's own newest record, not the other's"
        );
    }

    /// Staging arrives on the activity stream, as the body of a window in place
    /// of the step counts an awake one carries — not on the typed stream, where
    /// a decoder for it sat unreachable while ten nights went unstored.
    #[test]
    fn a_staged_sleep_record_is_captured() {
        use crate::objects::{WamVasistasDuration, WamVasistasHead, WamVasistasSleep};
        let mut client = authenticated();
        let actions = client.handle(frame(
            Command::CMD_WAM_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5_000 }),
                WppObject::WamVasistasDuration(WamVasistasDuration { duration: 780 }),
                WppObject::WamVasistasSleep(WamVasistasSleep { level: 2 }),
            ],
        ));
        let stored: Vec<Record> = actions
            .iter()
            .find_map(|a| match a {
                Action::Store { records, .. } => Some(records.clone()),
                _ => None,
            })
            .unwrap_or_default();
        let Some(Record::Activity(minute)) = stored.first() else {
            panic!("the staged window was not stored: {stored:?}");
        };
        assert_eq!(minute.at, UnixTime(5_000));
        assert_eq!(
            minute.duration_secs, 780,
            "a level covers its window, not the instant it is dated"
        );
        assert_eq!(minute.sleep_level, Some(2));
        assert_eq!(
            client.take_unhandled(),
            vec![],
            "nothing in a staged window should go unread"
        );
    }

    /// The failure that hid the staging was silent: a catch-all consumed an
    /// object type no arm handled. Anything unread has to be reportable.
    #[test]
    fn an_object_nothing_reads_is_reported() {
        use crate::objects::{WamVasistasHead, WamVasistasSleepDbg};
        let mut client = authenticated();
        client.handle(frame(
            Command::CMD_WAM_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5_000 }),
                WppObject::WamVasistasSleepDbg(WamVasistasSleepDbg { level: 1 }),
            ],
        ));
        assert_eq!(
            client.take_unhandled(),
            vec![(
                Command::CMD_WAM_VASISTAS_GET.0,
                1542,
                "TYPE_WAM_VASISTAS_SLEEP_DBG"
            )]
        );
        assert_eq!(client.take_unhandled(), vec![], "taking clears them");
    }

    /// A whole session driven message by message, asserting what the client
    /// sends at each step and the state it ends in.
    #[test]
    fn a_scripted_session_reaches_the_expected_end_state() {
        use crate::objects::{Null, VasistasCbt, VasistasHeartrate, WamVasistasHead};

        let mut client = Client::new(credentials(), vec![(Category(8), UnixTime(4000))]);
        assert_eq!(client.phase(), Phase::Idle);

        // 1. link up -> probe
        let actions = client.handle(Event::Connected);
        assert_eq!(sent(&actions), vec![Command::CMD_PROBE]);
        assert_eq!(client.phase(), Phase::Probing);

        // 2. the watch challenges us -> we answer it
        let actions = client.handle(frame(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![
                    244, 197, 79, 127, 24, 111, 82, 130, 216, 87, 5, 54, 35, 63, 193, 35,
                ],
            })],
        ));
        assert_eq!(sent(&actions), vec![Command::CMD_PROBE_CHALLENGE]);
        assert_eq!(client.phase(), Phase::Authenticating);
        let Action::Send(reply) = &actions[0] else {
            panic!()
        };
        assert!(reply.objects.contains(&WppObject::ProbeChallengeResponse(
            ProbeChallengeResponse {
                answer: vec![
                    84, 20, 165, 52, 232, 6, 253, 184, 77, 32, 105, 86, 199, 96, 220, 232, 42, 76,
                    25, 32
                ],
            }
        )));

        // 3. the watch accepts -> ask what it already knows (a workout in
        //    progress, the nights it has staged), then start the history walk
        let actions = client.handle(frame(Command::CMD_PROBE, vec![]));
        assert_eq!(
            sent(&actions),
            vec![Command::CMD_WORKOUT_STATUS, Command::CMD_VASISTAS_GET]
        );
        assert_eq!(client.phase(), Phase::Syncing);
        assert_eq!(client.current(), Some((Category(8), UnixTime(4000))));
        let request = actions
            .iter()
            .find_map(|a| match a {
                Action::Send(f) if f.command == Command::CMD_VASISTAS_GET => Some(f),
                _ => None,
            })
            .expect("history requested");
        assert!(request
            .objects
            .contains(&WppObject::WamVasistasGet(WamVasistasGet {
                utc_start: 4000,
                max: 0,
            })));

        // 4. a window of samples -> stored, and the next request moves past it
        let actions = client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5000 }),
                WppObject::VasistasHeartrate(VasistasHeartrate {
                    heartrate: 62,
                    quality: 4,
                    temperature: 0,
                }),
                WppObject::VasistasCbt(VasistasCbt {
                    algo: 0,
                    attrib: 1,
                    temperature: 37255,
                }),
            ],
        ));
        assert_eq!(sent(&actions), vec![Command::CMD_VASISTAS_GET]);
        assert_eq!(
            stored(&actions),
            vec![
                Record::Sample {
                    measured_at: UnixMillis(5_000_000),
                    kind: SampleKind::HeartRate,
                    value: 62,
                    quality: Some(4),
                    source: Source::Stored,
                },
                Record::Sample {
                    measured_at: UnixMillis(5_000_000),
                    kind: SampleKind::CoreTemperature,
                    value: 37255,
                    quality: None,
                    source: Source::Stored,
                },
            ]
        );
        assert_eq!(client.current(), Some((Category(8), UnixTime(5001))));

        // 5. nothing left -> the sync closes out and the daily totals, which
        //    the walk kept the watch too busy to answer, are asked for
        let actions = client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![WppObject::Null(Null {})],
        ));
        assert_eq!(
            sent(&actions),
            vec![
                Command::CMD_SYNC_OK,
                Command::CMD_DISPLAYED_INFO_GET,
                Command::CMD_BATTERY_STATUS,
            ]
        );

        assert_eq!(client.phase(), Phase::Finished);
        assert_eq!(client.current(), None);
        assert_eq!(client.pending_deletes(), 0);
        // Finishing must not discard where the walk got to; the next pass
        // resumes from here rather than replaying the history.
        assert_eq!(client.watermarks(), vec![(Category(8), UnixTime(5001))]);
    }
}
