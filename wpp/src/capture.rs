use crate::frame::{Frame, FrameError, PROTOCOL_VERSION};
use crate::units::UnixMillis;
use std::collections::HashMap;

const BTSNOOP_EPOCH_US: i64 = 0x00dc_ddb3_0f2f_8000;

const BTSNOOP_MAGIC: &[u8] = b"btsnoop\0";
const BTSNOOP_HEADER_LEN: usize = 16;
const RECORD_HEADER_LEN: usize = 24;

const H4_ACL: u8 = 0x02;
const L2CAP_CID_ATT: u16 = 0x0004;

const ATT_WRITE_REQUEST: u8 = 0x12;
const ATT_WRITE_COMMAND: u8 = 0x52;
const ATT_HANDLE_VALUE_NOTIFICATION: u8 = 0x1b;
const ATT_HANDLE_VALUE_INDICATION: u8 = 0x1d;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Direction {
    Sent,
    Received,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CaptureError {
    NotBtsnoop,
    UnsupportedDatalink { found: u32 },
    Truncated { at: usize },
}

impl core::fmt::Display for CaptureError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            CaptureError::NotBtsnoop => {
                write!(f, "not a btsnoop file (expected a \"btsnoop\\0\" magic)")
            }
            CaptureError::UnsupportedDatalink { found } => {
                write!(
                    f,
                    "unsupported btsnoop datalink type {found}, expected 1002 (HCI H4)"
                )
            }
            CaptureError::Truncated { at } => write!(f, "file truncated at byte {at}"),
        }
    }
}

impl std::error::Error for CaptureError {}

#[derive(Debug, Clone, PartialEq)]
pub struct AttPacket {
    pub direction: Direction,
    pub att_handle: u16,
    pub received_at: UnixMillis,
    pub value: Vec<u8>,
}

pub fn att_packets(file: &[u8]) -> Result<Vec<AttPacket>, CaptureError> {
    if file.len() < BTSNOOP_HEADER_LEN || &file[..8] != BTSNOOP_MAGIC {
        return Err(CaptureError::NotBtsnoop);
    }
    let datalink = u32::from_be_bytes(file[12..16].try_into().unwrap());
    if datalink != 1002 {
        return Err(CaptureError::UnsupportedDatalink { found: datalink });
    }

    let mut packets = Vec::new();
    let mut acl: HashMap<(Direction, u16), Vec<u8>> = HashMap::new();
    let mut pos = BTSNOOP_HEADER_LEN;

    while pos < file.len() {
        if pos + RECORD_HEADER_LEN > file.len() {
            return Err(CaptureError::Truncated { at: pos });
        }
        let included = u32::from_be_bytes(file[pos + 4..pos + 8].try_into().unwrap()) as usize;
        let flags = u32::from_be_bytes(file[pos + 8..pos + 12].try_into().unwrap());
        let stamp = i64::from_be_bytes(file[pos + 16..pos + 24].try_into().unwrap());
        let received_at = UnixMillis((stamp - BTSNOOP_EPOCH_US) / 1_000);
        let start = pos + RECORD_HEADER_LEN;
        if start + included > file.len() {
            return Err(CaptureError::Truncated { at: start });
        }
        let packet = &file[start..start + included];
        pos = start + included;

        let direction = if flags & 0x01 == 0 {
            Direction::Sent
        } else {
            Direction::Received
        };
        if packet.first() != Some(&H4_ACL) || packet.len() < 5 {
            continue;
        }

        let handle_flags = u16::from_le_bytes([packet[1], packet[2]]);
        let acl_handle = handle_flags & 0x0fff;
        let continuation = (handle_flags >> 12) & 0x03 == 0x01;
        let acl_len = u16::from_le_bytes([packet[3], packet[4]]) as usize;
        let body = &packet[5..packet.len().min(5 + acl_len)];

        let slot = acl.entry((direction, acl_handle)).or_default();
        if continuation {
            slot.extend_from_slice(body);
        } else {
            slot.clear();
            slot.extend_from_slice(body);
        }

        if slot.len() < 4 {
            continue;
        }
        let l2cap_len = u16::from_le_bytes([slot[0], slot[1]]) as usize;
        let cid = u16::from_le_bytes([slot[2], slot[3]]);
        if slot.len() < 4 + l2cap_len {
            continue;
        }
        let l2cap_payload = slot[4..4 + l2cap_len].to_vec();
        slot.clear();

        if cid != L2CAP_CID_ATT || l2cap_payload.len() < 3 {
            continue;
        }
        let opcode = l2cap_payload[0];
        if !matches!(
            opcode,
            ATT_WRITE_REQUEST
                | ATT_WRITE_COMMAND
                | ATT_HANDLE_VALUE_NOTIFICATION
                | ATT_HANDLE_VALUE_INDICATION
        ) {
            continue;
        }
        packets.push(AttPacket {
            direction,
            att_handle: u16::from_le_bytes([l2cap_payload[1], l2cap_payload[2]]),
            received_at,
            value: l2cap_payload[3..].to_vec(),
        });
    }
    Ok(packets)
}

#[derive(Debug, Clone, PartialEq)]
pub enum StreamItem {
    Frame { frame: Frame, bytes: Vec<u8> },
    Desync { bytes: Vec<u8>, cause: FrameError },
}

