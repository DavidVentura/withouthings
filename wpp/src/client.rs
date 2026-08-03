use crate::activity::Minute;
use crate::debug_dump::DebugDump;
use crate::frame::Channel;
use crate::objects::{
    ActivitySubcategory, AncsStatus, AppProbe, AppProbeOsVersion, EndTime, FeatureTagsDeprecated,
    Id, InfoType, MeasureCategory, MeasureLiveAppStatus, NotificationsDisplayState, Null,
    ProbeChallenge, ProbeChallengeResponse, StartTime, StoredSignalMeta, TimeSet, TrackerUser,
    TrackerWearPos, VasistasCbt, VasistasType, Version, WamScreensList, WamVasistasGet,
    WorkoutScreenMetadata,
};
use crate::signal::{Signal, SignalCollector};
use crate::units::{UnixMillis, UnixTime};
use crate::{Command, Frame, WppObject};

const ERR_DEVBUSY: i32 = -2;

pub(crate) fn rejects_probe(frame: &Frame) -> bool {
    frame.objects.iter().any(|o| {
        matches!(o, WppObject::Cmderror(e)
            if e.cmd == Command::CMD_PROBE.0 || e.cmd == Command::CMD_PROBE_CHALLENGE.0)
    })
}
pub const APP_PROBE: AppProbe = AppProbe {
    os: 1,
    app: 1,
    version: 8070101,
};

pub fn probe_frame() -> Frame {
    Frame::new(
        Command::CMD_PROBE,
        vec![
            WppObject::AppProbe(APP_PROBE),
            WppObject::AppProbeOsVersion(AppProbeOsVersion { os_version: 35 }),
        ],
    )
}

const MAX_BUSY_RETRIES: u32 = 20;
const MIN_REFRESH_INTERVAL_MS: i64 = 300_000;
const MIN_WALK_INTERVAL_MS: i64 = 900_000;
const MIN_DUMP_INTERVAL_MS: i64 = 900_000;
const SILENCE_TIMEOUT_MS: i64 = 90_000;
const SCREEN_SLOTS: usize = 24;
const ACTIVITY_SLOTS: usize = 8;
const MIN_WORKOUT_SECS: i64 = 30;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Credentials {
    pub mac: String,
    pub secret: String,
}

