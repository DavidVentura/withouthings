//! The phone side of a firmware update.
//!
//! The watch pulls: the phone announces the package with `CMD_FW_AVAILABLE`,
//! the watch asks for a span with `CMD_REQUEST_FW_CHUNK`, the phone answers on
//! the same command with `FwChunk` objects and a `Null` terminator, and the
//! watch asks again until it has the whole file. It then hashes what it wrote
//! to external flash and compares against the SHA-1 the phone declared, which
//! is the only content check there is. `CMD_RESTART_TO_UPDATE` sets the boot
//! mode the bootloader reads and reboots.

use std::io;
use std::time::Instant;

use wpp::client::sha1;
use wpp::commands::Command;
use wpp::frame::Frame;
use wpp::objects::{FwChunk, FwChunkRequest, FwInfo, Null};
use wpp::WppObject;

use crate::{report, Link, QUIET};

const HEADER_VERSION: u16 = 1;
const IE_APPL: u16 = 1;
const IE_ENTRY_LEN: u16 = 16;
/// `get_fw_version` (0x36e70) loads this absolute address -- the u32 after the
/// build string -- and the probe reply carries what it returns. The appl part is
/// flashed at 0x27000, so the version sits at a fixed offset into the part and
/// not at its end: a part that carries more than the stock app image is longer
/// without moving it.
const APPL_BASE: usize = 0x27000;
const APPL_VERSION_ADDRESS: usize = 0xf1178;
/// Beyond the protocol's own cap the watch has no say in the split, so the
/// largest chunk that leaves room for the frame and object headers is the one
/// that costs the fewest round trips.
const FRAME_OVERHEAD: usize = 5 + 4 + 5;
const TRANSFER_COMPLETE: u32 = 1;

/// The package as the watch will store it: the bank on external flash is these
/// bytes verbatim, header included, which is why the `fwblk` table the
/// bootloader parses is the package header.
pub struct Package {
    pub bytes: Vec<u8>,
    pub appl_version: u32,
    pub sha1: [u8; 20],
    pub crc: u32,
}

impl Package {
    pub fn parse(bytes: Vec<u8>) -> Result<Package, String> {
        if bytes.len() < 8 {
            return Err(format!("{} bytes is not a package", bytes.len()));
        }
        let version = u16::from_le_bytes([bytes[0], bytes[1]]);
        let body_length = u16::from_le_bytes([bytes[2], bytes[3]]) as usize;
        if version != HEADER_VERSION {
            return Err(format!("header version {version}, expected {HEADER_VERSION}"));
        }
        let stored = read_u32(&bytes, 4 + body_length)?;
        let computed = crc32(&bytes[..4 + body_length]);
        if stored != computed {
            return Err(format!("header CRC {stored:#010x} != computed {computed:#010x}"));
        }

        let mut appl_version = None;
        let mut end = 0usize;
        let mut at = 4;
        while at < 4 + body_length {
            let ie = u16::from_le_bytes([bytes[at], bytes[at + 1]]);
            let entry_length = u16::from_le_bytes([bytes[at + 2], bytes[at + 3]]);
            if entry_length != IE_ENTRY_LEN {
                return Err(format!("header entry ie={ie} has length {entry_length}"));
            }
            at += 4;
            let address = read_u32(&bytes, at)? as usize;
            let length = read_u32(&bytes, at + 4)? as usize;
            let crc = read_u32(&bytes, at + 8)?;
            let component_version = read_u32(&bytes, at + 12)?;
            if address + length > bytes.len() {
                return Err(format!("entry ie={ie} runs past the end of the file"));
            }
            let aligned = (length + 3) & !3;
            let computed = crc32(&bytes[address..address + aligned]);
            if computed != crc {
                return Err(format!("entry ie={ie} CRC {crc:#010x} != computed {computed:#010x}"));
            }
            if ie == IE_APPL {
                let trailer = address + (APPL_VERSION_ADDRESS - APPL_BASE);
                let embedded = read_u32(&bytes, trailer)?;
                if embedded != component_version {
                    return Err(format!(
                        "the appl trailer at {trailer:#x} holds {embedded}, not the header's {component_version}"
                    ));
                }
                appl_version = Some(component_version);
            }
            end = end.max(address + length);
            at += entry_length as usize;
        }
        if end != bytes.len() {
            return Err(format!("the entries end at {end:#x} but the file is {:#x} bytes", bytes.len()));
        }
        let Some(appl_version) = appl_version else {
            return Err("no appl entry in the header".to_string());
        };
        Ok(Package {
            sha1: sha1(&bytes),
            crc: crc32(&bytes),
            appl_version,
            bytes,
        })
    }
}

fn read_u32(bytes: &[u8], at: usize) -> Result<u32, String> {
    let slice = bytes
        .get(at..at + 4)
        .ok_or_else(|| format!("the header runs past the end of the file at {at:#x}"))?;
    Ok(u32::from_le_bytes([slice[0], slice[1], slice[2], slice[3]]))
}

fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for byte in data {
        crc ^= *byte as u32;
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0xEDB8_8320 & (0u32.wrapping_sub(crc & 1)));
        }
    }
    !crc
}

fn announce(link: &mut Link, package: &Package) -> io::Result<()> {
    link.send_quietly(&Frame::new(
        Command::CMD_FW_AVAILABLE,
        vec![WppObject::FwInfo(FwInfo {
            version: package.appl_version,
            size: package.bytes.len() as u32,
            crc: package.crc,
            sha1: package.sha1.to_vec(),
        })],
    ))
}

pub fn run(link: &mut Link, package: &Package) -> io::Result<Result<(), String>> {
    println!(
        "\n== firmware update: version {}, {} bytes, sha1 {} ==",
        package.appl_version,
        package.bytes.len(),
        package.sha1.iter().map(|b| format!("{b:02x}")).collect::<String>()
    );
    // Every window is one announcement: the watch emits a `FwChunkRequest`
    // only from the tx-complete continuation the FW_AVAILABLE handler arms
    // (0x651aa arms it, 0x65078 disarms it once the request is out), and the
    // handler keeps the download context when the declared SHA-1 matches the
    // one in flight, so announcing again resumes rather than restarts.
    announce(link, package)?;

    // Coverage rather than a high-water mark: the watch does not ask for the
    // package in order, and the only thing that proves the transfer is the
    // digest it checks, so the client tracks what it actually handed over.
    let mut covered = 0usize;
    loop {
        let Some(frame) = link.next_answer(Instant::now() + QUIET)? else {
            return Ok(Err(format!(
                "the watch went quiet having asked for {covered} of {} bytes",
                package.bytes.len()
            )));
        };
        let requesting = frame.command.opcode() == Command::CMD_REQUEST_FW_CHUNK.0
            // The first request and every one after an acknowledgement ride back
            // on CMD_FW_AVAILABLE; the continuation at 0x65078 answers on 0x967.
            || frame.command.opcode() == Command::CMD_FW_AVAILABLE.0;
        let request = frame.objects.iter().find_map(|o| match o {
            WppObject::FwChunkRequest(r) => Some(r.clone()),
            _ => None,
        });
        let status = frame.objects.iter().find_map(|o| match o {
            WppObject::ReturnCode(r) => Some(r.rc),
            _ => None,
        });
        if !requesting {
            report(&frame);
            continue;
        }
        // The download state machine ends in the state whose push returns 1
        // (0x64ddc), and that 1 is what comes back as the reply's status: the
        // image is in the bank, hashed and accepted.
        if status == Some(TRANSFER_COMPLETE) {
            report(&frame);
            if covered != package.bytes.len() {
                return Ok(Err(format!(
                    "the watch called the transfer complete after {covered} of {} bytes",
                    package.bytes.len()
                )));
            }
            return Ok(Ok(()));
        }
        if let Some(rc) = status.filter(|rc| *rc != 0) {
            report(&frame);
            return Ok(Err(format!("the watch answered rc {rc} at {covered} bytes")));
        }
        let Some(request) = request else {
            // A status with no request acknowledges a served window; the next
            // one comes from announcing the same package again.
            announce(link, package)?;
            continue;
        };
        if request.size == 0 {
            return Ok(Err(format!("the watch asked for no bytes at offset {}", request.offset)));
        }
        serve(link, package, &request)?;
        covered += request.size as usize;
        if covered >= package.bytes.len() {
            println!("-> {covered} of {} bytes served", package.bytes.len());
        }
    }
}

fn serve(link: &mut Link, package: &Package, request: &FwChunkRequest) -> io::Result<()> {
    let start = request.offset as usize;
    let end = start + request.size as usize;
    if end > package.bytes.len() {
        panic!(
            "the watch asked for {start:#x}..{end:#x}, past the {:#x} byte package it was told about",
            package.bytes.len()
        );
    }
    // The watch's packet_max_size is a u8 and the frame cap is 200 bytes, so
    // the chunk size is whichever of the two binds first.
    let cap = (wpp::frame::MAX_FRAME_BYTES - FRAME_OVERHEAD).min(request.packet_max_size as usize);
    if cap == 0 {
        panic!("the watch asked for chunks of no bytes (packet_max_size 0)");
    }
    println!(
        "-> serving {:#x}..{:#x} in {cap} byte chunks ({}%)",
        start, end, request.percent
    );
    let mut objects: Vec<WppObject> = package.bytes[start..end]
        .chunks(cap)
        .enumerate()
        .map(|(index, packet)| {
            WppObject::FwChunk(FwChunk {
                offset: (start + index * cap) as u32,
                packet: packet.to_vec(),
            })
        })
        .collect();
    objects.push(WppObject::Null(Null {}));
    link.send_quietly(&Frame::new(Command::CMD_REQUEST_FW_CHUNK, objects))
}

pub fn restart(link: &mut Link) -> io::Result<()> {
    println!("\n== restart to update ==");
    link.send(&Frame::new(Command::CMD_RESTART_TO_UPDATE, Vec::new()))
}