#[derive(Default)]
pub struct FrameReassembler {
    buf: Vec<u8>,
}

impl FrameReassembler {
    pub fn new() -> Self {
        FrameReassembler::default()
    }

    pub fn pending(&self) -> usize {
        self.buf.len()
    }

    pub fn reset(&mut self) {
        self.buf.clear();
    }

    pub fn push(&mut self, bytes: &[u8]) -> Vec<StreamItem> {
        self.buf.extend_from_slice(bytes);
        let mut items = Vec::new();
        loop {
            if let Some(&first) = self.buf.first() {
                if first != PROTOCOL_VERSION {
                    self.buf.remove(0);
                    items.push(StreamItem::Desync {
                        bytes: vec![first],
                        cause: FrameError::UnsupportedVersion { found: first },
                    });
                    continue;
                }
            }
            let Some(needed) = Frame::declared_len(&self.buf) else {
                return items;
            };
            if self.buf.len() < needed {
                return items;
            }
            match Frame::parse(&self.buf[..needed]) {
                Ok(frame) => {
                    let bytes: Vec<u8> = self.buf.drain(..needed).collect();
                    items.push(StreamItem::Frame { frame, bytes });
                }
                Err(cause) => {
                    let bytes: Vec<u8> = self.buf.drain(..needed).collect();
                    items.push(StreamItem::Desync { bytes, cause });
                }
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Captured {
    pub direction: Direction,
    pub att_handle: u16,
    pub received_at: UnixMillis,
    pub item: StreamItem,
}

pub fn frames(file: &[u8]) -> Result<Vec<Captured>, CaptureError> {
    let mut streams: HashMap<(Direction, u16), FrameReassembler> = HashMap::new();
    let mut out = Vec::new();
    for packet in att_packets(file)? {
        let key = (packet.direction, packet.att_handle);
        let reassembler = streams.entry(key).or_insert_with(FrameReassembler::new);
        for item in reassembler.push(&packet.value) {
            out.push(Captured {
                direction: key.0,
                att_handle: key.1,
                received_at: packet.received_at,
                item,
            });
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::BatteryStatus;
    use crate::{Command, WppObject};

    fn battery_frame() -> Frame {
        Frame::new(
            Command::CMD_BATTERY_STATUS,
            vec![WppObject::BatteryStatus(BatteryStatus {
                battery_percent: 62,
                battery_state: BatteryStatus::BATTERY_STATE_OK,
                battery_mv: 4180,
                reserved: 0,
            })],
        )
    }

    #[test]
    fn a_frame_split_across_notifications_is_rejoined() {
        let bytes = battery_frame().to_bytes();
        let mut r = FrameReassembler::new();
        assert!(r.push(&bytes[..4]).is_empty());
        assert!(r.push(&bytes[4..9]).is_empty());
        assert_eq!(
            r.push(&bytes[9..]),
            vec![StreamItem::Frame {
                frame: battery_frame(),
                bytes: bytes.clone()
            }]
        );
        assert_eq!(r.pending(), 0);
    }

    #[test]
    fn back_to_back_frames_in_one_notification_both_decode() {
        let one = battery_frame().to_bytes();
        let mut bytes = one.clone();
        bytes.extend(&one);
        let mut r = FrameReassembler::new();
        assert_eq!(
            r.push(&bytes),
            vec![
                StreamItem::Frame {
                    frame: battery_frame(),
                    bytes: one.clone()
                },
                StreamItem::Frame {
                    frame: battery_frame(),
                    bytes: one.clone()
                },
            ]
        );
    }

    #[test]
    fn a_frame_missing_a_notification_is_consumed_whole_and_shows_the_join() {
        let whole = battery_frame().to_bytes();
        let mut stream = whole[..7].to_vec();
        stream.extend(&whole);
        stream.extend(&whole);

        let mut r = FrameReassembler::new();
        let items = r.push(&stream);
        let StreamItem::Desync { bytes, .. } = &items[0] else {
            panic!("the spliced frame should not decode: {items:?}");
        };
        assert_eq!(
            bytes.len(),
            whole.len(),
            "exactly what the header declared is taken, no more and no less"
        );
        assert_eq!(
            Frame::splice_offset(bytes),
            Some(7),
            "the second frame's header is where the first one's body ran out"
        );
    }

    #[test]
    fn a_frame_that_is_merely_unreadable_shows_no_join() {
        let mut bytes = battery_frame().to_bytes();
        bytes[8] = 0xff;
        assert!(Frame::parse(&bytes).is_err());
        assert_eq!(Frame::splice_offset(&bytes), None);
    }

    #[test]
    fn leading_garbage_is_reported_and_skipped() {
        let mut bytes = vec![0xff, 0xff];
        bytes.extend(battery_frame().to_bytes());
        let mut r = FrameReassembler::new();
        let items = r.push(&bytes);
        assert_eq!(items.len(), 3);
        assert!(matches!(items[0], StreamItem::Desync { .. }));
        assert!(matches!(items[1], StreamItem::Desync { .. }));
        assert_eq!(
            items[2],
            StreamItem::Frame {
                frame: battery_frame(),
                bytes: battery_frame().to_bytes()
            }
        );
    }
}