impl Credentials {
    pub fn answer(&self, challenge: &[u8]) -> Vec<u8> {
        let mut input = Vec::with_capacity(challenge.len() + self.mac.len() + self.secret.len());
        input.extend_from_slice(challenge);
        input.extend_from_slice(self.mac.as_bytes());
        input.extend_from_slice(self.secret.as_bytes());
        sha1(&input).to_vec()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceIdentity {
    pub name: String,
    pub firmware: u32,
    pub bootloader: u32,
    pub hardware: Option<u32>,
    pub rescue: Option<u32>,
}

impl DeviceIdentity {
    pub const UNREPORTED: u32 = 0xFF_FFFF;

    pub fn of(reply: &crate::objects::ProbeReply) -> DeviceIdentity {
        let reported = |value: u32| Some(value).filter(|v| *v != DeviceIdentity::UNREPORTED);
        DeviceIdentity {
            name: reply.name.clone(),
            firmware: reply.soft_version,
            bootloader: reply.bl_version,
            hardware: reported(reply.hard_version),
            rescue: reported(reply.rescue_version),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct WorkoutScreen {
    pub id: u32,
    pub name: String,
    pub face_mode: u8,
    pub flag: u16,
    pub glyphs: Vec<(u8, crate::image::Mono)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserProfile {
    pub id: u32,
    pub weight: u32,
    pub height: u32,
    pub gender: u8,
    pub birth: i32,
    pub first_name: String,
}

impl UserProfile {
    pub fn of(user: &crate::objects::TrackerUser) -> UserProfile {
        UserProfile {
            id: user.id,
            weight: user.weight,
            height: user.height,
            gender: user.gender,
            birth: user.birth,
            first_name: user.first_name.clone(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DstChange {
    pub at: UnixTime,
    pub gmt_offset: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct NotificationConfig {
    pub accepted: bool,
    pub displayed: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Category(pub u8);

impl Category {
    pub const BODY: Category = Category(0);
    pub const ACTIVITY: Category = Category(255);
}

#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    Connected,
    Disconnected,
    Frame {
        frame: Frame,
        received_at: UnixMillis,
    },
    Stored { token: u64 },
    Tick { now: UnixMillis },
}

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Send(Frame),
    Store {
        token: u64,
        records: Vec<Record>,
    },
    Delete(Frame),
    Finished,
    Reconnect,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Record {
    Sample {
        measured_at: UnixMillis,
        kind: SampleKind,
        value: i64,
        quality: Option<i64>,
        source: Source,
        window_secs: Option<i64>,
        context: Option<i64>,
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
    WorkoutDropped {
        started_at: UnixTime,
    },
    Activity(Minute),
    Ecg(Box<Signal>),
    Identity(DeviceIdentity),
    User(UserProfile),
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
    BatteryState,
    BatteryMillivolts,
    SleepLevel,
    Spo2,
    Ascent,
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

    pub fn is_level(self) -> bool {
        matches!(
            self,
            SampleKind::BatteryPercent | SampleKind::BatteryState | SampleKind::BatteryMillivolts
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Stored,
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Idle,
    Probing,
    Authenticating,
    Syncing,
    Finished,
    NotAuthenticated,
}

pub struct Client {
    credentials: Credentials,
    phase: Phase,
    queue: Vec<(Category, UnixTime)>,
    current: Option<(Category, UnixTime)>,
    done: Vec<(Category, UnixTime)>,
    batch_high_water: Option<(Category, UnixTime)>,
    signals: SignalCollector,
    next_token: u64,
    pending_deletes: Vec<(u64, Frame)>,
    walk_started_from: Option<UnixTime>,
    busy_retries: u32,
    stream_total: u32,
    screens: Option<Vec<u8>>,
    activities: Option<Vec<u32>>,
    wear_position: Option<u8>,
    notifications: Option<NotificationConfig>,
    image_formats: Vec<crate::image::ImageFormat>,
    records_emitted: u64,
    unhandled: Vec<(u16, u16, &'static str)>,
    now: Option<UnixMillis>,
    last_heard: Option<UnixMillis>,
    last_spoke: Option<UnixMillis>,
    measuring: Option<i16>,
    live_samples: Vec<i16>,
    last_refresh: Option<UnixMillis>,
    last_walk: Option<UnixMillis>,
    dump: DebugDump,
    last_dump: Option<UnixMillis>,
    wanted_notifications: Option<bool>,
    zone: Option<(i32, Option<DstChange>)>,
    pending_stop: Option<(UnixTime, UnixTime)>,
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
            zone: None,
            pending_stop: None,
        }
    }

    pub fn watermarks(&self) -> Vec<(Category, UnixTime)> {
        let mut all = self.queue.clone();
        all.extend(self.done.iter().copied());
        if let Some(current) = self.current {
            all.push(current);
        }
        all.sort_by_key(|(c, _)| *c);
        all
    }

    pub fn sync_now(&mut self) -> Vec<Action> {
        if let (Some(now), Some(last)) = (self.now, self.last_walk) {
            if now.0 - last.0 < MIN_WALK_INTERVAL_MS {
                return Vec::new();
            }
        }
        let actions = self.walk_now();
        self.noted(actions)
    }

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

    pub fn walk_now(&mut self) -> Vec<Action> {
        if self.measuring.is_some() {
            return Vec::new();
        }
        if self.dump.running() {
            return Vec::new();
        }
        if self.phase != Phase::Finished && self.phase != Phase::Syncing {
            return Vec::new();
        }
        if self.current.is_some() {
            return Vec::new();
        }
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

    pub fn measuring(&self) -> Option<i16> {
        self.measuring
    }

    pub fn live_samples(&self) -> &[i16] {
        &self.live_samples
    }

    pub fn phase(&self) -> Phase {
        self.phase
    }

    pub fn current(&self) -> Option<(Category, UnixTime)> {
        self.current
    }

    pub fn pending_deletes(&self) -> usize {
        self.pending_deletes.len()
    }

    pub fn walk_span(&self) -> Option<(UnixTime, UnixTime)> {
        Some((self.walk_started_from?, self.current?.1))
    }

    pub fn transfer_progress(&self) -> Option<(usize, usize)> {
        self.signals.transfer_progress()
    }

    pub fn records_emitted(&self) -> u64 {
        self.records_emitted
    }

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
        let awaiting_reply = matches!(
            self.phase,
            Phase::Probing | Phase::Authenticating | Phase::Syncing
        ) || self.last_spoke.is_some_and(|spoke| spoke > heard);
        let quiet_since =
            self.last_spoke
                .map_or(heard, |spoke| if spoke.0 > heard.0 { spoke } else { heard });
        if awaiting_reply && now.0 - quiet_since.0 > SILENCE_TIMEOUT_MS {
            return vec![Action::Reconnect];
        }
        Vec::new()
    }

    fn on_disconnected(&mut self) -> Vec<Action> {
        self.phase = Phase::Idle;
        self.busy_retries = 0;
        self.last_heard = None;
        self.last_spoke = None;
        self.measuring = None;
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
        self.on_disconnected();
        self.phase = Phase::Probing;
        self.last_heard = self.now;
        vec![Action::Send(probe_frame())]
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

        self.collect_passive(&frame, received_at, &mut records);
        self.collect_history(&frame, &mut records);
        self.live_samples.extend(self.signals.take_live());
        actions.extend(self.dump.on_frame(&frame).into_iter().map(Action::Send));

        // A refused menu write erases the whole store on the watch, so the
        // menu must be re-read after any error.
        if frame.command.opcode() == Command::CMD_ERROR.0
            && frame.objects.iter().any(|o| {
                matches!(o, WppObject::Cmderror(e) if e.cmd == Command::CMD_WORKOUT_SCREEN_SET.0)
            })
        {
            self.activities = None;
            actions.push(Action::Send(Frame::new(
                Command::CMD_WORKOUT_SCREEN_LIST_GET,
                Vec::new(),
            )));
        }

        match frame.command.opcode() {
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
            c if c == Command::CMD_SYNC_REQUEST.0 => {
                actions.push(Action::Send(Frame::new(
                    Command::CMD_SYNC_REQUEST.with_channel(Channel::SlaveRequest),
                    Vec::new(),
                )));
            }
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
                            WppObject::ProbeChallenge(ProbeChallenge {
                                mac: self.credentials.mac.clone(),
                                challenge: vec![0; 16],
                            }),
                        ],
                    )));
                }
            }
            (Phase::Probing | Phase::Authenticating, c)
                if c == Command::CMD_ERROR.0 && rejects_probe(&frame) =>
            {
                self.phase = Phase::NotAuthenticated;
            }
            (Phase::Probing | Phase::Authenticating, c) if c == Command::CMD_PROBE.0 => {
                self.phase = Phase::Syncing;
                if let (Some(now), Some((gmt_offset, next_change))) = (self.now, self.zone.clone())
                {
                    actions.extend(self.set_time(
                        UnixTime(now.0 / 1000),
                        gmt_offset,
                        next_change,
                    ));
                }
                // A workout that began while nothing was connected is
                // undiscoverable otherwise: `CMD_WORKOUT_START` is pushed once
                // and never replayed.
                actions.push(Action::Send(Frame::new(
                    Command::CMD_WORKOUT_STATUS,
                    Vec::new(),
                )));
                actions.extend(self.request_next());
            }
            (Phase::Finished, c) if c == Command::CMD_SYNC_REQUEST.0 => {
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
                if busy && self.busy_retries < MAX_BUSY_RETRIES {
                    self.busy_retries += 1;
                    actions.extend(self.request_current());
                }
            }
            (Phase::Syncing, _) => {
                self.busy_retries = 0;
                let carries_records = frame
                    .objects
                    .iter()
                    .any(|o| matches!(o, WppObject::WamVasistasHead(_)));
                if let Some((seen, high)) = self.batch_high_water.take() {
                    if let Some((category, _)) = self.current {
                        if seen == category {
                            self.current = Some((category, UnixTime(high.0 + 1)));
                        }
                    }
                }
                let closed = !carries_records
                    && frame
                        .objects
                        .iter()
                        .any(|o| matches!(o, WppObject::Null(_)));
                if closed {
                    if let Some(finished) = self.current.take() {
                        self.done.push(finished);
                    }
                    actions.extend(self.request_next());
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
        let declared = crate::image::ImageFormat::declared(frame);
        if !declared.is_empty() {
            self.image_formats = declared;
        }

        if frame.command.opcode() == Command::CMD_WORKOUT_STATUS.0 {
            let running = frame
                .objects
                .iter()
                .any(|o| matches!(o, WppObject::Status(s) if s.value == 1));
            if let Some((started_at, ended_at)) = self.pending_stop.take() {
                if !running && ended_at.0 - started_at.0 < MIN_WORKOUT_SECS {
                    records.push(Record::WorkoutDropped { started_at });
                } else if !running {
                    records.push(Record::WorkoutEnded {
                        started_at,
                        ended_at,
                        paused_secs: frame
                            .objects
                            .iter()
                            .find_map(|o| match o {
                                WppObject::PauseState(p) => Some(p.sum as i64),
                                _ => None,
                            })
                            .unwrap_or(0),
                    });
                }
            }
        }

        for object in &frame.objects {
            self.signals.observe(object);
            match object {
                WppObject::ProbeReply(reply) => {
                    records.push(Record::Identity(DeviceIdentity::of(reply)));
                }
                WppObject::TrackerUser(user) => {
                    records.push(Record::User(UserProfile::of(user)));
                }
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
                    self.screens = Some(
                        list.screen_numbers
                            .iter()
                            .copied()
                            .filter(|id| *id != 0)
                            .collect(),
                    );
                }
                WppObject::Steps(steps) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::Steps,
                        value: steps.value as i64,
                        quality: None,
                        source: Source::Live,
                        window_secs: None,
                        context: None,
                    });
                }
                WppObject::Stairs(stairs) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::Ascent,
                        value: stairs.value as i64,
                        quality: None,
                        source: Source::Live,
                        window_secs: None,
                        context: None,
                    });
                }
                WppObject::Calories(calories) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::Calories,
                        value: calories.value as i64,
                        quality: None,
                        source: Source::Live,
                        window_secs: None,
                        context: None,
                    });
                }
                WppObject::Distance(distance) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::Distance,
                        value: distance.value as i64,
                        quality: None,
                        source: Source::Live,
                        window_secs: None,
                        context: None,
                    });
                }
                WppObject::Duration(duration) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::TrackedDuration,
                        value: duration.value as i64,
                        quality: None,
                        source: Source::Live,
                        window_secs: None,
                        context: None,
                    });
                }
                WppObject::BatteryStatus(battery) => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::BatteryPercent,
                        value: battery.battery_percent as i64,
                        quality: None,
                        source: Source::Live,
                        window_secs: None,
                        context: None,
                    });
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::BatteryState,
                        value: battery.battery_state as i64,
                        quality: None,
                        source: Source::Live,
                        window_secs: None,
                        context: None,
                    });
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::BatteryMillivolts,
                        value: battery.battery_mv as i64,
                        quality: None,
                        source: Source::Live,
                        window_secs: None,
                        context: None,
                    });
                }
                WppObject::LiveHr(live) if live.hr > 0 => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::HeartRate,
                        value: live.hr as i64,
                        quality: None,
                        source: Source::Live,
                        window_secs: None,
                        context: None,
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
                    let Some(started) = started else { continue };
                    let started_at = UnixTime(started as i64);
                    let ended_at = UnixTime(end.value as i64);
                    if ended_at.0 - started_at.0 < MIN_WORKOUT_SECS {
                        records.retain(|record| {
                            !matches!(
                                record,
                                Record::WorkoutStarted { started_at: s, .. } if *s == started_at
                            )
                        });
                        records.push(Record::WorkoutDropped { started_at });
                        continue;
                    }
                    records.push(Record::WorkoutEnded {
                        started_at,
                        ended_at,
                        paused_secs: paused,
                    });
                }
                _ => {}
            }
        }
    }

    fn collect_history(&mut self, frame: &Frame, records: &mut Vec<Record>) {
        let opcode = frame.command.opcode();
        if opcode == Command::CMD_WAM_VASISTAS_GET.0 {
            return self.collect_activity(frame, records);
        }
        if opcode != Command::CMD_VASISTAS_GET.0 && opcode != Command::CMD_BODY_VASISTAS_GET.0 {
            return;
        }
        let mut at: Option<UnixTime> = None;
        let mut window: Option<i64> = None;
        for object in &frame.objects {
            match object {
                WppObject::WamVasistasHead(head) => {
                    let time = UnixTime(head.utc as i64);
                    at = Some(time);
                    window = None;
                    self.note_head(time);
                }
                WppObject::WamVasistasDuration(covers) => {
                    window = Some(covers.duration as i64);
                }
                WppObject::VasistasHeartrate(hr) if hr.heartrate > 0 => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::HeartRate,
                            value: hr.heartrate as i64,
                            quality: Some(hr.quality as i64),
                            source: Source::Stored,
                            window_secs: window,
                            context: None,
                        });
                    }
                }
                WppObject::VasistasCbt(cbt) if cbt.attrib != VasistasCbt::ATTRIB_BASELINE => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::CoreTemperature,
                            value: cbt.temperature as i64,
                            quality: None,
                            source: Source::Stored,
                            window_secs: window,
                            context: Some(cbt.attrib as i64),
                        });
                    }
                }
                WppObject::VasistasCbt(_) => {}
                WppObject::VasistasSpo2(spo2) if spo2.error == 0 && spo2.spo2 > 0 => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::Spo2,
                            value: spo2.spo2 as i64,
                            quality: Some(spo2.quality as i64),
                            source: Source::Stored,
                            window_secs: window,
                            context: None,
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
                            window_secs: window,
                            context: None,
                        });
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::HrvRmssd,
                            value: hrv.rmssd as i64,
                            quality: Some(hrv.quality as i64),
                            source: Source::Stored,
                            window_secs: window,
                            context: None,
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
                            window_secs: window,
                            context: None,
                        });
                    }
                }
                WppObject::VasistasHeartrate(_) | WppObject::VasistasRr(_) => {}
                WppObject::WamVasistasGet(_) | WppObject::VasistasType(_) | WppObject::Null(_) => {}
                WppObject::VasistasFlags(_) => {}
                other => self.note_unhandled(frame, other),
            }
        }
    }

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
                WppObject::VasistasActiRecoV1V2(r) => {
                    minute.reco_v1 = Some(r.reco_v1 as i64);
                    minute.reco_v2 = Some(r.reco_v2 as i64);
                }
                WppObject::WamVasistasSleep(s) => minute.sleep_level = Some(s.level as i64),
                WppObject::WamVasistasGet(_) | WppObject::VasistasType(_) | WppObject::Null(_) => {}
                other => self.note_unhandled(frame, other),
            }
        }
        records.extend(open.map(Record::Activity));
    }

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

    pub fn take_unhandled(&mut self) -> Vec<(u16, u16, &'static str)> {
        std::mem::take(&mut self.unhandled)
    }

    pub fn screens(&self) -> Option<Vec<u8>> {
        self.screens.clone()
    }

    pub fn request_screens(&self) -> Vec<Action> {
        vec![Action::Send(Frame::new(
            Command::CMD_WAM_SCREENS_LIST_GET,
            Vec::new(),
        ))]
    }

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
            Action::Send(Frame::new(Command::CMD_WAM_SCREENS_LIST_GET, Vec::new())),
        ]
    }

    pub fn activities(&self) -> Option<Vec<u32>> {
        self.activities.clone()
    }

    pub fn wear_position(&self) -> Option<u8> {
        self.wear_position
    }

    pub fn request_device_config(&self) -> Vec<Action> {
        vec![
            Action::Send(Frame::new(Command::CMD_WORKOUT_SCREEN_LIST_GET, Vec::new())),
            Action::Send(Frame::new(Command::CMD_GET_TRACKER_WEAR_POS, Vec::new())),
            Action::Send(Frame::new(
                Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_GET,
                Vec::new(),
            )),
            Action::Send(Frame::new(Command::CMD_TRACKER_USER_GET, Vec::new())),
        ]
    }

    pub fn set_user(&self, profile: &UserProfile) -> Vec<Action> {
        vec![
            Action::Send(Frame::new(
                Command::CMD_TRACKER_USER_SET,
                vec![WppObject::TrackerUser(TrackerUser {
                    id: profile.id,
                    weight: profile.weight,
                    height: profile.height,
                    gender: profile.gender,
                    birth: profile.birth,
                    first_name: profile.first_name.clone(),
                })],
            )),
            Action::Send(Frame::new(Command::CMD_TRACKER_USER_GET, Vec::new())),
        ]
    }

    pub fn start_workout(&self, subcategory: i16, at: UnixTime) -> Vec<Action> {
        vec![
            Action::Send(Frame::new(
                Command::CMD_WORKOUT_START,
                vec![
                    WppObject::ActivitySubcategory(ActivitySubcategory { value: subcategory }),
                    WppObject::StartTime(StartTime { value: at.0 as i32 }),
                ],
            )),
            Action::Send(Frame::new(Command::CMD_WORKOUT_STATUS, Vec::new())),
        ]
    }

    /// An end earlier than the start — which a second of clock skew between
    /// host and watch is enough to cause — would otherwise be stored as an
    /// undeletable session lasting nearly a century, so the end is moved up
    /// to the start.
    pub fn stop_workout(&mut self, started_at: UnixTime, at: UnixTime) -> Vec<Action> {
        let ended_at = UnixTime(at.0.max(started_at.0));
        self.pending_stop = Some((started_at, ended_at));
        vec![
            Action::Send(Frame::new(
                Command::CMD_WORKOUT_STOP,
                vec![
                    WppObject::StartTime(StartTime {
                        value: started_at.0 as i32,
                    }),
                    WppObject::EndTime(EndTime {
                        value: ended_at.0 as i32,
                    }),
                ],
            )),
            Action::Send(Frame::new(Command::CMD_WORKOUT_STATUS, Vec::new())),
        ]
    }

    /// Each entry must go whole, since the watch keeps no catalogue of its own
    /// and an id alone means nothing to it; sending back the id array that a
    /// read returns empties the menu instead of setting it.
    pub fn set_activities(&self, screens: &[WorkoutScreen]) -> Vec<Action> {
        let mut objects = Vec::new();
        for screen in screens.iter().take(ACTIVITY_SLOTS) {
            objects.push(WppObject::WorkoutScreenMetadata(WorkoutScreenMetadata {
                id: screen.id,
                version: 0,
                name: screen.name.clone(),
                face_mode: screen.face_mode,
                flag: screen.flag,
            }));
            for (kind, glyph) in &screen.glyphs {
                objects.push(WppObject::ImageMetadata(glyph.metadata_of(*kind)));
                objects.extend(glyph.data_objects());
            }
        }

        objects.push(WppObject::Null(Null {}));

        vec![
            Action::Send(Frame::new(Command::CMD_WORKOUT_SCREEN_SET, objects)),
            Action::Send(Frame::new(Command::CMD_WORKOUT_SCREEN_LIST_GET, Vec::new())),
        ]
    }

    pub fn notifications(&self) -> Option<NotificationConfig> {
        self.notifications
    }

    pub fn image_formats(&self) -> &[crate::image::ImageFormat] {
        &self.image_formats
    }

    pub fn prefer_notifications(&mut self, enabled: bool) {
        self.wanted_notifications = Some(enabled);
    }

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

    pub fn set_zone(&mut self, gmt_offset: i32, next_change: Option<DstChange>) {
        self.zone = Some((gmt_offset, next_change));
    }

    pub fn set_time(
        &self,
        now: UnixTime,
        gmt_offset: i32,
        next_change: Option<DstChange>,
    ) -> Vec<Action> {
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

    /// The watch has no read side for this and the write carries the whole
    /// set: anything left out is silently switched off.
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

    pub fn factory_reset(&mut self) -> Vec<Action> {
        let actions = vec![Action::Send(Frame::new(
            Command::CMD_FACTORY_RESET,
            Vec::new(),
        ))];
        self.noted(actions)
    }

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

    pub(super) fn authenticated() -> Client {
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
                window_secs: None,
                context: None,
            }
        );
    }

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
        assert!(!requests
            .iter()
            .any(|f| f.command == Command::CMD_BATTERY_STATUS));
    }

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

        assert!(client.walk_now().is_empty());
    }

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
    }

    #[test]
    fn a_multi_frame_window_is_asked_for_once() {
        use crate::objects::{VasistasCbt, WamVasistasHead};
        let records = |from: u32| {
            frame(
                Command::CMD_VASISTAS_GET,
                (0..6)
                    .flat_map(|i| {
                        [
                            WppObject::WamVasistasHead(WamVasistasHead { utc: from + i * 60 }),
                            WppObject::VasistasCbt(VasistasCbt {
                                algo: VasistasCbt::ALGO_FREE_LIVING,
                                attrib: VasistasCbt::ATTRIB_NORMAL,
                                temperature: 36_900,
                            }),
                        ]
                    })
                    .collect(),
            )
        };
        let mut client = authenticated();

        for batch in 0..20 {
            let actions = client.handle(records(10_000 + batch * 360));
            assert!(
                sent(&actions).is_empty(),
                "frame {batch} of a reply still in flight asked for more"
            );
        }

        let closing = client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![WppObject::Null(Null {})],
        ));
        assert!(
            sent(&closing).contains(&Command::CMD_SYNC_OK),
            "the terminator closes the window: {:?}",
            sent(&closing)
        );
    }

    #[test]
    fn a_temperature_baseline_is_not_stored_but_a_sleeping_one_is() {
        use crate::objects::{VasistasCbt, WamVasistasDuration, WamVasistasHead};
        let mut client = authenticated();
        let stored_records = client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5_000 }),
                WppObject::WamVasistasDuration(WamVasistasDuration { duration: 60 }),
                WppObject::VasistasCbt(VasistasCbt {
                    algo: VasistasCbt::ALGO_FREE_LIVING,
                    attrib: VasistasCbt::ATTRIB_SLEEPING,
                    temperature: 36_500,
                }),
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5_060 }),
                WppObject::WamVasistasDuration(WamVasistasDuration { duration: 0 }),
                WppObject::VasistasCbt(VasistasCbt {
                    algo: VasistasCbt::ALGO_FREE_LIVING,
                    attrib: VasistasCbt::ATTRIB_BASELINE,
                    temperature: 37_030,
                }),
            ],
        ));
        assert_eq!(
            stored(&stored_records),
            vec![Record::Sample {
                measured_at: UnixMillis(5_000_000),
                kind: SampleKind::CoreTemperature,
                value: 36_500,
                quality: None,
                source: Source::Stored,
                window_secs: Some(60),
                context: Some(VasistasCbt::ATTRIB_SLEEPING as i64),
            }],
            "the baseline is dropped and the sleeping reading keeps its window"
        );
    }

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

    #[test]
    fn not_auth_for_another_command_does_not_abort_the_handshake() {
        use crate::objects::Cmderror;
        let mut client = Client::new(credentials(), vec![(Category::BODY, UnixTime(0))]);
        client.handle(Event::Connected);

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

        client.handle(at(MIN_REFRESH_INTERVAL_MS + 2));
        assert!(client.refresh().is_empty());
        assert!(!client.force_refresh().is_empty());
    }

    #[test]
    fn the_stream_walk_is_rate_limited_but_an_explicit_one_is_not() {
        let mut client = authenticated();
        let done = |ms: i64| Event::Frame {
            frame: Frame::new(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]),
            received_at: UnixMillis(ms),
        };
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

    #[test]
    fn the_clock_is_put_right_as_soon_as_the_watch_will_answer() {
        use crate::objects::{ProbeChallenge, ProbeReply, TimeSet};
        let mut client = Client::new(credentials(), Vec::new());
        client.set_zone(7200, None);
        client.handle(Event::Connected);

        let sent = |actions: &[Action]| -> Vec<Frame> {
            actions
                .iter()
                .filter_map(|a| match a {
                    Action::Send(f) if f.command == Command::CMD_TIME_SET => Some(f.clone()),
                    _ => None,
                })
                .collect()
        };

        let challenged = client.handle(Event::Frame {
            frame: Frame::new(
                Command::CMD_PROBE_CHALLENGE,
                vec![WppObject::ProbeChallenge(ProbeChallenge {
                    mac: "a4:7e:fa:44:d6:10".to_string(),
                    challenge: vec![1; 16],
                })],
            ),
            received_at: UnixMillis(1_785_000_000_000),
        });
        assert!(sent(&challenged).is_empty());

        let opened = client.handle(Event::Frame {
            frame: Frame::new(
                Command::CMD_PROBE,
                vec![WppObject::ProbeReply(ProbeReply::default())],
            ),
            received_at: UnixMillis(1_785_000_000_000),
        });
        let clock = sent(&opened);
        assert_eq!(clock.len(), 1, "the clock goes out once the probe closes");
        assert_eq!(
            clock[0].objects,
            vec![WppObject::TimeSet(TimeSet {
                utc: 1_785_000_000,
                gmt_offset: 7200,
                dst_change_time: 0,
                next_gmt_offset: 7200,
            })],
        );
    }

    #[test]
    fn a_stop_is_confirmed_by_the_session_being_gone() {
        use crate::objects::{StartTime, Status};
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
        let status = |value: i8| {
            frame(
                Command::CMD_WORKOUT_STATUS,
                vec![
                    WppObject::Status(Status { value }),
                    WppObject::StartTime(StartTime {
                        value: 1_785_000_000,
                    }),
                ],
            )
        };

        client.stop_workout(UnixTime(1_785_000_000), UnixTime(1_785_000_600));

        let ended = |actions: &[Action]| -> Vec<Record> {
            records(actions)
                .into_iter()
                .filter(|r| matches!(r, Record::WorkoutEnded { .. }))
                .collect()
        };
        assert!(ended(&client.handle(status(1))).is_empty());

        client.stop_workout(UnixTime(1_785_000_000), UnixTime(1_785_000_600));
        assert_eq!(
            ended(&client.handle(status(0))),
            vec![Record::WorkoutEnded {
                started_at: UnixTime(1_785_000_000),
                ended_at: UnixTime(1_785_000_600),
                paused_secs: 0,
            }],
        );

        assert!(ended(&client.handle(status(0))).is_empty());
    }

    #[test]
    fn a_short_session_stopped_from_here_is_dropped_too() {
        use crate::objects::{StartTime, Status};
        let mut client = authenticated();

        client.stop_workout(UnixTime(1_785_000_000), UnixTime(1_785_000_000 + 8));
        let actions = client.handle(frame(
            Command::CMD_WORKOUT_STATUS,
            vec![
                WppObject::Status(Status { value: 0 }),
                WppObject::StartTime(StartTime {
                    value: 1_785_000_000,
                }),
            ],
        ));
        let stored: Vec<Record> = actions
            .iter()
            .find_map(|a| match a {
                Action::Store { records, .. } => Some(records.clone()),
                _ => None,
            })
            .unwrap_or_default();
        assert_eq!(
            stored,
            vec![Record::WorkoutDropped {
                started_at: UnixTime(1_785_000_000),
            }],
            "eight seconds is a mis-press however it was stopped",
        );
    }

    #[test]
    fn a_session_too_short_to_be_one_is_dropped_rather_than_ended() {
        use crate::objects::{ActivitySubcategory, EndTime, StartTime, Status};
        let mut client = authenticated();

        let brief = client.handle(frame(
            Command::CMD_WORKOUT_STOP,
            vec![
                WppObject::StartTime(StartTime {
                    value: 1_785_000_000,
                }),
                WppObject::EndTime(EndTime {
                    value: 1_785_000_004,
                }),
            ],
        ));
        assert_eq!(
            stored(&brief),
            vec![Record::WorkoutDropped {
                started_at: UnixTime(1_785_000_000),
            }]
        );

        let both = client.handle(frame(
            Command::CMD_WORKOUT_STATUS,
            vec![
                WppObject::Status(Status { value: 1 }),
                WppObject::ActivitySubcategory(ActivitySubcategory { value: 16 }),
                WppObject::StartTime(StartTime {
                    value: 1_785_000_100,
                }),
                WppObject::EndTime(EndTime {
                    value: 1_785_000_105,
                }),
            ],
        ));
        assert_eq!(
            stored(&both),
            vec![Record::WorkoutDropped {
                started_at: UnixTime(1_785_000_100),
            }]
        );

        let kept = client.handle(frame(
            Command::CMD_WORKOUT_STOP,
            vec![
                WppObject::StartTime(StartTime {
                    value: 1_785_000_200,
                }),
                WppObject::EndTime(EndTime {
                    value: 1_785_000_230,
                }),
            ],
        ));
        assert_eq!(
            stored(&kept),
            vec![Record::WorkoutEnded {
                started_at: UnixTime(1_785_000_200),
                ended_at: UnixTime(1_785_000_230),
                paused_secs: 0,
            }],
            "thirty seconds is a session"
        );
    }

    #[test]
    fn a_walk_that_stops_replying_is_noticed() {
        let mut client = authenticated();
        assert_eq!(client.phase(), Phase::Syncing);

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

    #[test]
    fn a_reconnect_clears_a_measurement_that_never_stopped() {
        use crate::objects::MeasureCategory;
        let mut client = authenticated();
        client.handle(frame(
            Command::CMD_MEASURE_START,
            vec![WppObject::MeasureCategory(MeasureCategory { value: 1 })],
        ));
        assert_eq!(client.measuring(), Some(1));

        client.handle(Event::Connected);
        assert_eq!(client.measuring(), None, "a new link has no measurement");

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

        client.handle(frame(
            Command::CMD_MEASURE_LIVE_DATA,
            vec![WppObject::MeasureLiveEcg(MeasureLiveEcg {
                samples: vec![0x82, 0xff, 0x10, 0x00],
            })],
        ));
        assert_eq!(client.live_samples(), &[-126, 16]);

        assert!(client.sync_now().is_empty(), "no walk during a measurement");
        assert!(client.walk_now().is_empty(), "not even an explicit one");
        assert!(client.force_refresh().is_empty(), "nor the daily totals");

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

    #[test]
    fn silence_after_a_request_asks_for_a_reconnect() {
        let mut client = authenticated();
        let tick = |ms: i64| Event::Tick {
            now: UnixMillis(ms),
        };

        assert!(client.handle(tick(10_000)).is_empty());

        client.handle(Event::Frame {
            frame: Frame::new(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]),
            received_at: UnixMillis(20_000),
        });

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

        client.handle(Event::Frame {
            frame: Frame::new(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]),
            received_at: UnixMillis(1_200_000),
        });
        assert!(client.handle(tick(1_300_000)).is_empty(), "it answered");
    }

    #[test]
    fn the_daily_totals_do_not_move_the_walk_backwards() {
        use crate::objects::{Steps, WamVasistasHead};
        let mut client = authenticated();

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

        client.handle(frame(
            Command::CMD_DISPLAYED_INFO_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 1 }),
                WppObject::Steps(Steps { value: 3169 }),
            ],
        ));
        assert_eq!(client.watermarks(), reached, "midnight must not rewind it");
    }

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

        client.handle(cbt(900_000));
        client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![WppObject::Null(Null {})],
        ));

        client.handle(cbt(2_000));
        client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![WppObject::Null(Null {})],
        ));

        let marks = client.watermarks();
        assert!(
            marks.contains(&(Category(6), UnixTime(900_001))),
            "the current stream resumes past its own newest record: {marks:?}"
        );
        assert!(
            marks.contains(&(Category(11), UnixTime(2_001))),
            "and the stream behind is not dragged forward with it: {marks:?}"
        );
    }

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

    #[test]
    fn a_factory_reset_is_the_bare_command() {
        let mut client = authenticated();
        let actions = client.factory_reset();
        let Action::Send(frame) = &actions[0] else {
            panic!("expected a send, got {actions:?}")
        };
        assert_eq!(frame.command, Command::CMD_FACTORY_RESET);
        assert!(frame.objects.is_empty());
    }

    #[test]
    fn a_scripted_session_reaches_the_expected_end_state() {
        use crate::objects::{Null, VasistasCbt, VasistasHeartrate, WamVasistasHead};

        let mut client = Client::new(credentials(), vec![(Category(8), UnixTime(4000))]);
        assert_eq!(client.phase(), Phase::Idle);

        let actions = client.handle(Event::Connected);
        assert_eq!(sent(&actions), vec![Command::CMD_PROBE]);
        assert_eq!(client.phase(), Phase::Probing);

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
        assert!(
            sent(&actions).is_empty(),
            "a frame of records is part of a reply already in flight"
        );
        assert_eq!(
            stored(&actions),
            vec![
                Record::Sample {
                    measured_at: UnixMillis(5_000_000),
                    kind: SampleKind::HeartRate,
                    value: 62,
                    quality: Some(4),
                    source: Source::Stored,
                    window_secs: None,
                    context: None,
                },
                Record::Sample {
                    measured_at: UnixMillis(5_000_000),
                    kind: SampleKind::CoreTemperature,
                    value: 37255,
                    quality: None,
                    source: Source::Stored,
                    window_secs: None,
                    context: Some(VasistasCbt::ATTRIB_NORMAL as i64),
                },
            ]
        );
        assert_eq!(client.current(), Some((Category(8), UnixTime(5001))));

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
        assert_eq!(client.watermarks(), vec![(Category(8), UnixTime(5001))]);
    }
}

