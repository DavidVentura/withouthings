mod ancs;
mod dblib;
mod pipe;
mod update;

use std::env;
use std::fmt;
use std::io::{self, ErrorKind};
use std::process::ExitCode;
use std::time::{Duration, Instant};

use wpp::ancs::{Category, NotificationId};
use wpp::client::{probe_frame, time_set_frame, Action, Client, Event, Phase, Record};
use wpp::units::{UnixMillis, UnixTime};
use wpp::commands::Command;
use wpp::frame::{Channel, Frame, FrameError};
use wpp::objects::{
    ActivitySubcategory, Alarm, AncsStatus, Distance, InfoType, LocalNotification, MeasureCategory,
    MeasureLiveAppStatus, Pace, PauseState, ProbeReply, RawDataCmd, SleepActivityGet, Speed,
    StartTime, StoredSignalMeta, Uint32, VasistasType, Version, WamAutoSleep, WamVasistasGet,
    WorkoutGpsStatus,
};
use wpp::WppObject;

// The simulator's WPP pipe, one message per [len u16 big-endian][payload].
// Inbound a message is one GATT write to the WPP characteristic, outbound one
// notification, so both directions carry the byte stream a real link would.
const DEFAULT_ENDPOINT: &str = "127.0.0.1:7788";
const ATT_MTU: usize = 23;
const WRITE_LIMIT: usize = ATT_MTU - 3;
// A whole WPP frame in one write, as a phone that negotiated a larger MTU
// does. The update pushes a megabyte, so the twenty-byte split would be ten
// times the GATT writes for the same bytes.
const FRAME_PER_WRITE_LIMIT: usize = wpp::frame::MAX_FRAME_BYTES;
const REPLY_TIMEOUT: Duration = Duration::from_secs(120);
const QUIET: Duration = Duration::from_secs(20);
const SYNC_TIMEOUT: Duration = Duration::from_secs(300);
const SYNC_POLL: Duration = Duration::from_secs(1);
/// A whole ECG measurement: thirty seconds of the 300 Hz the watch reports in
/// the measurement's own StoredSignalMeta.
const ECG_SAMPLES: u32 = 30 * 300;
/// The subcategory the workout scenario starts by default. The watch reads it
/// as the sport, and running is the one that turns on every algorithm a
/// workout can run.
const ACTIVITY_SUBCATEGORY_RUNNING: i16 = 2;
/// A wall time for a scenario that did not ask for one, so the records a
/// workout or a sleep window writes carry a plausible timestamp rather than
/// the watch's uptime. 2025-09-18 09:00:00 UTC.
const DEFAULT_SCENARIO_TIME: u32 = 1758186000;
/// The bundle a notification comes from when the caller does not name one.
const DEFAULT_BUNDLE_ID: &str = "dev.davidv.withoutings";
/// How long the phone keeps answering attribute requests after the last one it
/// served. The watch asks for the app id, then the title, the subtitle and the
/// message, one request at a time, with a screen redraw in between.
const ANCS_QUIET: Duration = Duration::from_secs(10);
/// How long the watch is given to discover the service and subscribe to it,
/// which it does once, on the command that turns notifications on.
const ANCS_DISCOVERY: Duration = Duration::from_secs(30);

/// Why a run failed, which is what the exit code stands for.
#[derive(Debug)]
pub enum Failure {
    Usage(String),
    Setup(String),
    Link(io::Error),
    Silent(String),
    Refused(String),
    NotAuthenticated(String),
    Update(update::UpdateError),
    NotSubscribed,
    SyncUnfinished(String),
}

impl fmt::Display for Failure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Failure::Usage(reason) => write!(f, "{reason}\n{USAGE}"),
            Failure::Setup(reason) => write!(f, "{reason}"),
            Failure::Link(error) => write!(f, "the link failed: {error}"),
            Failure::Silent(label) => write!(f, "{label}: nothing within the timeout"),
            Failure::Refused(label) => write!(f, "{label}: the watch answered CMD_ERROR"),
            Failure::NotAuthenticated(reason) => write!(f, "not authenticated: {reason}"),
            Failure::Update(error) => write!(f, "update failed: {error}"),
            Failure::NotSubscribed => write!(f, "the watch never subscribed to ANCS"),
            Failure::SyncUnfinished(reason) => write!(f, "the sync did not finish: {reason}"),
        }
    }
}

impl From<io::Error> for Failure {
    fn from(error: io::Error) -> Failure {
        Failure::Link(error)
    }
}

const USAGE: &str = "usage: wpp-sim-client [--endpoint host:port | --port n] --secret-from-dump <external_flash.bin> [--set-time <unix>] [--probe-only | --ecg <seconds> | --spo2 <seconds> | --ppg <seconds> | --body-temp | --raw-data | --send <command>... | --stored <type>... | --sync | --update <package> [--version-address <hex>] | [--workout <seconds> [--activity <n>]] [--hr-measure <seconds>] [--alarm] [--notify] [--notification \"<title>|<message>\" [--app <bundle id>]] [--dismiss <id>] [--sleep <seconds>]]";

/// How much of a frame one write to the pipe carries.
#[derive(Clone, Copy)]
enum WriteSplit {
    AttMtu,
    WholeFrame,
}

impl WriteSplit {
    fn limit(self) -> usize {
        match self {
            WriteSplit::AttMtu => WRITE_LIMIT,
            WriteSplit::WholeFrame => FRAME_PER_WRITE_LIMIT,
        }
    }
}

pub struct Link {
    pipe: pipe::Pipe,
    received: Vec<u8>,
    write_limit: usize,
}

impl Link {
    fn connect(endpoint: &str, split: WriteSplit) -> io::Result<Link> {
        Ok(Link {
            pipe: pipe::Pipe::connect(endpoint)?,
            received: Vec::new(),
            write_limit: split.limit(),
        })
    }

    pub fn send(&mut self, frame: &Frame) -> io::Result<()> {
        println!("-> {:?} {:?}", frame.command.opcode_name(), frame.objects);
        self.send_quietly(frame)
    }

    pub fn send_quietly(&mut self, frame: &Frame) -> io::Result<()> {
        for part in frame.to_wire().expect("the client sends objects a frame can hold") {
            for write in part.to_bytes().chunks(self.write_limit) {
                self.pipe.write_message(write)?;
            }
        }
        Ok(())
    }

