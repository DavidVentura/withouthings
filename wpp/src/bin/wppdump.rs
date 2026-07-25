//! Decode the WPP traffic in an Android btsnoop_hci.log.
//!
//! Usage: wppdump <btsnoop_hci.log> [--quiet] [--follow] [--from-start] [--skip N]
//!
//! With --follow the file is re-read as it grows and only newly arrived frames
//! are printed, so it can be pointed at a log being mirrored off a phone.
//!
//! Exits non-zero when the capture contains anything the extracted protocol
//! description cannot account for.

use std::collections::{BTreeMap, BTreeSet};
use std::process::ExitCode;

use wpp::capture::{frames, Captured, Direction, StreamItem};
use wpp::{Frame, WppObject};

fn hex(bytes: &[u8]) -> String {
    bytes
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<Vec<_>>()
        .join("")
}

fn arrow(direction: Direction) -> &'static str {
    match direction {
        Direction::Sent => "phone -> watch",
        Direction::Received => "watch -> phone",
    }
}

fn describe(object: &WppObject) -> String {
    match object {
        WppObject::Unknown { type_id, data } => {
            format!("UNKNOWN type 0x{:04x} ({} bytes)", type_id, data.len())
        }
        WppObject::Malformed {
            type_id,
            data,
            error,
        } => {
            format!(
                "MALFORMED type 0x{:04x} ({} bytes): {}",
                type_id,
                data.len(),
                error
            )
        }
        other => format!("{} = {:?}", other.type_name(), other),
    }
}

fn print_frame(direction: Direction, att_handle: u16, frame: &Frame) {
    let channel = match frame.command.channel() {
        Some(c) => format!("{c:?}"),
        None => "channel?".to_string(),
    };
    println!(
        "{}  handle 0x{:04x}  {}  [{}]",
        arrow(direction),
        att_handle,
        frame.command.opcode_name().unwrap_or("UNKNOWN"),
        channel
    );
    for object in &frame.objects {
        println!("    {}", describe(object));
    }
}

#[derive(Default)]
struct Summary {
    frames: usize,
    desyncs: usize,
    reencode_mismatches: Vec<(String, Vec<u8>, Vec<u8>)>,
    wpp_handles: BTreeSet<u16>,
    ignored_handles: BTreeSet<u16>,
    commands: BTreeMap<&'static str, usize>,
    types: BTreeMap<&'static str, usize>,
    unknown_types: BTreeMap<u16, usize>,
    malformed: BTreeMap<(u16, String), usize>,
}

impl Summary {
    fn record(&mut self, frame: &Frame, bytes: &[u8]) {
        self.frames += 1;
        // Decoding a frame we could not have produced means the spec is wrong.
        let reencoded = frame.to_bytes();
        if reencoded != bytes {
            self.reencode_mismatches.push((
                frame.command.opcode_name().unwrap_or("UNKNOWN").to_string(),
                bytes.to_vec(),
                reencoded,
            ));
        }
        *self
            .commands
            .entry(frame.command.opcode_name().unwrap_or("UNKNOWN"))
            .or_default() += 1;
        for object in &frame.objects {
            match object {
                WppObject::Unknown { type_id, .. } => {
                    *self.unknown_types.entry(*type_id).or_default() += 1;
                }
                WppObject::Malformed { type_id, error, .. } => {
                    *self
                        .malformed
                        .entry((*type_id, error.to_string()))
                        .or_default() += 1;
                }
                known => *self.types.entry(known.type_name()).or_default() += 1,
            }
        }
    }

