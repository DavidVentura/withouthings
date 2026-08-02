//! Draining the watch's diagnostic buffer.
//!
//! The watch holds firmware telemetry it wants handed over, and asks for a
//! sync once a minute for as long as it holds any. Acknowledging the request
//! stops it repeating within the minute but not the asking: only taking the
//! data and acknowledging the transfer lets the watch drop it.
//!
//! The exchange is a chain. Each round selects the categories, asks from an
//! anchor, receives a type header followed by data, and ends with a frame
//! carrying a `Null` — with the anchor for the next round if there is one,
//! without it when the buffer is empty. The anchor is the watch's cursor, not
//! ours; we only ever echo back what it last gave us.
//!
//! Nothing is kept. The data is device telemetry for Withings' own servers,
//! and the point of collecting it here is that the watch stops holding it.

use crate::objects::{DebugDumpAnchor, DebugDumpMask};
use crate::{Command, Frame, WppObject};

/// Which parts the watch will hand over: bit 0 dblib, bit 1 wlog, bit 4 the
/// per-second sensor records. The official app asks for all three
/// (`0x20000013`); bit 4 alone is 2.68 MB against a few kilobytes for the rest,
/// and none of it is kept, so it is left out. Bit 29 is set for no reason the
/// firmware can act on, and is carried anyway to stay close to the official
/// app.
///
/// `CMD_DEBUG_SET` stores this on the watch rather than filtering one
/// transfer, so it outlives the connection.
const DUMP_MASK: u32 = 0x2000_0003;

/// Where a drain starts before the watch has given us a cursor. Health Mate
/// opens with one it kept from an earlier session; a client that has never
/// drained has nothing to open with.
const FIRST_ANCHOR: u32 = 0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Idle,
    /// Mask sent, waiting for the watch to take it.
    Selecting(u32),
    /// Asked from an anchor, taking data until the round ends.
    Receiving,
    /// Round acknowledged; `next` is where the following round starts, if the
    /// watch said there is one.
    Acking {
        next: Option<u32>,
    },
}

#[derive(Debug, Clone)]
pub struct DebugDump {
    state: State,
}

impl DebugDump {
    pub fn new() -> DebugDump {
        DebugDump { state: State::Idle }
    }

    pub fn running(&self) -> bool {
        self.state != State::Idle
    }

    /// Begin a drain. Does nothing if one is already under way.
    pub fn start(&mut self) -> Vec<Frame> {
        if self.running() {
            return Vec::new();
        }
        self.select(FIRST_ANCHOR)
    }

    /// A link carries the whole chain or none of it: an anchor is the watch's
    /// cursor into a transfer this connection was having, and means nothing to
    /// the next one.
    pub fn reset(&mut self) {
        self.state = State::Idle;
    }

    /// Advance the drain on a frame from the watch. Frames belonging to
    /// anything else leave it untouched.
    pub fn on_frame(&mut self, frame: &Frame) -> Vec<Frame> {
        let command = frame.command.opcode();
        // An error naming one of these ends the drain rather than leaving it
        // waiting for data that is not coming.
        let refused = frame.objects.iter().any(|o| {
            matches!(o, WppObject::Cmderror(e)
                if e.cmd == Command::CMD_DEBUG_SET.0
                    || e.cmd == Command::CMD_DEBUG_DUMP.0
                    || e.cmd == Command::CMD_DEBUG_DUMP_ACK.0)
        });
        if refused {
            self.state = State::Idle;
            return Vec::new();
        }

        match self.state {
            State::Idle => Vec::new(),
            State::Selecting(anchor) if command == Command::CMD_DEBUG_SET.0 => {
                self.state = State::Receiving;
                vec![Frame::new(
                    Command::CMD_DEBUG_DUMP,
                    vec![WppObject::DebugDumpAnchor(DebugDumpAnchor {
                        value: anchor,
                    })],
                )]
            }
            State::Receiving if command == Command::CMD_DEBUG_DUMP.0 => {
                // The header and the data carry no `Null`; the frame that does
                // is the end of the round, and the only thing worth reading out
                // of the whole transfer is the anchor beside it.
                if !frame
                    .objects
                    .iter()
                    .any(|o| matches!(o, WppObject::Null(_)))
                {
                    return Vec::new();
                }
                let next = frame.objects.iter().find_map(|o| match o {
                    WppObject::DebugDumpAnchor(a) => Some(a.value),
                    _ => None,
                });
                self.state = State::Acking { next };
                vec![Frame::new(Command::CMD_DEBUG_DUMP_ACK, Vec::new())]
            }
            State::Acking { next } if command == Command::CMD_DEBUG_DUMP_ACK.0 => match next {
                Some(anchor) => self.select(anchor),
                None => {
                    self.state = State::Idle;
                    Vec::new()
                }
            },
            _ => Vec::new(),
        }
    }

