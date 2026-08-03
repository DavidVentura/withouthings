use crate::objects::{DebugDumpAnchor, DebugDumpMask};
use crate::{Command, Frame, WppObject};

const DUMP_MASK: u32 = 0x2000_0003;

const FIRST_ANCHOR: u32 = 0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Idle,
    Selecting(u32),
    Receiving,
    Acking { next: Option<u32> },
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

    pub fn start(&mut self) -> Vec<Frame> {
        if self.running() {
            return Vec::new();
        }
        self.select(FIRST_ANCHOR)
    }

    pub fn reset(&mut self) {
        self.state = State::Idle;
    }

    pub fn on_frame(&mut self, frame: &Frame) -> Vec<Frame> {
        let command = frame.command.opcode();
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
