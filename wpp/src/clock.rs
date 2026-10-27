//! The watch's own clock, as the hand-setting screen shows it.
//!
//! Read with `CMD_TIME_GET`: the firmware's handler for it (0x5e3e0 in the
//! renode-sim ABI notes) answers with a `TimeSet` object, the same layout the
//! phone writes: the UTC second the watch is at, its GMT offset and its next
//! DST change. The watch counts whole seconds, so a reading is good to about a
//! second plus the link's latency.
//!
//! Set with `CMD_TIME_SET`, which the watch answers with `TimeSetReply`. The
//! connection already sends one after every probe; a set asked for from the
//! screen waits for the link and for any open hand session (the hands are
//! parked at 12 for calibration then), and is read back once answered.

use crate::client::DstChange;
use crate::objects::Cmderror;
use crate::units::{UnixMillis, UnixTime};
use crate::{Command, Frame, WppObject};

/// How long a read or a set may go unanswered before the screen says so.
pub const ANSWER_WAIT_MS: i64 = 5_000;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Reading {
    /// The second the watch said it was at, UTC.
    pub watch_utc: UnixTime,
    pub gmt_offset: i32,
    pub dst_change_at: UnixTime,
    pub next_gmt_offset: i32,
    /// The phone's clock when the answer arrived.
    pub received_at: UnixMillis,
}

