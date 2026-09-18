mod dblib;
mod update;

use std::env;
use std::io::{ErrorKind, Read, Write};
use std::net::TcpStream;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use wpp::client::{probe_frame, Credentials};
use wpp::commands::Command;
use wpp::frame::{Channel, Frame};
use wpp::objects::{
    ActivitySubcategory, Alarm, Distance, InfoType, LocalNotification, MeasureCategory,
    MeasureLiveAppStatus, Pace, PauseState, ProbeChallenge, ProbeChallengeResponse, ProbeReply,
    SleepActivityGet, Speed, StartTime, TimeSet, Uint32, VasistasType, Version, WamAutoSleep,
    WamVasistasGet, WorkoutGpsStatus,
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

/// A scenario's duration argument, which every live scenario takes the same way.
fn seconds(arguments: &mut impl Iterator<Item = String>, flag: &str) -> u64 {
    arguments
        .next()
        .unwrap_or_else(|| panic!("{flag} takes a number of seconds"))
        .parse()
        .unwrap_or_else(|_| panic!("{flag} takes a number of seconds"))
}

struct Link {
    socket: TcpStream,
    received: Vec<u8>,
    write_limit: usize,
}

impl Link {
    fn connect(endpoint: &str) -> std::io::Result<Link> {
        let socket = TcpStream::connect(endpoint)?;
        socket.set_nodelay(true)?;
        socket.set_read_timeout(Some(REPLY_TIMEOUT))?;
        Ok(Link { socket, received: Vec::new(), write_limit: WRITE_LIMIT })
    }

    fn send(&mut self, frame: &Frame) -> std::io::Result<()> {
        println!("-> {:?} {:?}", frame.command.opcode_name(), frame.objects);
        self.send_quietly(frame)
    }

    fn send_quietly(&mut self, frame: &Frame) -> std::io::Result<()> {
        for part in frame.to_wire() {
            for write in part.to_bytes().chunks(self.write_limit) {
                let mut message = Vec::with_capacity(write.len() + 2);
                message.extend_from_slice(&(write.len() as u16).to_be_bytes());
                message.extend_from_slice(write);
                self.socket.write_all(&message)?;
            }
        }
        Ok(())
    }

    fn receive(&mut self, timeout: Duration) -> std::io::Result<Option<Frame>> {
        self.socket.set_read_timeout(Some(timeout))?;
        loop {
            let mut header = [0u8; 2];
            match self.socket.read_exact(&mut header) {
                Ok(()) => {}
                Err(e) if e.kind() == ErrorKind::WouldBlock || e.kind() == ErrorKind::TimedOut => {
                    return Ok(None);
                }
                Err(e) => return Err(e),
            }
            let mut notification = vec![0u8; u16::from_be_bytes(header) as usize];
            self.socket.read_exact(&mut notification)?;
            self.received.extend_from_slice(&notification);
            // Anything short of a whole frame is a reply still arriving, since
            // the watch notifies in MTU-sized pieces.
            let Ok((frame, rest)) = Frame::parse_prefix(&self.received) else {
                continue;
            };
            let consumed = self.received.len() - rest.len();
            self.received.drain(..consumed);
            return Ok(Some(frame));
        }
    }

    /// Reads the next frame that is an answer rather than watch-initiated
    /// chatter, answering the chatter as a phone would so the watch stops
    /// repeating it.
    fn next_answer(&mut self, deadline: Instant) -> std::io::Result<Option<Frame>> {
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
                Command(frame.command.opcode()).with_channel(Channel::SlaveRequest),
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

fn run(link: &mut Link, step: &Step) -> std::io::Result<()> {
    println!("\n== {} ==", step.label);
    link.send(&step.frame)?;
    let opcode = step.frame.command.opcode();
    loop {
        let Some(reply) = link.next_answer(Instant::now() + REPLY_TIMEOUT)? else {
            println!("<- nothing within the timeout");
            return Ok(());
        };
        report(&reply);
        if reply.command.opcode() == Command::CMD_ERROR.0 {
            return Ok(());
        }
        match step.answers {
            Answers::One => return Ok(()),
            Answers::UntilQuiet if reply.command.opcode() == opcode => {
                // The walk ends with a quiet link rather than a marker frame.
                while let Some(more) = link.next_answer(Instant::now() + QUIET)? {
                    report(&more);
                }
                return Ok(());
            }
            Answers::UntilQuiet => {}
        }
    }
}

/// One ECG acquisition: ask for it, watch it run, stop it.
///
/// MEASURE_START and MEASURE_STOP are the only two commands the phone sends;
/// everything between them the watch pushes by itself on the slave channel, so
/// the middle of this is a read loop and not a request. The request objects are
/// the two the handler at 0x54238 parses -- MeasureCategory, which is what says
/// ECG rather than PPG or SpO2, and MeasureLiveAppStatus, which says the phone
/// has the live screen up and is therefore ready for the sample frames.
fn ecg(link: &mut Link, seconds: u64) -> std::io::Result<()> {
    let category =
        WppObject::MeasureCategory(MeasureCategory { value: MeasureCategory::VALUE_ECG });
    run(
        link,
        &Step {
            label: "ecg start",
            frame: Frame::new(
                Command::CMD_MEASURE_START,
                vec![
                    category.clone(),
                    WppObject::MeasureLiveAppStatus(MeasureLiveAppStatus {
                        app_live_screen_displayed: 1,
                    }),
                ],
            ),
            answers: Answers::One,
        },
    )?;
    // MEASURE_START is the phone saying it will take the live data; it does not
    // start anything, because on the watch an ECG is started by the wearer
    // holding the crown. CMD_ECG_TEST is the command that stands in for that,
    // and the watch answers it by sending its own MEASURE_START back with the
    // signal's real metadata. Its one object is how many samples to take, and
    // the number matters: the classifier asserts unless it is handed the whole
    // measurement, which at the 300 Hz the metadata reports is thirty seconds.
    // Without the object the handler logs that it could not read the count and
    // falls back to a thousand, and the run ends in that assert.
    run(
        link,
        &Step {
            label: "ecg run",
            frame: Frame::new(
                Command::CMD_ECG_TEST,
                vec![WppObject::Uint32(Uint32 { val: ECG_SAMPLES })],
            ),
            answers: Answers::One,
        },
    )?;

    println!("\n== ecg live, {seconds} s ==");
    let until = Instant::now() + Duration::from_secs(seconds);
    let (mut frames, mut samples) = (0usize, 0usize);
    while let Some(frame) = link.next_frame(until)? {
        frames += 1;
        for object in &frame.objects {
            if let WppObject::MeasureLiveEcg(live) = object {
                samples += live.samples.len();
            }
        }
        if frame.command.channel() != Some(Channel::SlaveRequest) {
            report(&frame);
        }
    }
    println!("   {frames} frames from the watch, {samples} ECG sample bytes");

    run(
        link,
        &Step {
            label: "ecg stop",
            frame: Frame::new(Command::CMD_MEASURE_STOP, vec![category]),
            answers: Answers::One,
        },
    )
}

/// How long a live scenario watches the link before it sends its closing
/// command, and how often it pushes the phone's own updates while it waits.
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
    mut tick: impl FnMut(&mut Link, u32) -> std::io::Result<()>,
) -> std::io::Result<usize> {
    println!("\n== {label} ==");
    let mut frames = 0usize;
    let mut ticks = 0u32;
    while Instant::now() < until {
        let next = (Instant::now() + LIVE_TICK).min(until);
        while let Some(frame) = link.next_frame(next)? {
            frames += 1;
            if frame.command.channel() != Some(Channel::SlaveRequest) {
                report(&frame);
            }
        }
        tick(link, ticks)?;
        ticks += 1;
    }
    println!("   {frames} frames from the watch over {label}");
    Ok(frames)
}

/// One workout, driven the way the phone drives it.
///
/// WORKOUT_START carries the three objects its handler at 0x6a864 parses --
/// StartTime, ActivitySubcategory and PauseState -- and the subcategory is what
/// says which sport, so it is what selects the algorithms the watch runs. The
/// phone owns the GPS, so what it sends during the workout is the fix's state
/// through WORKOUT_GPS_STATUS and the distance, pace and speed it derives
/// through WORKOUT_LIVE_DATA; the protocol has no coordinate object.
fn workout(link: &mut Link, seconds: u64, subcategory: i16, now: i32) -> std::io::Result<()> {
    run(
        link,
        &Step {
            label: "workout start",
            frame: Frame::new(
                Command::CMD_WORKOUT_START,
                vec![
                    WppObject::StartTime(StartTime { value: now }),
                    WppObject::ActivitySubcategory(ActivitySubcategory { value: subcategory }),
                    WppObject::PauseState(PauseState { value: 0, start_time: 0, sum: 0 }),
                ],
            ),
            answers: Answers::One,
        },
    )?;
    run(
        link,
        &Step {
            label: "workout status",
            frame: Frame::new(Command::CMD_WORKOUT_STATUS, Vec::new()),
            answers: Answers::One,
        },
    )?;
    // A fix takes a moment on a phone too: the first status is searching and
    // the rest are locked, which is the transition the watch's GPS screen reads.
    run(
        link,
        &Step {
            label: "gps searching",
            frame: Frame::new(
                Command::CMD_WORKOUT_GPS_STATUS,
                vec![WppObject::WorkoutGpsStatus(WorkoutGpsStatus { status: 1 })],
            ),
            answers: Answers::One,
        },
    )?;

    let until = Instant::now() + Duration::from_secs(seconds);
    watch(link, "workout live", until, |link, tick| {
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

    run(
        link,
        &Step {
            label: "workout stop",
            frame: Frame::new(Command::CMD_WORKOUT_STOP, Vec::new()),
            answers: Answers::One,
        },
    )?;
    run(
        link,
        &Step {
            label: "workout status, after",
            frame: Frame::new(Command::CMD_WORKOUT_STATUS, Vec::new()),
            answers: Answers::One,
        },
    )
}

/// A manual heart-rate measurement: the one the watch face's heart-rate tile
/// starts.
///
/// The handler at 0x624e4 reads a single Uint32 and nothing else: zero starts
/// the measurement and one stops it, and everything in between arrives as live
/// heart rate on the slave channel.
fn hr_measure(link: &mut Link, seconds: u64) -> std::io::Result<()> {
    run(
        link,
        &Step {
            label: "hr measure start",
            frame: Frame::new(Command::CMD_HR_MEASURE, vec![WppObject::Uint32(Uint32 { val: 0 })]),
            answers: Answers::One,
        },
    )?;
    let until = Instant::now() + Duration::from_secs(seconds);
    watch(link, "hr measure live", until, |link, _| {
        link.send(&Frame::new(Command::CMD_GET_LIVE_HR, Vec::new()))
    })?;
    run(
        link,
        &Step {
            label: "hr measure stop",
            frame: Frame::new(Command::CMD_HR_MEASURE, vec![WppObject::Uint32(Uint32 { val: 1 })]),
            answers: Answers::One,
        },
    )?;
    run(
        link,
        &Step {
            label: "heart rate, after",
            frame: Frame::new(Command::CMD_GET_HR, Vec::new()),
            answers: Answers::One,
        },
    )
}

/// An alarm the phone sets, and the settings read back.
fn alarm(link: &mut Link) -> std::io::Result<()> {
    for step in [
        Step {
            label: "set alarm",
            frame: Frame::new(
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
            ),
            answers: Answers::One,
        },
        Step {
            label: "alarm read back",
            frame: Frame::new(Command::CMD_GET_ALARM, Vec::new()),
            answers: Answers::One,
        },
        Step {
            label: "alarm settings",
            frame: Frame::new(Command::CMD_GET_ALARM_SETTINGS, Vec::new()),
            answers: Answers::One,
        },
    ] {
        run(link, &step)?;
    }
    Ok(())
}

/// Sleep: the watch decides it itself, so the phone's part is to turn automatic
/// sleep on and then leave the watch alone. The scenario is the quiet that
/// follows, which is what the sleep-wake algorithm reads.
fn sleep(link: &mut Link, seconds: u64, now: i32) -> std::io::Result<()> {
    for step in [
        Step {
            label: "auto sleep on",
            frame: Frame::new(
                Command::CMD_WAM_AUTO_SLEEP,
                vec![WppObject::WamAutoSleep(WamAutoSleep { auto_sleep: 1 })],
            ),
            answers: Answers::One,
        },
        Step {
            label: "auto sleep read back",
            frame: Frame::new(Command::CMD_WAM_AUTO_SLEEP_GET, Vec::new()),
            answers: Answers::One,
        },
    ] {
        run(link, &step)?;
    }
    let until = Instant::now() + Duration::from_secs(seconds);
    watch(link, "still and worn", until, |_, _| Ok(()))?;
    run(
        link,
        &Step {
            label: "sleep activity",
            frame: Frame::new(
                Command::CMD_SLEEP_ACTIVITY_GET,
                vec![WppObject::SleepActivityGet(SleepActivityGet { from_utc: now - 86400 })],
            ),
            answers: Answers::One,
        },
    )
}

/// A notification push. ANCS is the phone's own service and the sim has no
/// radio, so what is reachable over the pipe is the local-event path the watch
/// exposes for the same purpose.
fn notify(link: &mut Link) -> std::io::Result<()> {
    for step in [
        Step {
            label: "notification push",
            frame: Frame::new(
                Command::CMD_LOCAL_EVENT_NOTIFY,
                vec![WppObject::LocalNotification(LocalNotification { id: 1, status: 1 })],
            ),
            answers: Answers::One,
        },
        Step {
            label: "notification read back",
            frame: Frame::new(Command::CMD_NOTIFICATION_GET, Vec::new()),
            answers: Answers::One,
        },
    ] {
        run(link, &step)?;
    }
    Ok(())
}

fn report(frame: &Frame) {
    println!("<- {:?}", frame.command.opcode_name());
    for object in &frame.objects {
        println!("   {object:?}");
    }
}

/// The firmware version is in the probe reply and nowhere else, so the reply
/// is what a caller wants back rather than a yes/no.
fn authenticate(
    link: &mut Link,
    association: &dblib::Association,
) -> std::io::Result<Option<ProbeReply>> {
    link.send(&probe_frame())?;
    let Some(challenge_frame) = link.next_answer(Instant::now() + REPLY_TIMEOUT)? else {
        println!("the watch never answered the probe");
        return Ok(None);
    };
    report(&challenge_frame);
    let Some(challenge) = challenge_frame.objects.iter().find_map(|o| match o {
        WppObject::ProbeChallenge(c) => Some(c.clone()),
        _ => None,
    }) else {
        // Answering a watch that does not challenge means associating with it,
        // which this client deliberately does not do.
        println!("the watch did not challenge; nothing to authenticate against");
        return Ok(None);
    };
    let identity = challenge.mac.to_ascii_lowercase();
    if identity != association.mac {
        println!("the watch's identity {identity} is not the one the dump holds a secret for");
        return Ok(None);
    }
    let credentials = Credentials { mac: identity.clone(), secret: association.secret.clone() };
    let answer = credentials.answer(&challenge.challenge);
    link.send(&Frame::new(
        Command::CMD_PROBE_CHALLENGE,
        vec![
            WppObject::ProbeChallengeResponse(ProbeChallengeResponse { answer }),
            WppObject::ProbeChallenge(ProbeChallenge { mac: identity, challenge: vec![0; 16] }),
        ],
    ))?;
    let Some(reply) = link.next_answer(Instant::now() + REPLY_TIMEOUT)? else {
        println!("the watch never answered the challenge response");
        return Ok(None);
    };
    report(&reply);
    if reply.command.opcode() != Command::CMD_PROBE.0 {
        return Ok(None);
    }
    Ok(reply.objects.iter().find_map(|o| match o {
        WppObject::ProbeReply(r) => Some(r.clone()),
        _ => None,
    }))
}

fn main() -> ExitCode {
    let mut endpoint = DEFAULT_ENDPOINT.to_string();
    let mut dump_path: Option<String> = None;
    let mut set_time: Option<u32> = None;
    let mut package_path: Option<String> = None;
    let mut probe_only = false;
    // Seconds of ECG to watch between the start and the stop.
    let mut ecg_seconds: Option<u64> = None;
    // The live scenarios, each the seconds it runs for between its own start
    // and stop commands.
    let mut workout_seconds: Option<u64> = None;
    let mut workout_subcategory: i16 = ACTIVITY_SUBCATEGORY_RUNNING;
    let mut hr_seconds: Option<u64> = None;
    let mut sleep_seconds: Option<u64> = None;
    let mut alarm_scenario = false;
    let mut notify_scenario = false;
    // A bare command by number, for asking the watch what it does with one.
    // Removing a command from the dispatch table is only half an answer; what
    // the phone sees is the other half, and nothing else here can send a
    // command the scenario does not already use.
    let mut send_commands: Vec<u16> = Vec::new();
    let mut version_trailer = update::VersionTrailer::STOCK;
    let mut arguments = env::args().skip(1);
    while let Some(argument) = arguments.next() {
        match argument.as_str() {
            "--endpoint" => endpoint = arguments.next().expect("--endpoint takes an address"),
            "--secret-from-dump" => {
                dump_path = Some(arguments.next().expect("--secret-from-dump takes a path"))
            }
            "--set-time" => {
                set_time = Some(
                    arguments
                        .next()
                        .expect("--set-time takes a unix timestamp")
                        .parse()
                        .expect("--set-time takes a unix timestamp"),
                )
            }
            "--update" => {
                package_path = Some(arguments.next().expect("--update takes a package path"))
            }
            // Where the image in the package reads its own version. It is the
            // stock address unless a layout moved the trailer's section, and
            // tools/mkpkg.py prints the one it wrote to.
            "--version-address" => {
                let text = arguments.next().expect("--version-address takes an address");
                let parsed = usize::from_str_radix(text.trim_start_matches("0x"), 16)
                    .expect("--version-address takes a hexadecimal address");
                version_trailer = update::VersionTrailer::at(parsed)
                    .unwrap_or_else(|reason| panic!("--version-address {text}: {reason}"));
            }
            // The pipe is on localhost and a run that shares the host with
            // another run differs from it only in the port.
            "--port" => {
                endpoint = format!("127.0.0.1:{}", arguments.next().expect("--port takes a port"))
            }
            "--probe-only" => probe_only = true,
            "--workout" => workout_seconds = Some(seconds(&mut arguments, "--workout")),
            "--activity" => {
                workout_subcategory = arguments
                    .next()
                    .expect("--activity takes a subcategory")
                    .parse()
                    .expect("--activity takes a subcategory")
            }
            "--hr-measure" => hr_seconds = Some(seconds(&mut arguments, "--hr-measure")),
            "--sleep" => sleep_seconds = Some(seconds(&mut arguments, "--sleep")),
            "--alarm" => alarm_scenario = true,
            "--notify" => notify_scenario = true,
            "--ecg" => {
                ecg_seconds = Some(
                    arguments
                        .next()
                        .expect("--ecg takes a number of seconds")
                        .parse()
                        .expect("--ecg takes a number of seconds"),
                )
            }
            "--send" => send_commands.push(
                arguments
                    .next()
                    .expect("--send takes a command number")
                    .parse()
                    .expect("--send takes a command number"),
            ),
            other => {
                eprintln!("usage: wpp-sim-client [--endpoint host:port | --port n] --secret-from-dump <external_flash.bin> [--set-time <unix>] [--probe-only] [--ecg <seconds>] [--workout <seconds> [--activity <n>]] [--hr-measure <seconds>] [--sleep <seconds>] [--alarm] [--notify] [--send <command>] [--update <package> [--version-address <hex>]]");
                eprintln!("unknown argument {other}");
                return ExitCode::FAILURE;
            }
        }
    }
    let Some(dump_path) = dump_path else {
        eprintln!("--secret-from-dump <external_flash.bin> is required: the watch answers nothing before the challenge");
        return ExitCode::FAILURE;
    };
    let dump = std::fs::read(&dump_path).expect("the flash dump is readable");
    let association = dblib::association(&dump).expect("the dump holds a dblib association");
    println!("{association:?}");

    let package = package_path.map(|path| {
        let bytes = std::fs::read(&path).expect("the package is readable");
        match update::Package::parse(bytes, version_trailer) {
            Ok(package) => package,
            Err(reason) => panic!("{path} is not a usable package: {reason}"),
        }
    });

    let mut link = Link::connect(&endpoint).expect("the pipe accepts the connection");
    println!("connected to {endpoint}");
    let Some(identity) = authenticate(&mut link, &association).expect("the link stays up through the handshake")
    else {
        return ExitCode::FAILURE;
    };
    println!("authenticated");
    // The line the test harness reads; everything else here is for a human.
    println!("soft_version={}", identity.soft_version);

    if let Some(package) = package {
        link.write_limit = FRAME_PER_WRITE_LIMIT;
        match update::run(&mut link, &package).expect("the link stays up through the transfer") {
            Ok(()) => {}
            Err(reason) => {
                eprintln!("update failed: {reason}");
                return ExitCode::FAILURE;
            }
        }
        update::restart(&mut link).expect("the link stays up to the restart");
        println!("update pushed");
        return ExitCode::SUCCESS;
    }
    if let Some(seconds) = ecg_seconds {
        ecg(&mut link, seconds).expect("the link stays up");
        return ExitCode::SUCCESS;
    }
    // A scenario needs the watch's own clock to be a plausible wall time, since
    // a workout and a sleep window are both timestamped against it, so the
    // scenarios write the time first and then use the same value themselves.
    let scenario = workout_seconds.is_some()
        || hr_seconds.is_some()
        || sleep_seconds.is_some()
        || alarm_scenario
        || notify_scenario;
    if scenario {
        let now = set_time.unwrap_or(DEFAULT_SCENARIO_TIME);
        run(
            &mut link,
            &Step {
                label: "set time",
                frame: Frame::new(
                    Command::CMD_TIME_SET,
                    vec![WppObject::TimeSet(TimeSet {
                        utc: now,
                        gmt_offset: 0,
                        dst_change_time: 0,
                        next_gmt_offset: 0,
                    })],
                ),
                answers: Answers::One,
            },
        )
        .expect("the link stays up");
        if let Some(seconds) = workout_seconds {
            workout(&mut link, seconds, workout_subcategory, now as i32)
                .expect("the link stays up");
        }
        if let Some(seconds) = hr_seconds {
            hr_measure(&mut link, seconds).expect("the link stays up");
        }
        if alarm_scenario {
            alarm(&mut link).expect("the link stays up");
        }
        if notify_scenario {
            notify(&mut link).expect("the link stays up");
        }
        if let Some(seconds) = sleep_seconds {
            sleep(&mut link, seconds, now as i32).expect("the link stays up");
        }
        return ExitCode::SUCCESS;
    }
    if !send_commands.is_empty() {
        for number in send_commands {
            run(
                &mut link,
                &Step {
                    label: "command by number",
                    frame: Frame::new(Command(number), Vec::new()),
                    answers: Answers::One,
                },
            )
            .expect("the link stays up");
        }
        return ExitCode::SUCCESS;
    }
    if probe_only {
        return ExitCode::SUCCESS;
    }

    for step in read_only_steps() {
        run(&mut link, &step).expect("the link stays up");
    }
    let Some(now) = set_time else {
        return ExitCode::SUCCESS;
    };
    for step in [
        Step {
            label: "set time",
            frame: Frame::new(
                Command::CMD_TIME_SET,
                vec![WppObject::TimeSet(TimeSet {
                    utc: now,
                    gmt_offset: 0,
                    dst_change_time: 0,
                    next_gmt_offset: 0,
                })],
            ),
            answers: Answers::One,
        },
        Step {
            label: "time, read back",
            frame: Frame::new(Command::CMD_TIME_GET, Vec::new()),
            answers: Answers::One,
        },
    ] {
        run(&mut link, &step).expect("the link stays up");
    }
    ExitCode::SUCCESS
}