    fn select(&mut self, anchor: u32) -> Vec<Frame> {
        self.state = State::Selecting(anchor);
        vec![Frame::new(
            Command::CMD_DEBUG_SET,
            vec![WppObject::DebugDumpMask(DebugDumpMask { mask: DUMP_MASK })],
        )]
    }
}

impl Default for DebugDump {
    fn default() -> Self {
        DebugDump::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::{DebugDumpData, DebugDumpType, Null};

    fn reply(command: Command, objects: Vec<WppObject>) -> Frame {
        Frame::new(command, objects)
    }

    fn sent(frames: &[Frame]) -> Vec<u16> {
        frames.iter().map(|f| f.command.opcode()).collect()
    }

    /// One round, end to end, with no second anchor: select, ask, take the
    /// data, acknowledge, stop.
    #[test]
    fn a_single_round_drains_and_ends() {
        let mut dump = DebugDump::new();
        assert_eq!(sent(&dump.start()), vec![Command::CMD_DEBUG_SET.0]);
        assert!(dump.running());

        let asked = dump.on_frame(&reply(
            Command::CMD_DEBUG_SET,
            vec![WppObject::Null(Null {})],
        ));
        assert_eq!(sent(&asked), vec![Command::CMD_DEBUG_DUMP.0]);
        assert!(matches!(
            asked[0].objects[0],
            WppObject::DebugDumpAnchor(DebugDumpAnchor {
                value: FIRST_ANCHOR
            })
        ));

        let header = reply(
            Command::CMD_DEBUG_DUMP,
            vec![WppObject::DebugDumpType(DebugDumpType {
                r#type: 3,
                size: 128,
            })],
        );
        assert!(
            dump.on_frame(&header).is_empty(),
            "a header answers nothing"
        );
        let data = reply(
            Command::CMD_DEBUG_DUMP,
            vec![WppObject::DebugDumpData(DebugDumpData { buf: vec![0; 64] })],
        );
        assert!(dump.on_frame(&data).is_empty(), "nor does data");

        let end = reply(Command::CMD_DEBUG_DUMP, vec![WppObject::Null(Null {})]);
        assert_eq!(
            sent(&dump.on_frame(&end)),
            vec![Command::CMD_DEBUG_DUMP_ACK.0]
        );

        let done = reply(Command::CMD_DEBUG_DUMP_ACK, vec![WppObject::Null(Null {})]);
        assert!(dump.on_frame(&done).is_empty(), "nothing left to ask for");
        assert!(!dump.running());
    }

    /// The watch hands over the next cursor beside the `Null` that ends a
    /// round. Echoing it back is the whole of how the chain continues.
    #[test]
    fn the_anchor_the_watch_gives_back_opens_the_next_round() {
        let mut dump = DebugDump::new();
        dump.start();
        dump.on_frame(&reply(
            Command::CMD_DEBUG_SET,
            vec![WppObject::Null(Null {})],
        ));

        let end = reply(
            Command::CMD_DEBUG_DUMP,
            vec![
                WppObject::DebugDumpAnchor(DebugDumpAnchor { value: 2910826012 }),
                WppObject::Null(Null {}),
            ],
        );
        dump.on_frame(&end);
        let again = dump.on_frame(&reply(
            Command::CMD_DEBUG_DUMP_ACK,
            vec![WppObject::Null(Null {})],
        ));
        assert_eq!(sent(&again), vec![Command::CMD_DEBUG_SET.0]);

        let asked = dump.on_frame(&reply(
            Command::CMD_DEBUG_SET,
            vec![WppObject::Null(Null {})],
        ));
        assert!(
            matches!(asked[0].objects[0], WppObject::DebugDumpAnchor(DebugDumpAnchor { value })
                if value == 2910826012),
            "the next round asks from where the watch said"
        );
    }

    /// A refusal has to end the drain. Left waiting for data that is not
    /// coming, it would block every later one for the life of the link.
    #[test]
    fn a_refusal_ends_the_drain() {
        use crate::objects::Cmderror;
        let mut dump = DebugDump::new();
        dump.start();
        let error = reply(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror {
                cmd: Command::CMD_DEBUG_DUMP.0,
                err: -3,
            })],
        );
        assert!(dump.on_frame(&error).is_empty());
        assert!(!dump.running());
    }
}
