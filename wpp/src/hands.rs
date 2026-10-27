//! Setting the watch hands back onto the dial ("Set the time" in the Withings
//! app).
//!
//! The hands are stepper-driven and the firmware only knows where it last put
//! them, so a hand knocked off its step (the charger's magnet, a missed pulse
//! on a low cell) keeps showing a fixed error until the phone tells the watch
//! where 12 o'clock really is. The session, as Gadgetbridge drives it on the
//! Steel HR and ScanWatch (`WithingsSteelHRDeviceSupport`,
//! `WithingsCalibrationActivity`, `MoveHand`):
//!
//! 1. `CMD_HANDS_CAL_START` (286), no objects. The watch drives every hand to
//!    where it believes 12 o'clock (and 0 on the activity dial) is.
//! 2. `CMD_HANDS_MOVE` (284) carrying one `TYPE_HANDS_MOVE` (1292):
//!    `hand: u8` (0 minute, 1 hour, 2 activity/tracker), `nbSteps: i16` big
//!    endian; positive turns clockwise. As many as it takes, one hand at a
//!    time.
//! 3. `CMD_HANDS_CAL_STOP` (287), no objects. Wherever the hands now are is
//!    taken as 12 o'clock and they run back to the time.
//!
//! `CMD_HANDS_CAL_CANCEL` (2422) is in the APK's command table but no open
//! client sends it, so a cancel that the watch refuses is undone by hand: every
//! step this session moved is moved back, which leaves the hands where the
//! firmware already believed 12 was, and the session is then stopped, which
//! saves nothing new.
//!
//! The motor has 360 steps a revolution (`renode-sim/models/NrfHands.cs`), so
//! one step is a degree: ten seconds on the minute hand, two minutes on the
//! hour hand.
//!
//! Moves go out one at a time and the next waits for the watch to answer the
//! last, with taps that arrive meanwhile folded into one move. Gadgetbridge
//! fires them without waiting and testers report a calibration that "took 2
//! or 3 tries to save"; a stop that overtakes a move is the likely reason.

use std::collections::VecDeque;

use crate::objects::HandsMove;
use crate::units::UnixMillis;
use crate::{Command, Frame, WppObject};

/// Steps in one turn of any hand.
pub const STEPS_PER_REVOLUTION: i32 = 360;

/// How long an unanswered command is waited on before the session carries on
/// as though it had been answered. The firmware's replies are not documented,
/// so a silent watch must not leave the screen stuck.
pub const REPLY_WAIT_MS: i64 = 2_500;

/// How long a session asked for before the link is up waits for it before
/// giving up, so the screen can never sit on "starting" for ever.
pub const LINK_WAIT_MS: i64 = 10_000;

/// The largest move one frame carries; the field is an i16 but a turn and a
/// half is already more than anyone needs from one press.
const MAX_STEPS_PER_MOVE: i32 = 540;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Hand {
    Minute,
    Hour,
    /// The activity (motion) hand on the small dial.
    Activity,
}

impl Hand {
    pub fn wire(self) -> u8 {
        match self {
            Hand::Minute => 0,
            Hand::Hour => 1,
            Hand::Activity => 2,
        }
    }

    fn index(self) -> usize {
        self.wire() as usize
    }

