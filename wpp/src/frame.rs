//! WPP framing.
//!
//! A frame is `[version u8][command u16][payload_len u16][payload]`, and the
//! payload is a run of `[type u16][size u16][data size]` objects. Everything is
//! big-endian.

use crate::codec::{ParseError, Writer};
use crate::commands::Command;
use crate::objects::WppObject;

pub const PROTOCOL_VERSION: u8 = 0x01;
pub const HEADER_LEN: usize = 5;
pub const OBJECT_HEADER_LEN: usize = 4;

const CHANNEL_MASK: u16 = 0xC000;
const OPCODE_MASK: u16 = 0x3FFF;

/// The two high bits of a command id, from `Wpp.CMD_CHANNEL_*`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Channel {
    MasterRequest,
    SlaveRequest,
    Notification,
}

impl Channel {
    fn from_bits(bits: u16) -> Option<Channel> {
        match bits {
            0x0000 => Some(Channel::MasterRequest),
            0x4000 => Some(Channel::SlaveRequest),
            0x8000 => Some(Channel::Notification),
            _ => None,
        }
    }

    fn bits(self) -> u16 {
        match self {
            Channel::MasterRequest => 0x0000,
            Channel::SlaveRequest => 0x4000,
            Channel::Notification => 0x8000,
        }
    }
}

impl Command {
    /// The command id without its channel bits, which is what the `CMD_*`
    /// constants name.
    pub fn opcode(self) -> u16 {
        self.0 & OPCODE_MASK
    }

    pub fn channel(self) -> Option<Channel> {
        Channel::from_bits(self.0 & CHANNEL_MASK)
    }

    pub fn with_channel(self, channel: Channel) -> Command {
        Command(self.opcode() | channel.bits())
    }

    /// Name of the underlying command, ignoring channel bits.
    pub fn opcode_name(self) -> Option<&'static str> {
        Command(self.opcode()).name()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FrameError {
    /// Fewer than `HEADER_LEN` bytes; nothing can be decided yet.
    ShortHeader {
        available: usize,
    },
    UnsupportedVersion {
        found: u8,
    },
    /// The declared payload extends past the end of the buffer. Callers doing
    /// BLE reassembly should treat this as "need more data".
    IncompletePayload {
        declared: usize,
        available: usize,
    },
    /// The payload's object sizes do not add up to the declared length.
    PayloadLengthMismatch {
        declared: usize,
        consumed: usize,
    },
    /// Bytes remained after a frame that was expected to fill the buffer.
    TrailingData {
        remaining: usize,
    },
}

impl core::fmt::Display for FrameError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            FrameError::ShortHeader { available } => {
                write!(f, "short header: {available} of {HEADER_LEN} bytes")
            }
            FrameError::UnsupportedVersion { found } => {
                write!(f, "unsupported protocol version 0x{found:02x}")
            }
            FrameError::IncompletePayload {
                declared,
                available,
            } => {
                write!(
                    f,
                    "incomplete payload: declared {declared}, have {available}"
                )
            }
            FrameError::PayloadLengthMismatch { declared, consumed } => {
                write!(
                    f,
                    "payload length mismatch: declared {declared}, objects use {consumed}"
                )
            }
            FrameError::TrailingData { remaining } => {
                write!(f, "{remaining} bytes after the frame")
            }
        }
    }
}

impl std::error::Error for FrameError {}

#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub command: Command,
    pub objects: Vec<WppObject>,
}

impl Frame {
    pub fn new(command: Command, objects: Vec<WppObject>) -> Frame {
        Frame { command, objects }
    }

    pub fn payload_len(&self) -> usize {
        self.objects
            .iter()
            .map(|o| OBJECT_HEADER_LEN + o.data_size())
            .sum()
    }

    /// Total frame length declared by a header prefix, for BLE reassembly.
    /// Returns `None` until `HEADER_LEN` bytes are available.
    pub fn declared_len(buf: &[u8]) -> Option<usize> {
        if buf.len() < HEADER_LEN {
            return None;
        }
        Some(HEADER_LEN + u16::from_be_bytes([buf[3], buf[4]]) as usize)
    }