    fn report(&self) {
        println!("\n=== summary ===");
        println!("frames decoded: {}", self.frames);
        println!(
            "WPP ATT handles: {}",
            self.wpp_handles
                .iter()
                .map(|h| format!("0x{h:04x}"))
                .collect::<Vec<_>>()
                .join(", ")
        );
        if !self.ignored_handles.is_empty() {
            println!(
                "non-WPP ATT handles ignored: {}",
                self.ignored_handles
                    .iter()
                    .map(|h| format!("0x{h:04x}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            );
        }
        println!("distinct commands: {}", self.commands.len());
        for (name, count) in &self.commands {
            println!("  {count:5}  {name}");
        }
        println!("distinct object types: {}", self.types.len());
        for (name, count) in &self.types {
            println!("  {count:5}  {name}");
        }
        if self.desyncs > 0 {
            println!("resync events (bytes skipped): {}", self.desyncs);
        }
        if !self.unknown_types.is_empty() {
            println!("type ids with no class in the app:");
            for (type_id, count) in &self.unknown_types {
                let name = wpp::objects::type_name_for_id(*type_id).unwrap_or("unnamed");
                println!("  {count:5}  0x{type_id:04x} {name}");
            }
        }
        if !self.reencode_mismatches.is_empty() {
            println!(
                "FRAMES THAT DID NOT RE-ENCODE TO THE CAPTURED BYTES: {}",
                self.reencode_mismatches.len()
            );
            for (command, wire, ours) in self.reencode_mismatches.iter().take(5) {
                println!(
                    "  {command}\n    wire: {}\n    ours: {}",
                    hex(wire),
                    hex(ours)
                );
            }
        }
        if !self.malformed.is_empty() {
            println!("OBJECTS THAT DID NOT MATCH THE EXTRACTED LAYOUT:");
            for ((type_id, error), count) in &self.malformed {
                println!("  {count:5}  0x{type_id:04x}: {error}");
            }
        }
    }

    fn clean(&self) -> bool {
        self.malformed.is_empty() && self.reencode_mismatches.is_empty()
    }
}

type Decoded = Vec<Captured>;

fn decode(path: &str) -> Result<Decoded, String> {
    let file = std::fs::read(path).map_err(|err| format!("{path}: {err}"))?;
    frames(&file).map_err(|err| format!("{path}: {err}"))
}

/// An ATT handle that never yields a frame is some other GATT characteristic,
/// not a WPP stream we failed to decode.
fn wpp_handles(items: &Decoded) -> BTreeSet<u16> {
    items
        .iter()
        .filter(|c| matches!(c.item, StreamItem::Frame { .. }))
        .map(|c| c.att_handle)
        .collect()
}

fn consume(
    items: &Decoded,
    from: usize,
    handles: &BTreeSet<u16>,
    quiet: bool,
    summary: &mut Summary,
) {
    for captured in &items[from..] {
        let (direction, att_handle) = (&captured.direction, &captured.att_handle);
        if !handles.contains(att_handle) {
            summary.ignored_handles.insert(*att_handle);
            continue;
        }
        match &captured.item {
            StreamItem::Frame { frame, bytes } => {
                summary.record(frame, bytes);
                if !quiet {
                    print_frame(*direction, *att_handle, frame);
                }
            }
            StreamItem::Desync { bytes, cause } => {
                summary.desyncs += bytes.len();
                if !quiet {
                    println!(
                        "{}  handle 0x{:04x}  skipped {} ({})",
                        arrow(*direction),
                        att_handle,
                        hex(bytes),
                        cause
                    );
                }
            }
        }
    }
}

fn follow(path: &str, quiet: bool, from_start: bool) -> ExitCode {
    use std::io::Write;

    let mut consumed = 0usize;
    let mut summary = Summary::default();

    if !from_start {
        // tail -f semantics: skip whatever the log already held.
        match decode(path) {
            Ok(items) => {
                consumed = items.len();
                eprintln!("following {path}: skipping {consumed} existing records");
            }
            Err(err) => eprintln!("{err}"),
        }
    }

    loop {
        let items = match decode(path) {
            Ok(items) => items,
            Err(err) => {
                eprintln!("{err}");
                std::thread::sleep(std::time::Duration::from_millis(500));
                continue;
            }
        };
        // A shorter log means the phone rotated it; start over.
        let len = items.len();
        if len < consumed {
            eprintln!("{path}: log rotated, restarting");
            consumed = 0;
        }
        if len > consumed {
            let handles = wpp_handles(&items);
            summary.wpp_handles = handles.clone();
            let before = summary.frames;
            consume(&items, consumed, &handles, quiet, &mut summary);
            consumed = len;
            if summary.frames > before {
                println!(
                    "-- +{} frames ({} total)",
                    summary.frames - before,
                    summary.frames
                );
            }
            let _ = std::io::stdout().flush();
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let quiet = args.iter().any(|a| a == "--quiet");
    let following = args.iter().any(|a| a == "--follow");
    let from_start = args.iter().any(|a| a == "--from-start");
    let skip: usize = match args.iter().position(|a| a == "--skip") {
        Some(i) => match args.get(i + 1).and_then(|n| n.parse().ok()) {
            Some(n) => n,
            None => {
                eprintln!("--skip needs a record count");
                return ExitCode::FAILURE;
            }
        },
        None => 0,
    };
    let positional: Vec<&String> = args
        .iter()
        .skip(1)
        .enumerate()
        .filter(|(i, a)| !a.starts_with("--") && args.get(*i) != Some(&"--skip".to_string()))
        .map(|(_, a)| a)
        .collect();
    let Some(path) = positional.first() else {
        eprintln!(
            "usage: wppdump <btsnoop_hci.log> [--quiet] [--follow] [--from-start] [--skip N]"
        );
        return ExitCode::FAILURE;
    };

    if following {
        return follow(path, quiet, from_start);
    }

    let items = match decode(path) {
        Ok(items) => items,
        Err(err) => {
            eprintln!("{err}");
            return ExitCode::FAILURE;
        }
    };

    let handles = wpp_handles(&items);
    let mut summary = Summary::default();
    consume(&items, skip.min(items.len()), &handles, quiet, &mut summary);
    summary.wpp_handles = handles;
    summary.report();

    if summary.clean() {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
