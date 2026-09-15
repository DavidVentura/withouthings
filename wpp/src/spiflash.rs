use crate::objects::{Cmderror, SpiFlashCmd};
use crate::{Command, Frame, WppObject};

/// How a caller-driven external-flash read stands: what has arrived so far,
/// what was asked for, and whether it finished or the watch refused it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SpiFlashProgress {
    pub received: usize,
    pub total: usize,
    pub done: bool,
    pub error: Option<i32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Outcome {
    Reading,
    Done,
    Refused(i32),
}

#[derive(Debug, Clone)]
struct Read {
    len: usize,
    buf: Vec<u8>,
    outcome: Outcome,
}

/// The read side of `CMD_SPI_FLASH`. The watch streams the requested span back
/// as 16-byte `SpiFlashChunk`s ended by a `TYPE_NULL`, driving the loop itself,
/// so this only has to accumulate the chunks of the one read in flight.
#[derive(Debug, Clone, Default)]
pub struct SpiFlash {
    active: Option<Read>,
}

impl SpiFlash {
    pub fn new() -> SpiFlash {
        SpiFlash { active: None }
    }

    pub fn reset(&mut self) {
        self.active = None;
    }

    /// Opens a fresh read of `len` bytes at `addr` and yields the frame that
    /// starts it. Any read still in flight is dropped: resending is how the
    /// shell recovers a block the watch never finished. Only `cmd == 0` (read)
    /// is exposed; the handler answers anything else with `-4`.
    pub fn start(&mut self, addr: u32, len: u32) -> Frame {
        self.active = Some(Read {
            len: len as usize,
            buf: Vec::with_capacity(len as usize),
            outcome: Outcome::Reading,
        });
        Frame::new(
            Command::CMD_SPI_FLASH,
            vec![WppObject::SpiFlashCmd(SpiFlashCmd {
                cmd: 0,
                arg1: addr,
                arg2: len,
                arg3: 0,
            })],
        )
    }

    pub fn on_frame(&mut self, frame: &Frame) {
        let Some(read) = self.active.as_mut() else {
            return;
        };
        if read.outcome != Outcome::Reading {
            return;
        }
        if let Some(err) = frame.objects.iter().find_map(|o| match o {
            WppObject::Cmderror(Cmderror { cmd, err }) if *cmd == Command::CMD_SPI_FLASH.0 => {
                Some(*err)
            }
            _ => None,
        }) {
            read.outcome = Outcome::Refused(err);
            return;
        }
        if frame.command.opcode() != Command::CMD_SPI_FLASH.0 {
            return;
        }
        for object in &frame.objects {
            match object {
                WppObject::SpiFlashChunk(chunk) => read.buf.extend_from_slice(&chunk.chunk),
                WppObject::Null(_) => read.outcome = Outcome::Done,
                _ => {}
            }
        }
    }

    pub fn progress(&self) -> Option<SpiFlashProgress> {
        let read = self.active.as_ref()?;
        Some(SpiFlashProgress {
            received: read.buf.len().min(read.len),
            total: read.len,
            done: read.outcome == Outcome::Done,
            error: match read.outcome {
                Outcome::Refused(err) => Some(err),
                _ => None,
            },
        })
    }

    /// The bytes of a finished read, taken once and the reader left idle. The
    /// buffer is trimmed to the requested length: the watch steps by 16
    /// whatever the length, so an unaligned read carries up to 15 bytes past
    /// the end.
    pub fn take(&mut self) -> Option<Vec<u8>> {
        if self.active.as_ref()?.outcome != Outcome::Done {
            return None;
        }
        let mut read = self.active.take().unwrap();
        read.buf.truncate(read.len);
        Some(read.buf)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::{Null, SpiFlashChunk};

    fn reply(objects: Vec<WppObject>) -> Frame {
        Frame::new(Command::CMD_SPI_FLASH, objects)
    }

    fn chunk(bytes: &[u8]) -> WppObject {
        WppObject::SpiFlashChunk(SpiFlashChunk {
            chunk: bytes.to_vec(),
        })
    }

    #[test]
    fn chunks_accumulate_until_the_null_closes_the_read() {
        let mut flash = SpiFlash::new();
        let frame = flash.start(0x6000, 32);
        assert_eq!(frame.command.opcode(), Command::CMD_SPI_FLASH.0);

        flash.on_frame(&reply(vec![chunk(&[1; 16]), chunk(&[2; 16])]));
        let progress = flash.progress().unwrap();
        assert_eq!(progress.received, 32);
        assert!(!progress.done);
        assert!(flash.take().is_none(), "not done, nothing to take");

        flash.on_frame(&reply(vec![WppObject::Null(Null {})]));
        assert!(flash.progress().unwrap().done);
        let bytes = flash.take().unwrap();
        assert_eq!(bytes.len(), 32);
        assert_eq!(&bytes[..16], &[1; 16]);
        assert!(flash.progress().is_none(), "taken read leaves it idle");
    }

    #[test]
    fn an_unaligned_read_is_trimmed_to_the_asked_length() {
        let mut flash = SpiFlash::new();
        flash.start(0, 20);
        flash.on_frame(&reply(vec![chunk(&[7; 16]), chunk(&[7; 16])]));
        flash.on_frame(&reply(vec![WppObject::Null(Null {})]));
        assert_eq!(flash.take().unwrap().len(), 20);
    }

    #[test]
    fn a_refusal_is_surfaced_and_stops_collecting() {
        let mut flash = SpiFlash::new();
        flash.start(0, 16);
        flash.on_frame(&Frame::new(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror {
                cmd: Command::CMD_SPI_FLASH.0,
                err: -4,
            })],
        ));
        assert_eq!(flash.progress().unwrap().error, Some(-4));
        flash.on_frame(&reply(vec![chunk(&[1; 16])]));
        assert_eq!(flash.progress().unwrap().received, 0, "refused stays refused");
        assert!(flash.take().is_none());
    }

    #[test]
    fn a_read_ignores_frames_when_idle() {
        let mut flash = SpiFlash::new();
        flash.on_frame(&reply(vec![chunk(&[1; 16])]));
        assert!(flash.progress().is_none());
    }
}