    fn receive(&mut self, timeout: Duration) -> io::Result<Option<Frame>> {
        let deadline = Instant::now() + timeout;
        loop {
            match Frame::parse_prefix(&self.received) {
                Ok((frame, rest)) => {
                    let consumed = self.received.len() - rest.len();
                    self.received.drain(..consumed);
                    return Ok(Some(frame));
                }
                // The watch notifies in MTU-sized pieces, so this is a reply
                // still arriving; anything else will not parse however long
                // it is waited for.
                Err(FrameError::ShortHeader { .. } | FrameError::IncompletePayload { .. }) => {}
                Err(error) => return Err(io::Error::new(ErrorKind::InvalidData, error)),
            }
            let left = deadline.saturating_duration_since(Instant::now());
            let Some(notification) = self.pipe.read_message(left)? else {
                return Ok(None);
            };
            self.received.extend_from_slice(&notification);
        }
    }

    /// Reads the next frame that is an answer rather than watch-initiated
    /// chatter, answering the chatter as a phone would so the watch stops
    /// repeating it.
    pub fn next_answer(&mut self, deadline: Instant) -> io::Result<Option<Frame>> {
        loop {
            let Some(frame) = self.next_frame(deadline)? else {
                return Ok(None);
            };
            if frame.command.channel() == Some(Channel::SlaveRequest) {
                continue;
            }
            return Ok(Some(frame));
        }
    }

    /// The same read with the watch-initiated frames kept rather than skipped.
    /// A measurement is driven from the watch: the phone asks for it once and
    /// everything after that arrives on the slave channel, so a scenario that
    /// wants the measurement has to see those frames and not only acknowledge
    /// them.
    fn next_frame(&mut self, deadline: Instant) -> std::io::Result<Option<Frame>> {
        // The deadline is absolute because answering chatter must not push the
        // wait for the answer out again.
        let Some(left) = deadline.checked_duration_since(Instant::now()) else {
            return Ok(None);
        };
        let Some(frame) = self.receive(left)? else {
            return Ok(None);
        };
        if frame.command.channel() == Some(Channel::SlaveRequest) {
            println!("   [slave request] {:?} {:?}", frame.command.opcode_name(), frame.objects);
            let echo = Frame::new(
                frame.command.opcode().with_channel(Channel::SlaveRequest),
                Vec::new(),
            );
            self.send(&echo)?;
        }
        Ok(Some(frame))
    }
}

/// How many answers a request draws: most commands answer once, the vasistas
/// walks stream frames of the same opcode until they run out.
#[derive(Clone, Copy)]
enum Answers {
    One,
    UntilQuiet,
}

struct Step {
    label: &'static str,
    frame: Frame,
    answers: Answers,
}

/// What the watch made of a request it answered.
#[derive(Clone, Copy, PartialEq)]
enum Answered {
    Took,
    Refused,
}

fn read_only_steps() -> Vec<Step> {
    let simple = [
        ("battery status", Command::CMD_BATTERY_STATUS, Vec::new()),
        ("battery percent", Command::CMD_BATTERY_PERCENT, Vec::new()),
        ("time", Command::CMD_TIME_GET, Vec::new()),
        ("time counters", Command::CMD_TIME_COUNTERS_GET, Vec::new()),
        (
            "daily totals",
            Command::CMD_DISPLAYED_INFO_GET,
            vec![WppObject::InfoType(InfoType { value: 4 })],
        ),
        ("screen settings", Command::CMD_SCREEN_SETTINGS_GET, Vec::new()),
        ("home screen", Command::CMD_GET_HOME_SCREEN, Vec::new()),
        ("wear position", Command::CMD_GET_TRACKER_WEAR_POS, Vec::new()),
        ("tracker user", Command::CMD_TRACKER_USER_GET, Vec::new()),
        ("locale", Command::CMD_LOCALE_GET, Vec::new()),
        ("feature mask", Command::CMD_FEATURE_MASK_GET, Vec::new()),
        ("alarm settings", Command::CMD_GET_ALARM_SETTINGS, Vec::new()),
        ("ble info", Command::CMD_BLE_INFO, Vec::new()),
        ("workout status", Command::CMD_WORKOUT_STATUS, Vec::new()),
        ("light sensor", Command::CMD_GET_LIGHT_SENSOR, Vec::new()),
        ("environment measure", Command::CMD_GET_RT_ENV_MEASURE, Vec::new()),
        ("pressure/temperature", Command::CMD_GET_PRESSURE_TEMPERATURE, Vec::new()),
        ("heart rate", Command::CMD_GET_HR, Vec::new()),
        ("live heart rate", Command::CMD_GET_LIVE_HR, Vec::new()),
        ("display prefs", Command::CMD_DISPLAY_PREFS_GET, Vec::new()),
        ("luminosity", Command::CMD_GET_LUMINOSITY_LEVEL, Vec::new()),
    ];
    let mut steps: Vec<Step> = simple
        .into_iter()
        .map(|(label, command, objects)| Step {
            label,
            frame: Frame::new(command, objects),
            answers: Answers::One,
        })
        .collect();
    // From the epoch: the walks are the phone's watermarked sync, and with no
    // watermark the watch answers from the oldest record it still holds.
    let window = WppObject::WamVasistasGet(WamVasistasGet { utc_start: 0, max: 8 });
    steps.push(Step {
        label: "heart-rate series (body vasistas)",
        frame: Frame::new(Command::CMD_BODY_VASISTAS_GET, vec![window.clone()]),
        answers: Answers::UntilQuiet,
    });
    steps.push(Step {
        label: "activity series (wam vasistas)",
        frame: Frame::new(
            Command::CMD_WAM_VASISTAS_GET,
            vec![
                window.clone(),
                WppObject::Version(Version { value: Version::VALUE_ACTI_RECO_V3 }),
            ],
        ),
        answers: Answers::UntilQuiet,
    });
    steps.push(Step {
        label: "HRV series (vasistas type 11)",
        frame: Frame::new(
            Command::CMD_VASISTAS_GET,
            vec![window, WppObject::VasistasType(VasistasType { value: 11 })],
        ),
        answers: Answers::UntilQuiet,
    });
    steps
}

