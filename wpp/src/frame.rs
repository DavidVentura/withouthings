use crate::codec::{ParseError, Writer};
use crate::commands::Command;
use crate::objects::WppObject;

pub const PROTOCOL_VERSION: u8 = 0x01;
pub const HEADER_LEN: usize = 5;
pub const OBJECT_HEADER_LEN: usize = 4;

const CHANNEL_MASK: u16 = 0xC000;
const OPCODE_MASK: u16 = 0x3FFF;

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
    pub fn opcode(self) -> u16 {
        self.0 & OPCODE_MASK
    }

    pub fn channel(self) -> Option<Channel> {
        Channel::from_bits(self.0 & CHANNEL_MASK)
    }

    pub fn with_channel(self, channel: Channel) -> Command {
        Command(self.opcode() | channel.bits())
    }

    pub fn opcode_name(self) -> Option<&'static str> {
        Command(self.opcode()).name()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FrameError {
    ShortHeader { available: usize },
    UnsupportedVersion { found: u8 },
    IncompletePayload { declared: usize, available: usize },
    PayloadLengthMismatch { declared: usize, consumed: usize },
    TrailingData { remaining: usize },
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

/// Over this many bytes the watch panics and reboots several seconds later;
/// anything longer must be split across frames of the same command, which the
/// watch accumulates.
pub const MAX_FRAME_BYTES: usize = 200;

impl Frame {
    pub fn new(command: Command, objects: Vec<WppObject>) -> Frame {
        Frame { command, objects }
    }

    pub fn to_wire(&self) -> Vec<Frame> {
        if HEADER_LEN + self.payload_len() <= MAX_FRAME_BYTES {
            return vec![self.clone()];
        }
        let mut frames = Vec::new();
        let mut batch: Vec<WppObject> = Vec::new();
        let mut used = HEADER_LEN;
        for object in &self.objects {
            let cost = OBJECT_HEADER_LEN + object.data_size();
            if !batch.is_empty() && used + cost > MAX_FRAME_BYTES {
                frames.push(Frame::new(self.command, std::mem::take(&mut batch)));
                used = HEADER_LEN;
            }
            used += cost;
            batch.push(object.clone());
        }
        if !batch.is_empty() {
            frames.push(Frame::new(self.command, batch));
        }
        frames
    }

    pub fn payload_len(&self) -> usize {
        self.objects
            .iter()
            .map(|o| OBJECT_HEADER_LEN + o.data_size())
            .sum()
    }

    pub fn declared_len(buf: &[u8]) -> Option<usize> {
        if buf.len() < HEADER_LEN {
            return None;
        }
        Some(HEADER_LEN + u16::from_be_bytes([buf[3], buf[4]]) as usize)
    }

    pub fn declared_command(buf: &[u8]) -> Option<u16> {
        if buf.len() < HEADER_LEN {
            return None;
        }
        Some(u16::from_be_bytes([buf[1], buf[2]]))
    }

    pub fn splice_offset(buf: &[u8]) -> Option<usize> {
        (1..buf.len().saturating_sub(HEADER_LEN)).find(|&at| {
            buf[at] == PROTOCOL_VERSION
                && Command(u16::from_be_bytes([buf[at + 1], buf[at + 2]]))
                    .opcode_name()
                    .is_some()
        })
    }

    pub fn parse(buf: &[u8]) -> Result<Frame, FrameError> {
        let (frame, rest) = Frame::parse_prefix(buf)?;
        if !rest.is_empty() {
            return Err(FrameError::TrailingData {
                remaining: rest.len(),
            });
        }
        Ok(frame)
    }

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

    pub fn malformed(&self) -> impl Iterator<Item = (u16, &ParseError)> {
        self.objects.iter().filter_map(|o| match o {
            WppObject::Malformed { type_id, error, .. } => Some((*type_id, error)),
            _ => None,
        })
    }

    pub fn unknown(&self) -> impl Iterator<Item = u16> + '_ {
        self.objects.iter().filter_map(|o| match o {
            WppObject::Unknown { type_id, .. } => Some(*type_id),
            _ => None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::ImageData;

    fn sized(total: usize) -> Frame {
        Frame::new(
            Command::CMD_WORKOUT_SCREEN_SET,
            vec![WppObject::ImageData(ImageData {
                data: vec![0; total - HEADER_LEN - OBJECT_HEADER_LEN - 1],
            })],
        )
    }

    #[test]
    fn the_limit_counts_the_header_too() {
        assert_eq!(sized(MAX_FRAME_BYTES).to_bytes().len(), MAX_FRAME_BYTES);
        assert_eq!(sized(MAX_FRAME_BYTES).to_wire().len(), 1);
    }

    #[test]
    fn one_byte_over_takes_two_frames() {
        let frame = Frame::new(
            Command::CMD_WORKOUT_SCREEN_SET,
            vec![
                WppObject::ImageData(ImageData {
                    data: vec![0; MAX_FRAME_BYTES - HEADER_LEN - OBJECT_HEADER_LEN - 1],
                }),
                WppObject::ImageData(ImageData { data: Vec::new() }),
            ],
        );
        let wire = frame.to_wire();
        assert_eq!(wire.len(), 2, "a frame past the limit has to be split");
        assert_eq!(wire[0].to_bytes().len(), MAX_FRAME_BYTES);
        for piece in &wire {
            assert!(piece.to_bytes().len() <= MAX_FRAME_BYTES);
        }
    }

    #[test]
    fn an_object_larger_than_a_frame_goes_rather_than_vanishing() {
        let frame = sized(MAX_FRAME_BYTES + 1);
        assert_eq!(frame.objects.len(), 1);
        let wire = frame.to_wire();
        assert_eq!(wire.len(), 1);
        assert_eq!(wire[0].objects, frame.objects);
    }
}
