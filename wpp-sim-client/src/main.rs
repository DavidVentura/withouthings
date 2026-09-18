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
    InfoType, MeasureCategory, MeasureLiveAppStatus, ProbeChallenge, ProbeChallengeResponse,
    ProbeReply, TimeSet, Uint32, VasistasType, Version, WamVasistasGet,
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
    // A bare command by number, for asking the watch what it does with one.
    // Removing a command from the dispatch table is only half an answer; what
    // the phone sees is the other half, and nothing else here can send a
    // command the scenario does not already use.
    let mut send_commands: Vec<u16> = Vec::new();
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
            "--probe-only" => probe_only = true,
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
                eprintln!("usage: wpp-sim-client [--endpoint host:port] --secret-from-dump <external_flash.bin> [--set-time <unix>] [--probe-only] [--ecg <seconds>] [--send <command>] [--update <package>]");
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
        match update::Package::parse(bytes) {
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