impl Reading {
    /// Watch minus phone, at the moment of the answer; positive when the
    /// watch is ahead.
    pub fn offset_ms(&self) -> i64 {
        self.watch_utc.0 * 1000 - self.received_at.0
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Sync {
    pub sent_at: UnixMillis,
    pub phone_utc: UnixTime,
    pub gmt_offset: i32,
    /// Asked for on the hand-setting screen, not the connection's own.
    pub from_screen: bool,
    /// How far from the phone's clock the time sent was (0 = phone time).
    pub shift_ms: i64,
    pub answered_at: Option<UnixMillis>,
    /// `TimeSetReply.drift`, as the watch sent it.
    pub drift: Option<i32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadState {
    Never,
    /// Asked for before the link was up; goes out when it is.
    WaitingForWatch,
    Asking,
    Answered,
    Unanswered,
    /// The watch answered with an error code.
    Refused(i32),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SetState {
    None,
    WaitingForHands,
    WaitingForWatch,
    Sending,
    Set,
    Unanswered,
    Refused(i32),
}

#[derive(Debug, Clone)]
pub struct WatchClock {
    reading: Option<Reading>,
    read_state: ReadState,
    asked_at: Option<UnixMillis>,
    last_sync: Option<Sync>,
    set_state: SetState,
    wanted: Option<(i32, Option<DstChange>)>,
    /// A custom time is kept as a fixed distance from the phone's clock, so
    /// the connection's own set on every reconnect keeps it too.
    shift_ms: i64,
}

impl Default for WatchClock {
    fn default() -> Self {
        Self::new()
    }
}

pub fn time_set_frame(utc: UnixTime, gmt_offset: i32, next_change: Option<DstChange>) -> Frame {
    let change = next_change.unwrap_or(DstChange {
        at: UnixTime(0),
        gmt_offset,
    });
    Frame::new(
        Command::CMD_TIME_SET,
        vec![WppObject::TimeSet(crate::objects::TimeSet {
            utc: utc.0 as u32,
            gmt_offset,
            dst_change_time: change.at.0 as u32,
            next_gmt_offset: change.gmt_offset,
        })],
    )
}

fn refusal(frame: &Frame, command: Command) -> Option<i32> {
    if frame.command.opcode() != Command::CMD_ERROR.0 {
        return None;
    }
    frame.objects.iter().find_map(|o| match o {
        WppObject::Cmderror(Cmderror { cmd, err }) if *cmd == command.0 => Some(*err),
        _ => None,
    })
}

impl WatchClock {
    pub fn new() -> Self {
        Self {
            reading: None,
            read_state: ReadState::Never,
            asked_at: None,
            last_sync: None,
            set_state: SetState::None,
            wanted: None,
            shift_ms: 0,
        }
    }

    /// The distance from the phone's clock the watch is being kept at.
    pub fn shift_ms(&self) -> i64 {
        self.shift_ms
    }

    pub fn reading(&self) -> Option<Reading> {
        self.reading
    }

    pub fn read_state(&self) -> ReadState {
        self.read_state
    }

    pub fn last_sync(&self) -> Option<Sync> {
        self.last_sync
    }

    pub fn set_state(&self) -> SetState {
        self.set_state
    }

    /// A read for when the link comes up; `pump` sends it.
    pub fn want_read(&mut self) {
        if self.read_state != ReadState::Asking {
            self.read_state = ReadState::WaitingForWatch;
        }
    }

    /// A set from the phone's clock, sent by `pump` once it is safe.
    pub fn want_set(&mut self, gmt_offset: i32, next_change: Option<DstChange>) {
        self.want_set_shifted(gmt_offset, next_change, 0);
    }

    /// A set to the phone's clock plus `shift_ms` (a custom time), kept for
    /// later sets until the next plain one.
    pub fn want_set_shifted(&mut self, gmt_offset: i32, next_change: Option<DstChange>, shift_ms: i64) {
        self.shift_ms = shift_ms;
        self.wanted = Some((gmt_offset, next_change));
        self.set_state = SetState::WaitingForWatch;
    }

    /// Sends what is waiting, given whether the watch is linked and whether a
    /// hand session holds it.
    pub fn pump(&mut self, now: UnixMillis, linked: bool, hands_open: bool) -> Vec<Frame> {
        let mut frames = Vec::new();
        if let Some((gmt_offset, next_change)) = self.wanted {
            if !linked {
                self.set_state = SetState::WaitingForWatch;
            } else if hands_open {
                self.set_state = SetState::WaitingForHands;
            } else {
                self.wanted = None;
                let utc = UnixMillis(now.0 + self.shift_ms).to_seconds();
                self.note_set(now, utc, gmt_offset, true);
                self.set_state = SetState::Sending;
                frames.push(time_set_frame(utc, gmt_offset, next_change));
                // Read back after the answer, not now: the watch takes them
                // in order, but the read is only meaningful once it is set.
                return frames;
            }
        }
        if linked && self.read_state == ReadState::WaitingForWatch {
            frames.extend(self.ask(now));
        }
        frames
    }

    fn ask(&mut self, now: UnixMillis) -> Vec<Frame> {
        self.read_state = ReadState::Asking;
        self.asked_at = Some(now);
        vec![Frame::new(Command::CMD_TIME_GET, Vec::new())]
    }

    /// Any `CMD_TIME_SET` that goes out, the connection's own included.
    pub fn note_set(&mut self, now: UnixMillis, utc: UnixTime, gmt_offset: i32, from_screen: bool) {
        self.last_sync = Some(Sync {
            sent_at: now,
            phone_utc: utc,
            gmt_offset,
            from_screen,
            shift_ms: self.shift_ms,
            answered_at: None,
            drift: None,
        });
    }

    /// The read's answer and errors are the clock's alone: during a sync's
    /// store drain an error would otherwise be read as the store's.
    pub fn claims(&self, frame: &Frame) -> bool {
        frame.command.opcode() == Command::CMD_TIME_GET.0
            || refusal(frame, Command::CMD_TIME_GET).is_some()
            || (self.set_state == SetState::Sending
                && refusal(frame, Command::CMD_TIME_SET).is_some())
    }

    pub fn on_frame(&mut self, frame: &Frame, now: UnixMillis) {
        if let Some(err) = refusal(frame, Command::CMD_TIME_GET) {
            self.read_state = ReadState::Refused(err);
            return;
        }
        if let Some(err) = refusal(frame, Command::CMD_TIME_SET) {
            self.set_state = SetState::Refused(err);
            return;
        }
        let reading = frame.objects.iter().find_map(|o| match o {
            WppObject::TimeSet(t) => Some(Reading {
                watch_utc: UnixTime(t.utc as i64),
                gmt_offset: t.gmt_offset,
                dst_change_at: UnixTime(t.dst_change_time as i64),
                next_gmt_offset: t.next_gmt_offset,
                received_at: now,
            }),
            _ => None,
        });
        match reading {
            Some(r) => {
                self.reading = Some(r);
                self.read_state = ReadState::Answered;
            }
            // An answer with nothing readable in it is no reading at all.
            None => self.read_state = ReadState::Unanswered,
        }
    }

    /// Watches the rest of the traffic for the answer to a set.
    pub fn observe(&mut self, frame: &Frame, now: UnixMillis) -> Vec<Frame> {
        if frame.command.opcode() != Command::CMD_TIME_SET.0 {
            return Vec::new();
        }
        let drift = frame.objects.iter().find_map(|o| match o {
            WppObject::TimeSetReply(r) => Some(r.drift),
            _ => None,
        });
        if let Some(sync) = self.last_sync.as_mut() {
            if sync.answered_at.is_none() {
                sync.answered_at = Some(now);
                sync.drift = drift;
            }
        }
        if self.set_state == SetState::Sending {
            self.set_state = SetState::Set;
            return self.ask(now);
        }
        Vec::new()
    }

    pub fn on_tick(&mut self, now: UnixMillis) {
        if self.read_state == ReadState::Asking
            && self.asked_at.is_some_and(|at| now.0 - at.0 > ANSWER_WAIT_MS)
        {
            self.read_state = ReadState::Unanswered;
        }
        if self.set_state == SetState::Sending
            && self
                .last_sync
                .is_some_and(|s| s.answered_at.is_none() && now.0 - s.sent_at.0 > ANSWER_WAIT_MS)
        {
            self.set_state = SetState::Unanswered;
        }
    }

    /// A read in flight is lost with the link; a set still wanted is kept and
    /// goes out on the next one.
    pub fn on_disconnected(&mut self) {
        if self.read_state == ReadState::Asking {
            self.read_state = ReadState::WaitingForWatch;
        }
        if self.set_state == SetState::Sending {
            self.set_state = SetState::Unanswered;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::{TimeSet, TimeSetReply};

    fn t(ms: i64) -> UnixMillis {
        UnixMillis(ms)
    }

    fn time_reply(utc: u32, gmt_offset: i32) -> Frame {
        Frame::new(
            Command::CMD_TIME_GET,
            vec![WppObject::TimeSet(TimeSet {
                utc,
                gmt_offset,
                dst_change_time: 0,
                next_gmt_offset: gmt_offset,
            })],
        )
    }

    fn set_reply(drift: i32) -> Frame {
        Frame::new(
            Command::CMD_TIME_SET,
            vec![WppObject::TimeSetReply(TimeSetReply { drift })],
        )
    }

    fn error(command: Command, err: i32) -> Frame {
        Frame::new(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror { cmd: command.0, err })],
        )
    }

    #[test]
    fn a_read_waits_for_the_link_then_asks_once() {
        let mut clock = WatchClock::new();
        clock.want_read();
        assert!(clock.pump(t(0), false, false).is_empty());
        assert_eq!(clock.read_state(), ReadState::WaitingForWatch);
        let frames = clock.pump(t(10), true, false);
        assert_eq!(frames, vec![Frame::new(Command::CMD_TIME_GET, Vec::new())]);
        assert_eq!(frames[0].to_bytes(), vec![0x01, 0x05, 0x0b, 0x00, 0x00]);
        assert!(clock.pump(t(20), true, false).is_empty());
        assert_eq!(clock.read_state(), ReadState::Asking);
    }

    #[test]
    fn the_answer_is_the_watchs_own_second_against_the_phones() {
        let mut clock = WatchClock::new();
        clock.want_read();
        clock.pump(t(0), true, false);
        let reply = time_reply(1_800_000_007, 3600);
        assert!(clock.claims(&reply));
        clock.on_frame(&reply, t(1_800_000_004_500));
        let reading = clock.reading().unwrap();
        assert_eq!(clock.read_state(), ReadState::Answered);
        assert_eq!(reading.watch_utc, UnixTime(1_800_000_007));
        assert_eq!(reading.gmt_offset, 3600);
        assert_eq!(reading.offset_ms(), 2_500);
    }

    #[test]
    fn an_unanswered_read_says_so_and_never_invents_a_time() {
        let mut clock = WatchClock::new();
        clock.want_read();
        clock.pump(t(0), true, false);
        clock.on_tick(t(ANSWER_WAIT_MS + 1));
        assert_eq!(clock.read_state(), ReadState::Unanswered);
        assert_eq!(clock.reading(), None);
    }

    #[test]
    fn a_refused_read_keeps_the_error_code() {
        let mut clock = WatchClock::new();
        clock.want_read();
        clock.pump(t(0), true, false);
        let refused = error(Command::CMD_TIME_GET, Cmderror::ERR_ARG_INVAL);
        assert!(clock.claims(&refused));
        clock.on_frame(&refused, t(5));
        assert_eq!(clock.read_state(), ReadState::Refused(Cmderror::ERR_ARG_INVAL));
        assert_eq!(clock.reading(), None);
    }

    #[test]
    fn a_set_waits_for_an_open_hand_session_to_close() {
        let mut clock = WatchClock::new();
        clock.want_set(0, None);
        assert!(clock.pump(t(0), true, true).is_empty());
        assert_eq!(clock.set_state(), SetState::WaitingForHands);
        assert!(clock.pump(t(1), false, false).is_empty());
        assert_eq!(clock.set_state(), SetState::WaitingForWatch);
        let frames = clock.pump(t(1_800_000_000_400), true, false);
        assert_eq!(frames, vec![time_set_frame(UnixTime(1_800_000_000), 0, None)]);
        assert_eq!(clock.set_state(), SetState::Sending);
    }

    #[test]
    fn a_set_carries_the_phones_second_offset_and_next_change() {
        let mut clock = WatchClock::new();
        let change = DstChange { at: UnixTime(1_803_000_000), gmt_offset: 3600 };
        clock.want_set(0, Some(change));
        let frames = clock.pump(t(1_800_000_000_999), true, false);
        assert_eq!(
            frames[0].objects,
            vec![WppObject::TimeSet(TimeSet {
                utc: 1_800_000_000,
                gmt_offset: 0,
                dst_change_time: 1_803_000_000,
                next_gmt_offset: 3600,
            })]
        );
        let sync = clock.last_sync().unwrap();
        assert!(sync.from_screen);
        assert_eq!(sync.phone_utc, UnixTime(1_800_000_000));
    }

    #[test]
    fn an_answered_set_is_read_back() {
        let mut clock = WatchClock::new();
        clock.want_set(0, None);
        clock.pump(t(1_000), true, false);
        let back = clock.observe(&set_reply(-3), t(1_200));
        assert_eq!(back, vec![Frame::new(Command::CMD_TIME_GET, Vec::new())]);
        assert_eq!(clock.set_state(), SetState::Set);
        let sync = clock.last_sync().unwrap();
        assert_eq!(sync.answered_at, Some(t(1_200)));
        assert_eq!(sync.drift, Some(-3));
    }

    #[test]
    fn the_connections_own_set_is_the_last_sync_but_reads_nothing_back() {
        let mut clock = WatchClock::new();
        clock.note_set(t(5_000), UnixTime(5), 0, false);
        assert!(clock.observe(&set_reply(0), t(5_100)).is_empty());
        let sync = clock.last_sync().unwrap();
        assert!(!sync.from_screen);
        assert_eq!(sync.answered_at, Some(t(5_100)));
        assert_eq!(clock.set_state(), SetState::None);
    }

    #[test]
    fn a_set_the_watch_never_answers_times_out() {
        let mut clock = WatchClock::new();
        clock.want_set(0, None);
        clock.pump(t(0), true, false);
        clock.on_tick(t(ANSWER_WAIT_MS + 1));
        assert_eq!(clock.set_state(), SetState::Unanswered);
    }

    #[test]
    fn errors_about_other_commands_are_not_the_clocks() {
        let clock = WatchClock::new();
        assert!(!clock.claims(&error(Command::CMD_HANDS_MOVE, -8)));
        assert!(!clock.claims(&error(Command::CMD_TIME_SET, -8)));
    }

    #[test]
    fn a_custom_time_is_the_phone_clock_plus_a_kept_shift() {
        let mut clock = WatchClock::new();
        clock.want_set_shifted(3600, None, -90_000);
        let frames = clock.pump(t(1_800_000_000_000), true, false);
        assert_eq!(frames, vec![time_set_frame(UnixTime(1_799_999_910), 3600, None)]);
        assert_eq!(clock.last_sync().unwrap().shift_ms, -90_000);
        assert_eq!(clock.shift_ms(), -90_000, "kept for the reconnect's own set");
        clock.want_set(3600, None);
        assert_eq!(clock.shift_ms(), 0, "a plain set goes back to phone time");
    }
}