#[cfg(test)]
mod workout_screen_tests {
    use super::tests::authenticated;
    use super::*;
    use crate::image::Mono;

    fn screen(id: u32, name: &str) -> WorkoutScreen {
        WorkoutScreen {
            id,
            name: name.to_string(),
            face_mode: 2,
            flag: 0,
            glyphs: vec![
                (0, Mono::pack(&vec![0xffff_ffff; 20 * 20], 20, 20)),
                (1, Mono::pack(&vec![0xffff_ffff; 34 * 34], 34, 34)),
            ],
        }
    }

    fn written(client: &Client, screens: &[WorkoutScreen]) -> Frame {
        let actions = client.set_activities(screens);
        let sent: Vec<&Frame> = actions
            .iter()
            .filter_map(|a| match a {
                Action::Send(f) if f.command == Command::CMD_WORKOUT_SCREEN_SET => Some(f),
                _ => None,
            })
            .collect();
        assert_eq!(sent.len(), 1, "the menu is one message, however it is sent");
        sent[0].clone()
    }

    #[test]
    fn no_frame_reaches_the_size_that_reboots_the_watch() {
        let client = authenticated();
        let frames = written(&client, &[screen(16, "Weights"), screen(2, "Running")]).to_wire();
        assert!(frames.len() > 1, "one frame would overflow the watch");
        for frame in &frames {
            let encoded = frame.to_bytes().len();
            assert!(
                encoded <= crate::frame::MAX_FRAME_BYTES,
                "frame of {encoded} bytes reboots the watch",
            );
        }
    }

    #[test]
    fn the_menu_carries_every_entry_and_closes_once() {
        let client = authenticated();
        let message = written(&client, &[screen(16, "Weights"), screen(2, "Running")]);
        let sent: Vec<u16> = message.objects.iter().map(|o| o.type_id()).collect();
        assert_eq!(
            sent.iter().filter(|t| **t == 317).count(),
            2,
            "one entry per screen and no filler",
        );
        assert_eq!(
            sent.iter().filter(|t| **t == 2397).count(),
            4,
            "both glyph sizes for both screens",
        );
        assert_eq!(
            sent.iter().filter(|t| **t == 2398).count(),
            2 * (1 + 3),
            "every chunk of every glyph survives",
        );
        assert_eq!(sent.iter().filter(|t| **t == 256).count(), 1);
        assert_eq!(sent.last().copied(), Some(256), "the list closes with Null");
    }

    #[test]
    fn splitting_keeps_every_object_in_order() {
        let client = authenticated();
        let message = written(&client, &[screen(16, "Weights"), screen(2, "Running")]);
        let split: Vec<WppObject> = message
            .to_wire()
            .into_iter()
            .flat_map(|f| f.objects)
            .collect();
        assert_eq!(split, message.objects);
    }
}