/// A watch that says nothing is a failure whatever was asked. What a refusal
/// means is up to the caller, so it comes back as the answer it is.
fn exchange(link: &mut Link, step: &Step) -> Result<Answered, Failure> {
    println!("\n== {} ==", step.label);
    link.send(&step.frame)?;
    let opcode = step.frame.command.opcode();
    loop {
        let Some(reply) = link.next_answer(Instant::now() + REPLY_TIMEOUT)? else {
            return Err(Failure::Silent(step.label.to_string()));
        };
        report(&reply);
        if reply.command.opcode() == Command::CMD_ERROR {
            return Ok(Answered::Refused);
        }
        match step.answers {
            Answers::One => return Ok(Answered::Took),
            Answers::UntilQuiet if reply.command.opcode() == opcode => {
                // The walk ends with a quiet link rather than a marker frame.
                while let Some(more) = link.next_answer(Instant::now() + QUIET)? {
                    report(&more);
                }
                return Ok(Answered::Took);
            }
            Answers::UntilQuiet => {}
        }
    }
}

/// A request the scenario depends on: a refusal fails the run.
fn request(
    link: &mut Link,
    label: &'static str,
    command: Command,
    objects: Vec<WppObject>,
) -> Result<(), Failure> {
    let step = Step { label, frame: Frame::new(command, objects), answers: Answers::One };
    match exchange(link, &step)? {
        Answered::Took => Ok(()),
        Answered::Refused => Err(Failure::Refused(label.to_string())),
    }
}

/// A request sent to see what the watch does with it, so a refusal is the
/// measurement rather than a failure.
fn ask(
    link: &mut Link,
    label: &'static str,
    command: Command,
    objects: Vec<WppObject>,
) -> Result<(), Failure> {
    let step = Step { label, frame: Frame::new(command, objects), answers: Answers::One };
    exchange(link, &step).map(|_| ())
}

/// The poll the official app makes after every sync, one per stored signal
/// type: the handler at 0x6e21c serves a type only when the request names it,
/// and an empty store answers Null.
fn stored(link: &mut Link, signal_type: u16) -> Result<(), Failure> {
    let step = Step {
        label: "stored measure signal",
        frame: Frame::new(
            Command::CMD_STORED_MEASURE_SIGNAL_GET,
            vec![WppObject::StoredSignalMeta(StoredSignalMeta {
                r#type: signal_type,
                ..StoredSignalMeta::default()
            })],
        ),
        answers: Answers::UntilQuiet,
    };
    exchange(link, &step).map(|_| ())
}

/// A sync as the app runs it: the phone-side `Client` drives the link from the
/// probe to the end, and every record it hands over for keeping is printed as a
/// `stored` line. With no series to walk, what it exercises is the handshake
/// and the stores drained after the walk.
fn sync(link: &mut Link, association: dblib::Association) -> Result<(), Failure> {
    let mut client = Client::new(association.credentials(), Vec::new(), Vec::new());
    let mut pending: std::collections::VecDeque<Action> = client.handle(Event::Connected { now: wall_clock() }).into();
    let deadline = Instant::now() + SYNC_TIMEOUT;
    while Instant::now() < deadline {
        while let Some(action) = pending.pop_front() {
            match action {
                Action::Send(frame) | Action::Delete(frame) => link.send(&frame)?,
                Action::Store { token, records } => {
                    for record in &records {
                        println!("stored {}", describe(record));
                    }
                    pending.extend(client.handle(Event::Stored { token }));
                }
                Action::Finished => {
                    if client.phase() == Phase::Finished && client.pending_deletes() == 0 {
                        println!("sync finished");
                        return Ok(());
                    }
                }
                Action::Reconnect => {
                    return Err(Failure::SyncUnfinished("the client gave up on a silent watch".to_string()));
                }
            }
        }
        let now = wall_clock();
        pending.extend(match link.receive(SYNC_POLL)? {
            Some(frame) => {
                report(&frame);
                client.handle(Event::Frame { frame, received_at: now })
            }
            None => client.handle(Event::Tick { now }),
        });
    }
    Err(Failure::SyncUnfinished(format!("still running after {}s", SYNC_TIMEOUT.as_secs())))
}

fn wall_clock() -> UnixMillis {
    UnixMillis(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64,
    )
}