    /// The command a buffer claims to carry, readable even when the body will
    /// not decode — which is exactly when it is worth knowing.
    pub fn declared_command(buf: &[u8]) -> Option<u16> {
        if buf.len() < HEADER_LEN {
            return None;
        }
        Some(u16::from_be_bytes([buf[1], buf[2]]))
    }

    /// Where a second frame begins inside this one, if it does.
    ///
    /// Frames span notifications once they outgrow the MTU, so one that goes
    /// missing leaves the head of a frame glued to the body of a later one.
    /// The join is visible: a header partway in separates a lost notification
    /// from a watch that sent something we cannot read — different faults with
    /// different cures.
    ///
    /// The second frame is usually cut off by the first one's declared length,
    /// so this cannot demand that it parses.
    pub fn splice_offset(buf: &[u8]) -> Option<usize> {
        (1..buf.len().saturating_sub(HEADER_LEN)).find(|&at| {
            buf[at] == PROTOCOL_VERSION
                && Command(u16::from_be_bytes([buf[at + 1], buf[at + 2]]))
                    .opcode_name()
                    .is_some()
        })
    }

    /// Decode one frame that fills `buf` exactly.
    pub fn parse(buf: &[u8]) -> Result<Frame, FrameError> {
        let (frame, rest) = Frame::parse_prefix(buf)?;
        if !rest.is_empty() {
            return Err(FrameError::TrailingData {
                remaining: rest.len(),
            });
        }
        Ok(frame)
    }

    /// Decode the frame at the start of `buf`, returning it with the bytes that
    /// follow it.
    pub fn parse_prefix(buf: &[u8]) -> Result<(Frame, &[u8]), FrameError> {
        if buf.len() < HEADER_LEN {
            return Err(FrameError::ShortHeader {
                available: buf.len(),
            });
        }
        if buf[0] != PROTOCOL_VERSION {
            return Err(FrameError::UnsupportedVersion { found: buf[0] });
        }

        let command = Command(u16::from_be_bytes([buf[1], buf[2]]));
        let declared = u16::from_be_bytes([buf[3], buf[4]]) as usize;
        let available = buf.len() - HEADER_LEN;
        if available < declared {
            return Err(FrameError::IncompletePayload {
                declared,
                available,
            });
        }

        let payload = &buf[HEADER_LEN..HEADER_LEN + declared];
        let mut objects = Vec::new();
        let mut pos = 0;
        while pos + OBJECT_HEADER_LEN <= payload.len() {
            let type_id = u16::from_be_bytes([payload[pos], payload[pos + 1]]);
            let size = u16::from_be_bytes([payload[pos + 2], payload[pos + 3]]) as usize;
            let start = pos + OBJECT_HEADER_LEN;
            if start + size > payload.len() {
                break;
            }
            objects.push(WppObject::parse(type_id, &payload[start..start + size]));
            pos = start + size;
        }

        if pos != declared {
            return Err(FrameError::PayloadLengthMismatch {
                declared,
                consumed: pos,
            });
        }
        Ok((Frame { command, objects }, &buf[HEADER_LEN + declared..]))
    }

    pub fn to_bytes(&self) -> Vec<u8> {
        let mut w = Writer::new();
        w.u8(PROTOCOL_VERSION);
        w.u16(self.command.0);
        w.u16(self.payload_len() as u16);
        for object in &self.objects {
            w.u16(object.type_id());
            w.u16(object.data_size() as u16);
            object.write_data(&mut w);
        }
        w.finish()
    }

    /// Objects whose type is known but whose bytes did not match the layout
    /// extracted from the app. A non-empty result means the extracted spec is
    /// wrong for that type.
    pub fn malformed(&self) -> impl Iterator<Item = (u16, &ParseError)> {
        self.objects.iter().filter_map(|o| match o {
            WppObject::Malformed { type_id, error, .. } => Some((*type_id, error)),
            _ => None,
        })
    }

    /// Objects carrying a type id the app has no class for.
    pub fn unknown(&self) -> impl Iterator<Item = u16> + '_ {
        self.objects.iter().filter_map(|o| match o {
            WppObject::Unknown { type_id, .. } => Some(*type_id),
            _ => None,
        })
    }
}