    const ALL: [Hand; 3] = [Hand::Minute, Hand::Hour, Hand::Activity];
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CalState {
    /// No session.
    Idle,
    /// Asked for while the watch was not yet authenticated (just
    /// reconnected, say); the start goes out as soon as it is.
    WaitingForWatch,
    /// The watch was not linked within [`LINK_WAIT_MS`]; nothing was sent.
    TimedOut,
    /// Start sent, not yet answered.
    Starting,
    /// Hands parked at the watch's idea of 12; moves are accepted.
    Adjusting,
    /// Stop sent after the last move; waiting for the watch.
    Saving,
    /// The watch answered the stop: the new 12 o'clock is kept.
    Saved,
    /// Cancel sent (or the move-back-and-stop fallback is running).
    Cancelling,
    /// Nothing from this session was kept.
    Cancelled,
    /// The link dropped mid-session.
    Interrupted,
    /// The watch refused the start or the stop.
    Failed { command: u16, err: i32 },
}

impl CalState {
    pub fn is_open(self) -> bool {
        matches!(
            self,
            CalState::Starting | CalState::Adjusting | CalState::Saving | CalState::Cancelling
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Finish {
    Save,
    Cancel,
    /// The cancel the watch refused, finished as a stop once the moves back
    /// are in.
    Revert,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Waiting {
    Start,
    Move { hand: Hand, steps: i16 },
    Stop,
    Cancel,
}

impl Waiting {
    fn command(self) -> Command {
        match self {
            Waiting::Start => Command::CMD_HANDS_CAL_START,
            Waiting::Move { .. } => Command::CMD_HANDS_MOVE,
            Waiting::Stop => Command::CMD_HANDS_CAL_STOP,
            Waiting::Cancel => Command::CMD_HANDS_CAL_CANCEL,
        }
    }
}

#[derive(Debug, Clone)]
pub struct HandsCalibration {
    state: CalState,
    waiting: Option<(Waiting, UnixMillis)>,
    queue: VecDeque<(Hand, i32)>,
    /// Steps sent (or queued) per hand this session, so a cancel can undo
    /// them.
    moved: [i32; 3],
    finish: Option<Finish>,
    /// When a session waiting for the link was asked for.
    asked_at: Option<UnixMillis>,
}

impl Default for HandsCalibration {
    fn default() -> Self {
        Self::new()
    }
}

pub fn start_frame() -> Frame {
    Frame::new(Command::CMD_HANDS_CAL_START, Vec::new())
}

pub fn move_frame(hand: Hand, steps: i16) -> Frame {
    Frame::new(
        Command::CMD_HANDS_MOVE,
        vec![WppObject::HandsMove(HandsMove {
            hand: hand.wire(),
            nb_steps: steps,
        })],
    )
}

pub fn stop_frame() -> Frame {
    Frame::new(Command::CMD_HANDS_CAL_STOP, Vec::new())
}

pub fn cancel_frame() -> Frame {
    Frame::new(Command::CMD_HANDS_CAL_CANCEL, Vec::new())
}

fn is_hands_command(code: u16) -> bool {
    code == Command::CMD_HANDS_CAL_START.0
        || code == Command::CMD_HANDS_MOVE.0
        || code == Command::CMD_HANDS_CAL_STOP.0
        || code == Command::CMD_HANDS_CAL_CANCEL.0
}

impl HandsCalibration {
    pub fn new() -> Self {
        HandsCalibration {
            state: CalState::Idle,
            waiting: None,
            queue: VecDeque::new(),
            moved: [0; 3],
            finish: None,
            asked_at: None,
        }
    }

    pub fn state(&self) -> CalState {
        self.state
    }

    /// Steps moved per hand this session (minute, hour, activity).
    pub fn moved(&self) -> [i32; 3] {
        self.moved
    }

    /// Moves still to go out, the one in flight included.
    pub fn pending_steps(&self) -> i32 {
        let queued: i32 = self.queue.iter().map(|(_, s)| s.abs()).sum();
        let flying = match self.waiting {
            Some((Waiting::Move { steps, .. }, _)) => (steps as i32).abs(),
            _ => 0,
        };
        queued + flying
    }

    pub fn start(&mut self, now: UnixMillis) -> Vec<Frame> {
        *self = HandsCalibration::new();
        self.state = CalState::Starting;
        self.send(Waiting::Start, now)
    }

    /// A session asked for while the link cannot carry it yet. Nothing is
    /// sent; [`HandsCalibration::on_linked`] sends the start once it can, and
    /// [`HandsCalibration::on_tick`] gives up after [`LINK_WAIT_MS`].
    pub fn start_when_linked(&mut self, now: UnixMillis) {
        *self = HandsCalibration::new();
        self.state = CalState::WaitingForWatch;
        self.asked_at = Some(now);
    }

    /// The link is authenticated: a session waiting for it starts now.
    pub fn on_linked(&mut self, now: UnixMillis) -> Vec<Frame> {
        if self.state != CalState::WaitingForWatch {
            return Vec::new();
        }
        self.start(now)
    }

    /// Queues `steps` on `hand`, clockwise when positive. Ignored outside a
    /// session or once it is being closed.
    pub fn nudge(&mut self, hand: Hand, steps: i32, now: UnixMillis) -> Vec<Frame> {
        if steps == 0 || !matches!(self.state, CalState::Starting | CalState::Adjusting) {
            return Vec::new();
        }
        if self.finish.is_some() {
            return Vec::new();
        }
        self.moved[hand.index()] += steps;
        self.enqueue(hand, steps);
        self.pump(now)
    }

    fn enqueue(&mut self, hand: Hand, steps: i32) {
        match self.queue.back_mut() {
            Some((last, total)) if *last == hand => *total += steps,
            _ => self.queue.push_back((hand, steps)),
        }
        if matches!(self.queue.back(), Some((_, 0))) {
            self.queue.pop_back();
        }
    }

    /// Keeps where the hands are now as 12 o'clock, once every move is in.
    pub fn save(&mut self, now: UnixMillis) -> Vec<Frame> {
        if !matches!(self.state, CalState::Starting | CalState::Adjusting) {
            return Vec::new();
        }
        self.finish = Some(Finish::Save);
        self.pump(now)
    }

    /// Drops everything this session did. Moves not yet sent are forgotten
    /// rather than sent.
    pub fn cancel(&mut self, now: UnixMillis) -> Vec<Frame> {
        if self.state == CalState::WaitingForWatch {
            // Nothing went out yet, so there is nothing to take back.
            self.state = CalState::Cancelled;
            self.asked_at = None;
            return Vec::new();
        }
        if !matches!(self.state, CalState::Starting | CalState::Adjusting | CalState::Saving) {
            return Vec::new();
        }
        if self.state == CalState::Saving && self.waiting.is_some() {
            // The stop is already on its way; too late to take it back.
            return Vec::new();
        }
        for (hand, steps) in self.queue.drain(..) {
            self.moved[hand.index()] -= steps;
        }
        self.finish = Some(Finish::Cancel);
        self.pump(now)
    }

    pub fn on_disconnected(&mut self) {
        if self.state.is_open() {
            self.state = CalState::Interrupted;
        }
        self.waiting = None;
        self.queue.clear();
        self.finish = None;
    }

    /// Whether `frame` belongs to this session. Claimed frames are not looked
    /// at by anything else, so an error about a hand command cannot be taken
    /// for the answer to a sync request.
    pub fn claims(&self, frame: &Frame) -> bool {
        let Some((waiting, _)) = self.waiting else {
            return false;
        };
        let opcode = frame.command.opcode();
        if opcode == waiting.command().0 {
            return true;
        }
        opcode == Command::CMD_ERROR.0
            && frame.objects.iter().any(
                |o| matches!(o, WppObject::Cmderror(e) if is_hands_command(e.cmd)),
            )
    }

    pub fn on_frame(&mut self, frame: &Frame, now: UnixMillis) -> Vec<Frame> {
        let Some((waiting, _)) = self.waiting else {
            return Vec::new();
        };
        let opcode = frame.command.opcode();
        let refused = frame.objects.iter().find_map(|o| match o {
            WppObject::Cmderror(e) if opcode == Command::CMD_ERROR.0 && is_hands_command(e.cmd) => {
                Some((e.cmd, e.err))
            }
            _ => None,
        });
        if refused.is_none() && opcode != waiting.command().0 {
            return Vec::new();
        }
        self.waiting = None;
        match (waiting, refused) {
            (Waiting::Start, Some((command, err))) => {
                self.state = CalState::Failed { command, err };
                self.queue.clear();
                self.finish = None;
                Vec::new()
            }
            (Waiting::Start, None) => {
                self.state = CalState::Adjusting;
                self.pump(now)
            }
            (Waiting::Move { hand, steps }, Some(_)) => {
                // Refused: the hand did not move, so there is nothing to undo.
                self.moved[hand.index()] -= steps as i32;
                self.pump(now)
            }
            (Waiting::Move { .. }, None) => self.pump(now),
            (Waiting::Stop, Some((command, err))) => {
                self.state = CalState::Failed { command, err };
                Vec::new()
            }
            (Waiting::Stop, None) => {
                self.state = if self.finish == Some(Finish::Revert) {
                    CalState::Cancelled
                } else {
                    CalState::Saved
                };
                self.finish = None;
                Vec::new()
            }
            (Waiting::Cancel, Some(_)) => {
                // No cancel on this firmware: walk every hand back and stop.
                for hand in Hand::ALL {
                    let back = -self.moved[hand.index()];
                    if back != 0 {
                        self.enqueue(hand, back);
                    }
                }
                self.finish = Some(Finish::Revert);
                self.pump(now)
            }
            (Waiting::Cancel, None) => {
                self.state = CalState::Cancelled;
                self.finish = None;
                Vec::new()
            }
        }
    }

    /// Carries on past a command the watch never answered.
    pub fn on_tick(&mut self, now: UnixMillis) -> Vec<Frame> {
        if self.state == CalState::WaitingForWatch {
            if let Some(asked) = self.asked_at {
                if now.0 - asked.0 >= LINK_WAIT_MS {
                    self.state = CalState::TimedOut;
                    self.asked_at = None;
                }
            }
            return Vec::new();
        }
        let Some((waiting, sent_at)) = self.waiting else {
            return Vec::new();
        };
        if now.0 - sent_at.0 < REPLY_WAIT_MS {
            return Vec::new();
        }
        // An answer is what moves the session on, and silence is treated as
        // one: the watch is known to act on these whether or not it replies.
        let ack = Frame::new(waiting.command(), Vec::new());
        self.on_frame(&ack, now)
    }

    fn send(&mut self, waiting: Waiting, now: UnixMillis) -> Vec<Frame> {
        self.waiting = Some((waiting, now));
        let frame = match waiting {
            Waiting::Start => start_frame(),
            Waiting::Move { hand, steps } => move_frame(hand, steps),
            Waiting::Stop => stop_frame(),
            Waiting::Cancel => cancel_frame(),
        };
        vec![frame]
    }

    fn pump(&mut self, now: UnixMillis) -> Vec<Frame> {
        if self.waiting.is_some() || self.state == CalState::Starting {
            return Vec::new();
        }
        if let Some((hand, total)) = self.queue.pop_front() {
            let chunk = total.clamp(-MAX_STEPS_PER_MOVE, MAX_STEPS_PER_MOVE);
            let rest = total - chunk;
            if rest != 0 {
                self.queue.push_front((hand, rest));
            }
            return self.send(
                Waiting::Move {
                    hand,
                    steps: chunk as i16,
                },
                now,
            );
        }
        match self.finish {
            Some(Finish::Save) | Some(Finish::Revert) => {
                if self.finish == Some(Finish::Save) {
                    self.state = CalState::Saving;
                } else {
                    self.state = CalState::Cancelling;
                }
                self.send(Waiting::Stop, now)
            }
            Some(Finish::Cancel) => {
                self.state = CalState::Cancelling;
                if self.moved == [0; 3] {
                    // Nothing moved: a plain stop keeps the old 12 and is a
                    // command every firmware knows.
                    self.finish = Some(Finish::Revert);
                    self.send(Waiting::Stop, now)
                } else {
                    self.send(Waiting::Cancel, now)
                }
            }
            None => Vec::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::Cmderror;

    fn t(ms: i64) -> UnixMillis {
        UnixMillis(ms)
    }

    fn reply(command: Command) -> Frame {
        Frame::new(command, Vec::new())
    }

    fn error(command: Command) -> Frame {
        Frame::new(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror {
                cmd: command.0,
                err: Cmderror::ERR_ARG_INVAL,
            })],
        )
    }

    fn moves(frames: &[Frame]) -> Vec<(u8, i16)> {
        frames
            .iter()
            .flat_map(|f| f.objects.iter())
            .filter_map(|o| match o {
                WppObject::HandsMove(m) => Some((m.hand, m.nb_steps)),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn move_frame_matches_gadgetbridges_move_hand_bytes() {
        // version, CMD_HANDS_MOVE 0x011c, payload 7: TYPE_HANDS_MOVE 0x050c,
        // length 3, hand 0 (minute), nbSteps +6 big endian.
        assert_eq!(
            move_frame(Hand::Minute, 6).to_bytes(),
            vec![0x01, 0x01, 0x1c, 0x00, 0x07, 0x05, 0x0c, 0x00, 0x03, 0x00, 0x00, 0x06]
        );
        // Hour hand, ten steps anticlockwise: -10 = 0xfff6.
        assert_eq!(
            move_frame(Hand::Hour, -10).to_bytes(),
            vec![0x01, 0x01, 0x1c, 0x00, 0x07, 0x05, 0x0c, 0x00, 0x03, 0x01, 0xff, 0xf6]
        );
        assert_eq!(Frame::parse(&move_frame(Hand::Activity, 1).to_bytes()).unwrap(), move_frame(Hand::Activity, 1));
    }

    #[test]
    fn start_stop_and_cancel_carry_no_objects() {
        assert_eq!(start_frame().to_bytes(), vec![0x01, 0x01, 0x1e, 0x00, 0x00]);
        assert_eq!(stop_frame().to_bytes(), vec![0x01, 0x01, 0x1f, 0x00, 0x00]);
        assert_eq!(cancel_frame().to_bytes(), vec![0x01, 0x09, 0x76, 0x00, 0x00]);
    }

    #[test]
    fn moves_wait_for_the_start_and_for_each_other() {
        let mut cal = HandsCalibration::new();
        assert_eq!(cal.start(t(0)), vec![start_frame()]);
        // Pressed before the watch answered the start: held.
        assert!(cal.nudge(Hand::Minute, 1, t(10)).is_empty());
        assert!(cal.nudge(Hand::Minute, 1, t(20)).is_empty());
        let sent = cal.on_frame(&reply(Command::CMD_HANDS_CAL_START), t(30));
        assert_eq!(moves(&sent), vec![(0, 2)], "the two taps go out as one move");
        assert_eq!(cal.state(), CalState::Adjusting);
        // In flight: the next taps fold together.
        assert!(cal.nudge(Hand::Minute, 10, t(40)).is_empty());
        assert!(cal.nudge(Hand::Minute, -1, t(50)).is_empty());
        assert!(cal.nudge(Hand::Hour, 3, t(60)).is_empty());
        assert_eq!(moves(&cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(70))), vec![(0, 9)]);
        assert_eq!(moves(&cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(80))), vec![(1, 3)]);
        assert!(cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(90)).is_empty());
        assert_eq!(cal.moved(), [11, 3, 0]);
    }

    #[test]
    fn save_goes_out_only_after_the_last_move() {
        let mut cal = HandsCalibration::new();
        cal.start(t(0));
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_START), t(1));
        assert_eq!(moves(&cal.nudge(Hand::Minute, -4, t(2))), vec![(0, -4)]);
        cal.nudge(Hand::Minute, -1, t(3));
        assert!(cal.save(t(4)).is_empty(), "stop must not overtake the moves");
        assert_eq!(moves(&cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(5))), vec![(0, -1)]);
        assert_eq!(cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(6)), vec![stop_frame()]);
        assert_eq!(cal.state(), CalState::Saving);
        // Taps after Done are ignored.
        assert!(cal.nudge(Hand::Hour, 1, t(7)).is_empty());
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_STOP), t(8));
        assert_eq!(cal.state(), CalState::Saved);
    }

    #[test]
    fn cancel_without_moves_is_a_plain_stop() {
        let mut cal = HandsCalibration::new();
        cal.start(t(0));
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_START), t(1));
        assert_eq!(cal.cancel(t(2)), vec![stop_frame()]);
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_STOP), t(3));
        assert_eq!(cal.state(), CalState::Cancelled);
    }

    #[test]
    fn cancel_drops_unsent_moves_and_sends_cancel() {
        let mut cal = HandsCalibration::new();
        cal.start(t(0));
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_START), t(1));
        cal.nudge(Hand::Minute, 5, t(2));
        cal.nudge(Hand::Hour, 7, t(3));
        // The hour move never went out; the cancel waits for the minute one.
        assert!(cal.cancel(t(4)).is_empty());
        assert_eq!(cal.moved(), [5, 0, 0]);
        assert_eq!(cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(5)), vec![cancel_frame()]);
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_CANCEL), t(6));
        assert_eq!(cal.state(), CalState::Cancelled);
    }

    #[test]
    fn a_refused_cancel_walks_the_hands_back_and_stops() {
        let mut cal = HandsCalibration::new();
        cal.start(t(0));
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_START), t(1));
        cal.nudge(Hand::Minute, 30, t(2));
        cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(3));
        cal.nudge(Hand::Hour, -2, t(4));
        cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(5));
        assert_eq!(cal.cancel(t(6)), vec![cancel_frame()]);
        let error = error(Command::CMD_HANDS_CAL_CANCEL);
        assert!(cal.claims(&error));
        assert_eq!(moves(&cal.on_frame(&error, t(7))), vec![(0, -30)]);
        assert_eq!(moves(&cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(8))), vec![(1, 2)]);
        assert_eq!(cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(9)), vec![stop_frame()]);
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_STOP), t(10));
        assert_eq!(cal.state(), CalState::Cancelled);
    }

    #[test]
    fn a_refused_start_fails_and_sends_nothing_more() {
        let mut cal = HandsCalibration::new();
        cal.start(t(0));
        cal.nudge(Hand::Minute, 1, t(1));
        assert!(cal.on_frame(&error(Command::CMD_HANDS_CAL_START), t(2)).is_empty());
        assert!(matches!(cal.state(), CalState::Failed { command: 286, .. }));
        assert!(cal.nudge(Hand::Minute, 1, t(3)).is_empty());
        assert!(cal.save(t(4)).is_empty());
    }

    #[test]
    fn silence_counts_as_an_answer_after_the_wait() {
        let mut cal = HandsCalibration::new();
        cal.start(t(0));
        cal.nudge(Hand::Hour, 1, t(1));
        assert!(cal.on_tick(t(REPLY_WAIT_MS - 1)).is_empty());
        assert_eq!(moves(&cal.on_tick(t(REPLY_WAIT_MS))), vec![(1, 1)]);
        assert_eq!(cal.state(), CalState::Adjusting);
    }

    #[test]
    fn a_dropped_link_interrupts_the_session() {
        let mut cal = HandsCalibration::new();
        cal.start(t(0));
        cal.nudge(Hand::Minute, 3, t(1));
        cal.on_disconnected();
        assert_eq!(cal.state(), CalState::Interrupted);
        assert!(cal.nudge(Hand::Minute, 1, t(2)).is_empty());
        assert!(!cal.claims(&reply(Command::CMD_HANDS_MOVE)));
    }

    #[test]
    fn only_frames_for_the_outstanding_command_are_claimed() {
        let mut cal = HandsCalibration::new();
        assert!(!cal.claims(&reply(Command::CMD_HANDS_CAL_START)));
        cal.start(t(0));
        assert!(cal.claims(&reply(Command::CMD_HANDS_CAL_START)));
        assert!(!cal.claims(&reply(Command::CMD_BATTERY_STATUS)));
        assert!(!cal.claims(&error(Command::CMD_WAM_VASISTAS_GET)));
        assert!(cal.claims(&error(Command::CMD_HANDS_CAL_START)));
    }

    #[test]
    fn a_session_asked_for_before_the_link_starts_when_it_comes_up() {
        let mut cal = HandsCalibration::new();
        cal.start_when_linked(t(0));
        assert_eq!(cal.state(), CalState::WaitingForWatch);
        assert!(cal.on_tick(t(5_000)).is_empty());
        cal.on_disconnected();
        assert_eq!(cal.state(), CalState::WaitingForWatch, "a reconnect does not lose it");
        let frames = cal.on_linked(t(6_000));
        assert_eq!(frames, vec![start_frame()]);
        assert_eq!(cal.state(), CalState::Starting);
        assert!(cal.on_linked(t(6_100)).is_empty(), "started once");
    }

    #[test]
    fn waiting_for_the_link_gives_up_after_ten_seconds() {
        let mut cal = HandsCalibration::new();
        cal.start_when_linked(t(0));
        assert!(cal.on_tick(t(LINK_WAIT_MS - 1)).is_empty());
        assert_eq!(cal.state(), CalState::WaitingForWatch);
        cal.on_tick(t(LINK_WAIT_MS));
        assert_eq!(cal.state(), CalState::TimedOut);
        assert!(cal.on_linked(t(LINK_WAIT_MS + 1)).is_empty(), "a timed-out session stays shut");
    }

    #[test]
    fn cancelling_while_waiting_for_the_link_sends_nothing() {
        let mut cal = HandsCalibration::new();
        cal.start_when_linked(t(0));
        assert!(cal.cancel(t(1)).is_empty());
        assert_eq!(cal.state(), CalState::Cancelled);
        assert!(cal.on_linked(t(2)).is_empty());
    }

    #[test]
    fn a_second_session_after_a_save_starts_from_scratch() {
        let mut cal = HandsCalibration::new();
        cal.start(t(0));
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_START), t(1));
        cal.nudge(Hand::Minute, 4, t(2));
        cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(3));
        cal.save(t(4));
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_STOP), t(5));
        assert_eq!(cal.state(), CalState::Saved);

        assert_eq!(cal.start(t(10)), vec![start_frame()]);
        assert_eq!(cal.state(), CalState::Starting);
        assert_eq!(cal.moved(), [0, 0, 0]);
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_START), t(11));
        assert_eq!(moves(&cal.nudge(Hand::Minute, -2, t(12))), vec![(0, -2)]);
    }

    #[test]
    fn a_huge_move_is_split() {
        let mut cal = HandsCalibration::new();
        cal.start(t(0));
        cal.on_frame(&reply(Command::CMD_HANDS_CAL_START), t(1));
        assert_eq!(moves(&cal.nudge(Hand::Minute, 1000, t(2))), vec![(0, 540)]);
        assert_eq!(moves(&cal.on_frame(&reply(Command::CMD_HANDS_MOVE), t(3))), vec![(0, 460)]);
    }
}