fn describe(record: &Record) -> String {
    let measures = |signal: &wpp::signal::Signal| {
        signal
            .measures
            .iter()
            .map(|m| format!("{}={}", m.r#type, m.value))
            .collect::<Vec<_>>()
            .join(",")
    };
    let uid = |signal: &wpp::signal::Signal| match &signal.measure {
        Some(measure) => measure.uid.to_string(),
        None => "none".to_string(),
    };
    match record {
        Record::PpgAfib { measured_at, signal } => format!(
            "ppg_afib measured_at={} uid={} measures={} samples={}",
            measured_at.0,
            uid(signal),
            measures(signal),
            signal.data.len(),
        ),
        Record::Spo2Check { measured_at, signal } => format!(
            "spo2_check measured_at={} uid={} measures={}",
            measured_at.0,
            uid(signal),
            measures(signal),
        ),
        Record::Ecg { signal, .. } => format!("ecg uid={} samples={}", uid(signal), signal.data.len()),
        other => format!("{other:?}"),
    }
}

/// MEASURE_START says the phone has the live screen up and is therefore ready
/// for the sample frames. The request objects are the two the handler at
/// 0x54238 parses -- MeasureCategory, which is what says ECG rather than PPG or
/// SpO2, and MeasureLiveAppStatus.
fn start_measuring(link: &mut Link, label: &'static str, category: &WppObject) -> Result<(), Failure> {
    ask(
        link,
        label,
        Command::CMD_MEASURE_START,
        vec![
            category.clone(),
            WppObject::MeasureLiveAppStatus(MeasureLiveAppStatus { app_live_screen_displayed: 1 }),
        ],
    )
}

/// One ECG acquisition: ask for it, watch it run, stop it.
///
/// MEASURE_START and MEASURE_STOP are the only two commands the phone sends;
/// everything between them the watch pushes by itself on the slave channel, so
/// the middle of this is a read loop and not a request.
fn ecg(link: &mut Link, seconds: u64) -> Result<(), Failure> {
    let category =
        WppObject::MeasureCategory(MeasureCategory { value: MeasureCategory::VALUE_ECG });
    start_measuring(link, "ecg start", &category)?;
    // MEASURE_START is the phone saying it will take the live data; it does not
    // start anything, because on the watch an ECG is started by the wearer
    // holding the crown. CMD_ECG_TEST is the command that stands in for that,
    // and the watch answers it by sending its own MEASURE_START back with the
    // signal's real metadata. Its one object is how many samples to take, and
    // the number matters: the classifier asserts unless it is handed the whole
    // measurement, which at the 300 Hz the metadata reports is thirty seconds.
    // Without the object the handler logs that it could not read the count and
    // falls back to a thousand, and the run ends in that assert.
    request(
        link,
        "ecg run",
        Command::CMD_ECG_TEST,
        vec![WppObject::Uint32(Uint32 { val: ECG_SAMPLES })],
    )?;

    let until = Instant::now() + Duration::from_secs(seconds);
    let mut samples = 0usize;
    watch(
        link,
        "ecg live",
        until,
        |frame| {
            for object in &frame.objects {
                if let WppObject::MeasureLiveEcg(live) = object {
                    samples += live.samples.len();
                }
            }
        },
        |_, _| Ok(()),
    )?;
    println!("   {samples} ECG sample bytes");

    ask(link, "ecg stop", Command::CMD_MEASURE_STOP, vec![category])
}

/// One live measurement of a category other than ECG, driven exactly the way
/// the ECG one is: the two objects the handler at 0x54238 parses, a read loop,
/// and the stop.
///
/// The category is what the measurement is, and the switch that reads it is
/// `measure_live_config_get` at 0x54028: it answers for 1 (ECG) and for 3
/// (SpO2) and logs `[MEASURE_LIVE] Unknown measure subcat %d` for everything
/// else, so a run of this flag with a category the firmware does not admit is
/// the measurement of that refusal and not a mistake.
fn measure(link: &mut Link, category: i16, seconds: u64) -> Result<(), Failure> {
    let object = WppObject::MeasureCategory(MeasureCategory { value: category });
    let name = MeasureCategory::value_name(category).unwrap_or("unknown");
    start_measuring(link, "measure start", &object)?;
    let until = Instant::now() + Duration::from_secs(seconds);
    watch(link, &format!("{name} live"), until, |_| {}, |_, _| Ok(()))?;
    ask(link, "measure stop", Command::CMD_MEASURE_STOP, vec![object])
}

/// The temperature the phone can ask for: the skin sensor, the heat-flux
/// sensor the core-body-temperature algorithm integrates, the barometer's own
/// temperature, and the greenTEG calibration the algorithm reads.
fn body_temperature(link: &mut Link) -> Result<(), Failure> {
    for (label, command) in [
        ("skin temperature", Command::CMD_SKIN_TEMPERATURE_MEASURE),
        ("heat flux", Command::CMD_FLUX_SENSOR_MEASURE),
        (
            "pressure and temperature",
            Command::CMD_GET_PRESSURE_TEMPERATURE,
        ),
        (
            "greenteg sensitivity bin",
            Command::CMD_GREENTEG_SENSITIVITY_BIN_GET,
        ),
        (
            "greenteg integration factor",
            Command::CMD_GREENTEG_INTEGRATION_FACTOR_GET,
        ),
        ("mcu temperature calibration", Command::CMD_MCU_TEMP_CAL_GET),
    ] {
        ask(link, label, command, Vec::new())?;
    }
    Ok(())
}

/// The raw-data capture the watch keeps in its external flash.
///
/// The handler at 0x58b34 reads one `RawDataCmd` and accepts 1 (erase), 2
/// (erase all) and 3 (mark synced); the two erases are what the phone sends
/// once it has the bytes, so only the marking is driven here and the capture
/// itself is started from the debug shell.
fn raw_data(link: &mut Link) -> Result<(), Failure> {
    for (label, cmd) in [
        ("raw data, mark synced", RawDataCmd::CMD_SET_SYNCED),
        ("raw data, no command", RawDataCmd::CMD_NONE),
    ] {
        ask(link, label, Command::CMD_RAW_DATA, vec![WppObject::RawDataCmd(RawDataCmd { cmd })])?;
    }
    Ok(())
}

/// How often a live scenario pushes the phone's own updates while it waits.
const LIVE_TICK: Duration = Duration::from_secs(5);

/// Everything the watch says between two commands, decoded.
///
/// A feature the phone starts and stops runs on the watch in between, and the
/// frames it pushes on the slave channel are the only report of it, so a
/// scenario's middle is a read loop with the phone's periodic writes folded in
/// rather than a request and an answer.
fn watch(
    link: &mut Link,
    label: &str,
    until: Instant,
    mut on_frame: impl FnMut(&Frame),
    mut tick: impl FnMut(&mut Link, u32) -> io::Result<()>,
) -> io::Result<()> {
    println!("\n== {label} ==");
    let mut frames = 0usize;
    let mut ticks = 0u32;
    while Instant::now() < until {
        let next = (Instant::now() + LIVE_TICK).min(until);
        while let Some(frame) = link.next_frame(next)? {
            frames += 1;
            on_frame(&frame);
            if frame.command.channel() != Some(Channel::SlaveRequest) {
                report(&frame);
            }
        }
        tick(link, ticks)?;
        ticks += 1;
    }
    println!("   {frames} frames from the watch over {label}");
    Ok(())
}

/// One workout, driven the way the phone drives it.
///
/// WORKOUT_START carries the three objects its handler at 0x6a864 parses --
/// StartTime, ActivitySubcategory and PauseState -- and the subcategory is what
/// says which sport, so it is what selects the algorithms the watch runs. The
/// phone owns the GPS, so what it sends during the workout is the fix's state
/// through WORKOUT_GPS_STATUS and the distance, pace and speed it derives
/// through WORKOUT_LIVE_DATA; the protocol has no coordinate object.
fn workout(link: &mut Link, seconds: u64, subcategory: i16, now: i32) -> Result<(), Failure> {
    request(
        link,
        "workout start",
        Command::CMD_WORKOUT_START,
        vec![
            WppObject::StartTime(StartTime { value: now }),
            WppObject::ActivitySubcategory(ActivitySubcategory { value: subcategory }),
            WppObject::PauseState(PauseState { value: 0, start_time: 0, sum: 0 }),
        ],
    )?;
    request(link, "workout status", Command::CMD_WORKOUT_STATUS, Vec::new())?;
    // A fix takes a moment on a phone too: the first status is searching and
    // the rest are locked, which is the transition the watch's GPS screen reads.
    request(
        link,
        "gps searching",
        Command::CMD_WORKOUT_GPS_STATUS,
        vec![WppObject::WorkoutGpsStatus(WorkoutGpsStatus { status: 1 })],
    )?;

    let until = Instant::now() + Duration::from_secs(seconds);
    watch(link, "workout live", until, |_| {}, |link, tick| {
        // Five metres a second is a run, and the pace is the reciprocal in the
        // seconds per kilometre the object carries.
        let metres = 5 * (tick as i32 + 1) * LIVE_TICK.as_secs() as i32;
        link.send(&Frame::new(
            Command::CMD_WORKOUT_GPS_STATUS,
            vec![WppObject::WorkoutGpsStatus(WorkoutGpsStatus { status: 2 })],
        ))?;
        link.send(&Frame::new(
            Command::CMD_WORKOUT_LIVE_DATA,
            vec![
                WppObject::Distance(Distance { value: metres }),
                WppObject::Pace(Pace { value: 200 }),
                WppObject::Speed(Speed { value: 5000 }),
            ],
        ))
    })?;

    request(link, "workout stop", Command::CMD_WORKOUT_STOP, Vec::new())?;
    request(link, "workout status, after", Command::CMD_WORKOUT_STATUS, Vec::new())
}

/// A manual heart-rate measurement: the one the watch face's heart-rate tile
/// starts. The firmware refuses parts of this, and what it does regardless is
/// what the run is for, so a refusal does not end it.
///
/// The handler at 0x624e4 reads a single Uint32 and nothing else: zero starts
/// the measurement and one stops it, and everything in between arrives as live
/// heart rate on the slave channel.
fn hr_measure(link: &mut Link, seconds: u64) -> Result<(), Failure> {
    ask(link, "hr measure start", Command::CMD_HR_MEASURE, vec![WppObject::Uint32(Uint32 { val: 0 })])?;
    let until = Instant::now() + Duration::from_secs(seconds);
    watch(link, "hr measure live", until, |_| {}, |link, _| {
        link.send(&Frame::new(Command::CMD_GET_LIVE_HR, Vec::new()))
    })?;
    ask(link, "hr measure stop", Command::CMD_HR_MEASURE, vec![WppObject::Uint32(Uint32 { val: 1 })])?;
    ask(link, "heart rate, after", Command::CMD_GET_HR, Vec::new())
}

/// An alarm the phone sets, and the settings read back.
fn alarm(link: &mut Link) -> Result<(), Failure> {
    request(
        link,
        "set alarm",
        Command::CMD_SET_ALARM,
        vec![WppObject::Alarm(Alarm {
            hour: 7,
            minute: 30,
            wday: 0x7f,
            mday: 0,
            month: 0,
            year: 0,
            span: 0,
        })],
    )?;
    request(link, "alarm read back", Command::CMD_GET_ALARM, Vec::new())?;
    request(link, "alarm settings", Command::CMD_GET_ALARM_SETTINGS, Vec::new())
}

/// Sleep: the watch decides it itself, so the phone's part is to turn automatic
/// sleep on and then leave the watch alone. The scenario is the quiet that
/// follows, which is what the sleep-wake algorithm reads, and it runs whether
/// or not the firmware admits the command that asked for it.
fn sleep(link: &mut Link, seconds: u64, now: i32) -> Result<(), Failure> {
    ask(
        link,
        "auto sleep on",
        Command::CMD_WAM_AUTO_SLEEP,
        vec![WppObject::WamAutoSleep(WamAutoSleep { auto_sleep: 1 })],
    )?;
    ask(link, "auto sleep read back", Command::CMD_WAM_AUTO_SLEEP_GET, Vec::new())?;
    let until = Instant::now() + Duration::from_secs(seconds);
    watch(link, "still and worn", until, |_| {}, |_, _| Ok(()))?;
    ask(
        link,
        "sleep activity",
        Command::CMD_SLEEP_ACTIVITY_GET,
        vec![WppObject::SleepActivityGet(SleepActivityGet { from_utc: now - 86400 })],
    )
}

/// A notification push. ANCS is the phone's own service and the sim has no
/// radio, so what is reachable over the pipe is the local-event path the watch
/// exposes for the same purpose.
fn notify(link: &mut Link) -> Result<(), Failure> {
    ask(
        link,
        "notification push",
        Command::CMD_LOCAL_EVENT_NOTIFY,
        vec![WppObject::LocalNotification(LocalNotification { id: 1, status: 1 })],
    )?;
    ask(link, "notification read back", Command::CMD_NOTIFICATION_GET, Vec::new())
}

/// The ANCS scenario: turn the watch's notification client on, announce what
/// the caller asked for, and serve the attribute requests the watch writes
/// back. The ANCS channel is the WPP port plus one, which is how the pipe lays
/// its two listeners out.
///
/// The socket is opened before the command that turns ANCS on, because the
/// pipe's ANCS connection is what stands for a bonded phone and the watch
/// refuses to discover anything without one.
fn announce(link: &mut Link, endpoint: &str, announcement: &Announcement) -> Result<(), Failure> {
    let (host, port) = endpoint
        .rsplit_once(':')
        .ok_or_else(|| Failure::Usage(format!("{endpoint} is not host:port")))?;
    let port: u16 = port.parse().map_err(|_| Failure::Usage(format!("{endpoint} is not host:port")))?;
    let ancs_endpoint = format!("{host}:{}", port + 1);
    let mut phone = ancs::Phone::connect(&ancs_endpoint)?;
    println!("[ancs] connected to {ancs_endpoint}");
    request(
        link,
        "notifications on",
        Command::CMD_REMOTE_NOTIFICATIONS_CONFIG_SET,
        vec![WppObject::AncsStatus(AncsStatus { status: 1 })],
    )?;
    if !phone.subscribed(ANCS_DISCOVERY)? {
        return Err(Failure::NotSubscribed);
    }
    for (title, message) in &announcement.notifications {
        phone.post(&announcement.bundle_id, title, message, Category::Social)?;
        let served = phone.serve(ANCS_QUIET)?;
        println!("[ancs] served {served} attribute requests");
    }
    // What the watch says about a notification it now holds, which is the half
    // of the notification path WPP owns.
    ask(link, "notification get", Command::CMD_NOTIFICATION_GET, Vec::new())?;
    ask(
        link,
        "local event notify",
        Command::CMD_LOCAL_EVENT_NOTIFY,
        vec![WppObject::LocalNotification(LocalNotification { id: 1, status: 1 })],
    )?;
    for id in &announcement.dismissals {
        phone.dismiss(NotificationId(*id))?;
        let served = phone.serve(ANCS_QUIET)?;
        println!("[ancs] served {served} attribute requests after the dismissal");
    }
    Ok(())
}

pub fn report(frame: &Frame) {
    println!("<- {:?}", frame.command.opcode_name());
    for object in &frame.objects {
        println!("   {object:?}");
    }
}

/// The firmware version is in the probe reply and nowhere else, so the reply
/// is what a caller wants back rather than a yes/no.
fn authenticate(link: &mut Link, association: dblib::Association) -> Result<ProbeReply, Failure> {
    link.send(&probe_frame())?;
    let Some(challenge_frame) = link.next_answer(Instant::now() + REPLY_TIMEOUT)? else {
        return Err(Failure::Silent("probe".to_string()));
    };
    report(&challenge_frame);
    let Some(challenge) = challenge_frame.objects.iter().find_map(|o| match o {
        WppObject::ProbeChallenge(c) => Some(c.clone()),
        _ => None,
    }) else {
        // Answering a watch that does not challenge means associating with it,
        // which this client deliberately does not do.
        return Err(Failure::NotAuthenticated(
            "the watch did not challenge; nothing to authenticate against".to_string(),
        ));
    };
    let credentials = association.credentials();
    let identity = challenge.mac.to_ascii_lowercase();
    if identity != credentials.mac().as_str() {
        return Err(Failure::NotAuthenticated(format!(
            "the watch's identity {identity} is not the one the dump holds a secret for"
        )));
    }
    link.send(&credentials.challenge_reply(&challenge))?;
    let Some(reply) = link.next_answer(Instant::now() + REPLY_TIMEOUT)? else {
        return Err(Failure::Silent("challenge response".to_string()));
    };
    report(&reply);
    if reply.command.opcode() != Command::CMD_PROBE {
        return Err(Failure::NotAuthenticated("the watch refused the challenge response".to_string()));
    }
    reply
        .objects
        .iter()
        .find_map(|o| match o {
            WppObject::ProbeReply(r) => Some(r.clone()),
            _ => None,
        })
        .ok_or_else(|| Failure::NotAuthenticated("the probe answer carries no reply".to_string()))
}

/// The notifications to announce over ANCS, each a title and a message, and the
/// ids to take back afterwards.
#[derive(Debug, PartialEq)]
struct Announcement {
    bundle_id: String,
    notifications: Vec<(String, String)>,
    dismissals: Vec<u32>,
}

/// The scenarios a run on the watch's clock is made of, in the order they run.
#[derive(Debug, PartialEq, Default)]
struct Clocked {
    workout: Option<(u64, i16)>,
    hr_seconds: Option<u64>,
    alarm: bool,
    notify: bool,
    announcement: Option<Announcement>,
    sleep_seconds: Option<u64>,
}

/// What one run does. Each of these is a whole run: anything that could be
/// combined with something else lives inside `Clocked`.
#[derive(Debug, PartialEq)]
enum Scenario {
    Survey,
    ProbeOnly,
    Sync,
    Update { package: String, trailer: update::VersionTrailer },
    Ecg { seconds: u64 },
    Measure { category: i16, seconds: u64 },
    BodyTemperature,
    RawData,
    Stored { types: Vec<u16> },
    Send { commands: Vec<u16> },
    Clocked(Clocked),
}

#[derive(Debug, PartialEq)]
struct Invocation {
    endpoint: String,
    dump_path: String,
    set_time: Option<u32>,
    scenario: Scenario,
}

fn value<T: std::str::FromStr>(
    arguments: &mut impl Iterator<Item = String>,
    flag: &str,
    what: &str,
) -> Result<T, Failure> {
    arguments
        .next()
        .and_then(|text| text.parse().ok())
        .ok_or_else(|| Failure::Usage(format!("{flag} takes {what}")))
}

fn parse_arguments(arguments: impl IntoIterator<Item = String>) -> Result<Invocation, Failure> {
    const SECONDS: &str = "a number of seconds";
    let mut endpoint = DEFAULT_ENDPOINT.to_string();
    let mut dump_path: Option<String> = None;
    let mut set_time: Option<u32> = None;
    let mut activity: Option<i16> = None;
    let mut bundle_id: Option<String> = None;
    let mut notifications: Vec<(String, String)> = Vec::new();
    let mut dismissals: Vec<u32> = Vec::new();
    let mut version_address: Option<update::VersionTrailer> = None;
    let mut clocked = Clocked::default();
    let mut workout_seconds: Option<u64> = None;
    // A bare command by number, for asking the watch what it does with one.
    // Removing a command from the dispatch table is only half an answer; what
    // the phone sees is the other half, and nothing else here can send a
    // command the scenario does not already use.
    let mut send_commands: Vec<u16> = Vec::new();
    let mut stored_types: Vec<u16> = Vec::new();
    let mut whole_runs: Vec<(String, Scenario)> = Vec::new();

    let mut arguments = arguments.into_iter();
    while let Some(argument) = arguments.next() {
        let flag = argument.as_str();
        match flag {
            "--endpoint" => endpoint = value(&mut arguments, flag, "an address")?,
            "--secret-from-dump" => dump_path = Some(value(&mut arguments, flag, "a path")?),
            "--set-time" => set_time = Some(value(&mut arguments, flag, "a unix timestamp")?),
            "--update" => {
                let package = value(&mut arguments, flag, "a package path")?;
                whole_runs.push((
                    flag.to_string(),
                    Scenario::Update { package, trailer: update::VersionTrailer::STOCK },
                ));
            }
            // Where the image in the package reads its own version. It is the
            // stock address unless a layout moved the trailer's section, and
            // tools/mkpkg.py prints the one it wrote to.
            "--version-address" => {
                let text: String = value(&mut arguments, flag, "an address")?;
                let parsed = usize::from_str_radix(text.trim_start_matches("0x"), 16)
                    .map_err(|_| Failure::Usage(format!("{flag} takes a hexadecimal address")))?;
                let trailer = update::VersionTrailer::at(parsed)
                    .map_err(|reason| Failure::Usage(format!("{flag} {text}: {reason}")))?;
                version_address = Some(trailer);
            }
            // The pipe is on localhost and a run that shares the host with
            // another run differs from it only in the port.
            "--port" => {
                let port: u16 = value(&mut arguments, flag, "a port")?;
                endpoint = format!("127.0.0.1:{port}");
            }
            "--probe-only" => whole_runs.push((flag.to_string(), Scenario::ProbeOnly)),
            "--sync" => whole_runs.push((flag.to_string(), Scenario::Sync)),
            "--ecg" => {
                let seconds = value(&mut arguments, flag, SECONDS)?;
                whole_runs.push((flag.to_string(), Scenario::Ecg { seconds }));
            }
            "--spo2" => {
                let seconds = value(&mut arguments, flag, SECONDS)?;
                whole_runs.push((
                    flag.to_string(),
                    Scenario::Measure { category: MeasureCategory::VALUE_SPO2, seconds },
                ));
            }
            "--ppg" => {
                let seconds = value(&mut arguments, flag, SECONDS)?;
                whole_runs.push((
                    flag.to_string(),
                    Scenario::Measure { category: MeasureCategory::VALUE_PPG, seconds },
                ));
            }
            "--body-temp" => whole_runs.push((flag.to_string(), Scenario::BodyTemperature)),
            "--raw-data" => whole_runs.push((flag.to_string(), Scenario::RawData)),
            "--workout" => workout_seconds = Some(value(&mut arguments, flag, SECONDS)?),
            "--activity" => activity = Some(value(&mut arguments, flag, "a subcategory")?),
            "--hr-measure" => clocked.hr_seconds = Some(value(&mut arguments, flag, SECONDS)?),
            "--sleep" => clocked.sleep_seconds = Some(value(&mut arguments, flag, SECONDS)?),
            "--alarm" => clocked.alarm = true,
            "--notify" => clocked.notify = true,
            "--notification" => {
                const SHAPE: &str = "\"<title>|<message>\"";
                let text: String = value(&mut arguments, flag, SHAPE)?;
                let (title, message) = text
                    .split_once('|')
                    .ok_or_else(|| Failure::Usage(format!("{flag} takes {SHAPE}, got {text:?}")))?;
                notifications.push((title.to_string(), message.to_string()));
            }
            "--app" => bundle_id = Some(value(&mut arguments, flag, "a bundle id")?),
            "--dismiss" => dismissals.push(value(&mut arguments, flag, "a notification id")?),
            "--send" => send_commands.push(value(&mut arguments, flag, "a command number")?),
            "--stored" => stored_types.push(value(&mut arguments, flag, "a stored signal type")?),
            other => return Err(Failure::Usage(format!("unknown argument {other}"))),
        }
    }

    if activity.is_some() && workout_seconds.is_none() {
        return Err(Failure::Usage("--activity qualifies --workout".to_string()));
    }
    clocked.workout =
        workout_seconds.map(|seconds| (seconds, activity.unwrap_or(ACTIVITY_SUBCATEGORY_RUNNING)));

    if bundle_id.is_some() && notifications.is_empty() {
        return Err(Failure::Usage("--app qualifies --notification".to_string()));
    }
    if !notifications.is_empty() || !dismissals.is_empty() {
        clocked.announcement = Some(Announcement {
            bundle_id: bundle_id.unwrap_or_else(|| DEFAULT_BUNDLE_ID.to_string()),
            notifications,
            dismissals,
        });
    }
    if clocked != Clocked::default() {
        whole_runs.push(("the clocked scenarios".to_string(), Scenario::Clocked(clocked)));
    }
    if !send_commands.is_empty() {
        whole_runs.push(("--send".to_string(), Scenario::Send { commands: send_commands }));
    }
    if !stored_types.is_empty() {
        whole_runs.push(("--stored".to_string(), Scenario::Stored { types: stored_types }));
    }
    if whole_runs.len() > 1 {
        let named: Vec<&str> = whole_runs.iter().map(|(name, _)| name.as_str()).collect();
        return Err(Failure::Usage(format!("{} cannot share a run", named.join(" and "))));
    }
    let mut scenario = whole_runs.pop().map_or(Scenario::Survey, |(_, scenario)| scenario);
    match (&mut scenario, version_address) {
        (Scenario::Update { trailer, .. }, Some(address)) => *trailer = address,
        (_, Some(_)) => return Err(Failure::Usage("--version-address qualifies --update".to_string())),
        (_, None) => {}
    }
    if scenario == Scenario::Sync && set_time.is_some() {
        return Err(Failure::Usage("--sync sets the clock itself, so --set-time has no place in it".to_string()));
    }
    let Some(dump_path) = dump_path else {
        return Err(Failure::Usage(
            "--secret-from-dump <external_flash.bin> is required: the watch answers nothing before the challenge".to_string(),
        ));
    };
    Ok(Invocation { endpoint, dump_path, set_time, scenario })
}

fn set_clock(link: &mut Link, now: u32) -> Result<(), Failure> {
    let step = Step {
        label: "set time",
        frame: time_set_frame(UnixTime(now as i64), 0, None),
        answers: Answers::One,
    };
    match exchange(link, &step)? {
        Answered::Took => Ok(()),
        Answered::Refused => Err(Failure::Refused(step.label.to_string())),
    }
}

fn run_clocked(
    link: &mut Link,
    endpoint: &str,
    set_time: Option<u32>,
    clocked: &Clocked,
) -> Result<(), Failure> {
    // A scenario needs the watch's own clock to be a plausible wall time, since
    // a workout and a sleep window are both timestamped against it, so the
    // scenarios write the time first and then use the same value themselves.
    let now = set_time.unwrap_or(DEFAULT_SCENARIO_TIME);
    set_clock(link, now)?;
    if let Some((seconds, subcategory)) = clocked.workout {
        workout(link, seconds, subcategory, now as i32)?;
    }
    if let Some(seconds) = clocked.hr_seconds {
        hr_measure(link, seconds)?;
    }
    if clocked.alarm {
        alarm(link)?;
    }
    if clocked.notify {
        notify(link)?;
    }
    if let Some(announcement) = &clocked.announcement {
        announce(link, endpoint, announcement)?;
    }
    if let Some(seconds) = clocked.sleep_seconds {
        sleep(link, seconds, now as i32)?;
    }
    Ok(())
}

fn run_survey(link: &mut Link, set_time: Option<u32>) -> Result<(), Failure> {
    for step in read_only_steps() {
        exchange(link, &step)?;
    }
    let Some(now) = set_time else {
        return Ok(());
    };
    set_clock(link, now)?;
    request(link, "time, read back", Command::CMD_TIME_GET, Vec::new())
}

fn drive(invocation: Invocation) -> Result<(), Failure> {
    let Invocation { endpoint, dump_path, set_time, scenario } = invocation;
    let dump = std::fs::read(&dump_path)
        .map_err(|error| Failure::Setup(format!("{dump_path}: {error}")))?;
    let association = dblib::association(&dump)
        .map_err(|error| Failure::Setup(format!("{dump_path}: {error}")))?;
    println!("{association:?}");

    let package = match &scenario {
        Scenario::Update { package, trailer } => {
            let bytes = std::fs::read(package)
                .map_err(|error| Failure::Setup(format!("{package}: {error}")))?;
            let parsed = update::Package::parse(bytes, *trailer)
                .map_err(|reason| Failure::Setup(format!("{package} is not a usable package: {reason}")))?;
            Some(parsed)
        }
        _ => None,
    };
    let split = if package.is_some() { WriteSplit::WholeFrame } else { WriteSplit::AttMtu };

    let mut link = Link::connect(&endpoint, split)?;
    let link = &mut link;
    println!("connected to {endpoint}");
    if scenario == Scenario::Sync {
        return sync(link, association);
    }
    let identity = authenticate(link, association)?;
    println!("authenticated");
    // The line the test harness reads; everything else here is for a human.
    println!("soft_version={}", identity.soft_version);

    match (&scenario, set_time) {
        (Scenario::Survey | Scenario::Clocked(_), _) | (_, None) => {}
        (_, Some(now)) => set_clock(link, now)?,
    }
    match &scenario {
        Scenario::Sync => unreachable!("a sync returned above"),
        Scenario::Update { .. } => {
            let package = package.expect("an update scenario parsed its package");
            update::run(link, &package).map_err(Failure::Update)?;
            update::restart(link)?;
            println!("update pushed");
            Ok(())
        }
        Scenario::ProbeOnly => Ok(()),
        Scenario::Survey => run_survey(link, set_time),
        Scenario::Ecg { seconds } => ecg(link, *seconds),
        Scenario::Measure { category, seconds } => measure(link, *category, *seconds),
        Scenario::BodyTemperature => body_temperature(link),
        Scenario::RawData => raw_data(link),
        Scenario::Stored { types } => types.iter().try_for_each(|signal_type| stored(link, *signal_type)),
        Scenario::Send { commands } => commands
            .iter()
            .try_for_each(|number| ask(link, "command by number", Command(*number), Vec::new())),
        Scenario::Clocked(clocked) => run_clocked(link, &endpoint, set_time, clocked),
    }
}

fn main() -> ExitCode {
    let outcome = parse_arguments(env::args().skip(1)).and_then(drive);
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(failure) => {
            eprintln!("{failure}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parsed(arguments: &[&str]) -> Result<Invocation, Failure> {
        let mut all = vec!["--secret-from-dump".to_string(), "dump".to_string()];
        all.extend(arguments.iter().map(|argument| argument.to_string()));
        parse_arguments(all)
    }

    fn scenario(arguments: &[&str]) -> Scenario {
        parsed(arguments).unwrap().scenario
    }

    #[test]
    fn no_scenario_is_the_read_only_survey() {
        assert_eq!(scenario(&[]), Scenario::Survey);
    }

    #[test]
    fn the_clocked_scenarios_combine() {
        let Scenario::Clocked(clocked) = scenario(&["--alarm", "--notify", "--workout", "5"]) else {
            panic!("not the clocked scenarios");
        };
        assert!(clocked.alarm && clocked.notify);
        assert_eq!(clocked.workout, Some((5, ACTIVITY_SUBCATEGORY_RUNNING)));
    }

    #[test]
    fn scenarios_that_are_a_whole_run_refuse_company_rather_than_dropping_one() {
        for pair in [
            &["--ecg", "5", "--workout", "5"][..],
            &["--sync", "--probe-only"],
            &["--ecg", "5", "--spo2", "5"],
            &["--send", "1", "--stored", "1"],
        ] {
            assert!(matches!(parsed(pair), Err(Failure::Usage(_))), "{pair:?}");
        }
    }

    #[test]
    fn a_qualifier_needs_what_it_qualifies() {
        assert!(matches!(parsed(&["--activity", "6"]), Err(Failure::Usage(_))));
        assert!(matches!(parsed(&["--app", "a.b"]), Err(Failure::Usage(_))));
        assert!(matches!(parsed(&["--version-address", "0x27000"]), Err(Failure::Usage(_))));
    }

    #[test]
    fn a_sync_refuses_a_clock_it_would_ignore() {
        assert!(matches!(parsed(&["--sync", "--set-time", "1"]), Err(Failure::Usage(_))));
        assert!(parsed(&["--ecg", "5", "--set-time", "1"]).is_ok());
    }

    #[test]
    fn a_bad_value_is_a_usage_error() {
        assert!(matches!(parsed(&["--ecg", "soon"]), Err(Failure::Usage(_))));
        assert!(matches!(parsed(&["--notification", "no separator"]), Err(Failure::Usage(_))));
        assert!(matches!(parsed(&["--nonsense"]), Err(Failure::Usage(_))));
    }

    #[test]
    fn the_dump_is_required() {
        assert!(matches!(parse_arguments(Vec::new()), Err(Failure::Usage(_))));
    }
}
