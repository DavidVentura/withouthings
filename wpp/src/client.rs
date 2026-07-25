//! The sync conversation, as a pure state machine.
//!
//! [`Client::handle`] takes an [`Event`] and returns [`Action`]s. It performs
//! no I/O and holds no clock, so the same code drives a live BLE link and a
//! replayed capture, and the tests below are the protocol spec.
//!
//! Deleting a stored measurement is irreversible on the watch, so
//! [`Action::Delete`] is only ever produced after the host reports the data
//! durable via [`Event::Stored`].

use crate::objects::{
    AppProbe, AppProbeOsVersion, Id, ProbeChallenge, ProbeChallengeResponse, StoredSignalMeta,
    VasistasType, WamVasistasGet,
};
use crate::signal::{Signal, SignalCollector};
use crate::units::{UnixMillis, UnixTime};
use crate::{Command, Frame, WppObject};

/// Shared secret established at association, and the watch's address.
///
/// The secret is the 32-character key sent as `AccountKey`/`AdvKey` by
/// `CMD_ASSOCIATION_KEYS_SET`.
#[derive(Debug, Clone)]
pub struct Credentials {
    pub mac: String,
    pub secret: String,
}

impl Credentials {
    /// `SHA1(challenge || mac_ascii || secret_ascii)`, as the app computes it.
    pub fn answer(&self, challenge: &[u8]) -> Vec<u8> {
        let mut input = Vec::with_capacity(challenge.len() + self.mac.len() + self.secret.len());
        input.extend_from_slice(challenge);
        input.extend_from_slice(self.mac.as_bytes());
        input.extend_from_slice(self.secret.as_bytes());
        sha1(&input).to_vec()
    }
}

/// Which historical series to walk. The watch keeps them separately and each
/// needs its own watermark.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Category(pub u8);

#[derive(Debug, Clone, PartialEq)]
pub enum Event {
    /// The link is up and notifications are subscribed.
    Connected,
    /// A decoded frame and when the host received it. Live pushes carry no
    /// timestamp of their own, so this is the only time they get.
    Frame {
        frame: Frame,
        received_at: UnixMillis,
    },
    /// Everything handed over by [`Action::Store`] up to `token` is durable.
    Stored { token: u64 },
}

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Send(Frame),
    /// Persist these records, then report back with [`Event::Stored`].
    Store {
        token: u64,
        records: Vec<Record>,
    },
    /// Safe to remove from the watch: the data behind it is committed.
    Delete(Frame),
    /// The sync finished; the link can be dropped.
    Finished,
}

/// What a sync produced, in wire units.
#[derive(Debug, Clone, PartialEq)]
pub enum Record {
    Sample {
        measured_at: UnixMillis,
        kind: SampleKind,
        value: i64,
        quality: Option<i64>,
        source: Source,
    },
    WorkoutStarted {
        started_at: UnixTime,
        subcategory: i16,
    },
    WorkoutEnded {
        started_at: UnixTime,
        ended_at: UnixTime,
        paused_secs: i64,
    },
    Ecg(Box<Signal>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleKind {
    HeartRate,
    CoreTemperature,
    HrvSdnn,
    HrvRmssd,
    RespiratoryRate,
    BatteryPercent,
    Steps,
}

impl SampleKind {
    pub fn id(self) -> i64 {
        match self {
            SampleKind::HeartRate => 1,
            SampleKind::CoreTemperature => 2,
            SampleKind::HrvSdnn => 3,
            SampleKind::HrvRmssd => 4,
            SampleKind::RespiratoryRate => 5,
            SampleKind::BatteryPercent => 6,
            SampleKind::Steps => 7,
        }
    }
}

/// Which ingest path a sample arrived by; the two differ in resolution and
/// must not overwrite each other.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// Historical series pulled during a sync.
    Stored,
    /// 1 Hz notifications during a live workout or measurement.
    Live,
}

impl Source {
    pub fn id(self) -> i64 {
        match self {
            Source::Stored => 0,
            Source::Live => 1,
        }
    }
}

/// Where the conversation has got to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Idle,
    /// Probe sent, waiting for the watch's challenge.
    Probing,
    /// Our answer sent, waiting for the watch to accept it.
    Authenticating,
    /// Walking one category's history forward.
    Syncing,
    Finished,
}

pub struct Client {
    credentials: Credentials,
    phase: Phase,
    /// Categories still to walk, and where each has been read up to.
    queue: Vec<(Category, UnixTime)>,
    current: Option<(Category, UnixTime)>,
    /// Timestamp of the newest record seen in the batch being collected.
    batch_high_water: Option<UnixTime>,
    signals: SignalCollector,
    next_token: u64,
    /// Deletes held back until the matching Store is confirmed durable.
    pending_deletes: Vec<(u64, Frame)>,
    app_probe: AppProbe,
}

impl Client {
    pub fn new(credentials: Credentials, watermarks: Vec<(Category, UnixTime)>) -> Client {
        Client {
            credentials,
            phase: Phase::Idle,
            queue: watermarks,
            current: None,
            batch_high_water: None,
            signals: SignalCollector::new(),
            next_token: 1,
            pending_deletes: Vec::new(),
            app_probe: AppProbe {
                os: 1,
                app: 1,
                version: 8070101,
            },
        }
    }

    /// Where each category has been read up to. Persist this only after the
    /// corresponding records are durable, so a crash re-reads rather than skips.
    pub fn watermarks(&self) -> Vec<(Category, UnixTime)> {
        let mut all = self.queue.clone();
        if let Some(current) = self.current {
            all.push(current);
        }
        all.sort_by_key(|(c, _)| *c);
        all
    }

    pub fn phase(&self) -> Phase {
        self.phase
    }

    /// The category being walked, and the point it has been read up to.
    pub fn current(&self) -> Option<(Category, UnixTime)> {
        self.current
    }

    /// Non-zero means data has been read from the watch but not yet committed.
    pub fn pending_deletes(&self) -> usize {
        self.pending_deletes.len()
    }

    pub fn handle(&mut self, event: Event) -> Vec<Action> {
        match event {
            Event::Connected => self.on_connected(),
            Event::Frame { frame, received_at } => self.on_frame(frame, received_at),
            Event::Stored { token } => self.on_stored(token),
        }
    }

    fn on_connected(&mut self) -> Vec<Action> {
        self.phase = Phase::Probing;
        vec![Action::Send(Frame::new(
            Command::CMD_PROBE,
            vec![
                WppObject::AppProbe(self.app_probe.clone()),
                WppObject::AppProbeOsVersion(AppProbeOsVersion { os_version: 35 }),
            ],
        ))]
    }

    fn on_stored(&mut self, token: u64) -> Vec<Action> {
        let mut actions: Vec<Action> = Vec::new();
        self.pending_deletes.retain(|(pending, frame)| {
            if *pending <= token {
                actions.push(Action::Delete(frame.clone()));
                false
            } else {
                true
            }
        });
        actions
    }

    fn store(&mut self, records: Vec<Record>) -> Option<Action> {
        if records.is_empty() {
            return None;
        }
        let token = self.next_token;
        self.next_token += 1;
        Some(Action::Store { token, records })
    }

    fn on_frame(&mut self, frame: Frame, received_at: UnixMillis) -> Vec<Action> {
        let mut actions = Vec::new();
        let mut records = Vec::new();

        // Decoding does not depend on who asked for the data; the phase below
        // only decides what to send next. This also lets a captured session be
        // replayed through the same code.
        self.collect_passive(&frame, received_at, &mut records);
        self.collect_history(&frame, &mut records);

        match (self.phase, frame.command.opcode()) {
            (Phase::Probing, c) if c == Command::CMD_PROBE_CHALLENGE.0 => {
                if let Some(challenge) = frame.objects.iter().find_map(|o| match o {
                    WppObject::ProbeChallenge(c) => Some(c.clone()),
                    _ => None,
                }) {
                    self.phase = Phase::Authenticating;
                    actions.push(Action::Send(Frame::new(
                        Command::CMD_PROBE_CHALLENGE,
                        vec![
                            WppObject::ProbeChallengeResponse(ProbeChallengeResponse {
                                answer: self.credentials.answer(&challenge.challenge),
                            }),
                            // Our own challenge; the watch answers it in kind.
                            WppObject::ProbeChallenge(ProbeChallenge {
                                mac: self.credentials.mac.clone(),
                                challenge: vec![0; 16],
                            }),
                        ],
                    )));
                }
            }
            (Phase::Authenticating, c) if c == Command::CMD_PROBE.0 => {
                self.phase = Phase::Syncing;
                actions.extend(self.request_next());
            }
            (Phase::Syncing, _) => {
                let empty = frame
                    .objects
                    .iter()
                    .any(|o| matches!(o, WppObject::Null(_)));
                if empty {
                    // Nothing left in this category; move on.
                    self.current = None;
                    actions.extend(self.request_next());
                } else if let Some(high) = self.batch_high_water.take() {
                    if let Some((category, _)) = self.current {
                        // Resume one second past the newest record so the next
                        // request does not return it again.
                        self.current = Some((category, UnixTime(high.0 + 1)));
                    }
                    actions.extend(self.request_current());
                }
            }
            _ => {}
        }

        for signal in self.take_signals() {
            let delete = delete_frame(&signal);
            records.push(Record::Ecg(Box::new(signal)));
            if let Some(action) = self.store(std::mem::take(&mut records)) {
                if let Action::Store { token, .. } = action {
                    self.pending_deletes.push((token, delete));
                }
                actions.push(action);
            }
        }
        if let Some(action) = self.store(records) {
            actions.push(action);
        }
        if self.phase == Phase::Finished {
            actions.push(Action::Finished);
        }
        actions
    }

    fn take_signals(&mut self) -> Vec<Signal> {
        self.signals
            .take_completed()
            .into_iter()
            .filter(|s| s.is_complete())
            .collect()
    }

    fn collect_passive(
        &mut self,
        frame: &Frame,
        received_at: UnixMillis,
        records: &mut Vec<Record>,
    ) {
        for object in &frame.objects {
            self.signals.observe(object);
            match object {
                WppObject::LiveHr(live) if live.hr > 0 => {
                    records.push(Record::Sample {
                        measured_at: received_at,
                        kind: SampleKind::HeartRate,
                        value: live.hr as i64,
                        quality: None,
                        source: Source::Live,
                    });
                }
                WppObject::StartTime(start)
                    if frame.command.opcode() == Command::CMD_WORKOUT_START.0 =>
                {
                    let subcategory = frame
                        .objects
                        .iter()
                        .find_map(|o| match o {
                            WppObject::ActivitySubcategory(a) => Some(a.value),
                            _ => None,
                        })
                        .unwrap_or(0);
                    records.push(Record::WorkoutStarted {
                        started_at: UnixTime(start.value as i64),
                        subcategory,
                    });
                }
                WppObject::EndTime(end) => {
                    let started = frame.objects.iter().find_map(|o| match o {
                        WppObject::StartTime(s) => Some(s.value),
                        _ => None,
                    });
                    let paused = frame
                        .objects
                        .iter()
                        .find_map(|o| match o {
                            WppObject::PauseState(p) => Some(p.sum as i64),
                            _ => None,
                        })
                        .unwrap_or(0);
                    if let Some(started) = started {
                        records.push(Record::WorkoutEnded {
                            started_at: UnixTime(started as i64),
                            ended_at: UnixTime(end.value as i64),
                            paused_secs: paused,
                        });
                    }
                }
                _ => {}
            }
        }
    }

    /// Historical samples are grouped: a `WamVasistasHead` timestamp followed
    /// by the values measured in that window.
    fn collect_history(&mut self, frame: &Frame, records: &mut Vec<Record>) {
        let mut at: Option<UnixTime> = None;
        for object in &frame.objects {
            match object {
                WppObject::WamVasistasHead(head) => {
                    let time = UnixTime(head.utc as i64);
                    at = Some(time);
                    self.batch_high_water = Some(
                        self.batch_high_water
                            .map_or(time, |h| UnixTime(h.0.max(time.0))),
                    );
                }
                WppObject::VasistasHeartrate(hr) if hr.heartrate > 0 => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::HeartRate,
                            value: hr.heartrate as i64,
                            quality: Some(hr.quality as i64),
                            source: Source::Stored,
                        });
                    }
                }
                WppObject::VasistasCbt(cbt) => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::CoreTemperature,
                            value: cbt.temperature as i64,
                            quality: None,
                            source: Source::Stored,
                        });
                    }
                }
                WppObject::VasistasHrv(hrv) => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::HrvSdnn,
                            value: hrv.sdnn as i64,
                            quality: Some(hrv.quality as i64),
                            source: Source::Stored,
                        });
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::HrvRmssd,
                            value: hrv.rmssd as i64,
                            quality: Some(hrv.quality as i64),
                            source: Source::Stored,
                        });
                    }
                }
                WppObject::VasistasRr(rr) if rr.rr > 0 => {
                    if let Some(time) = at {
                        records.push(Record::Sample {
                            measured_at: time.to_millis(),
                            kind: SampleKind::RespiratoryRate,
                            value: rr.rr as i64,
                            quality: None,
                            source: Source::Stored,
                        });
                    }
                }
                _ => {}
            }
        }
    }

    fn request_next(&mut self) -> Vec<Action> {
        match self.queue.pop() {
            Some(next) => {
                self.current = Some(next);
                self.request_current()
            }
            None => {
                self.phase = Phase::Finished;
                vec![Action::Send(Frame::new(Command::CMD_SYNC_OK, Vec::new()))]
            }
        }
    }

    fn request_current(&mut self) -> Vec<Action> {
        let Some((category, from)) = self.current else {
            return Vec::new();
        };
        vec![Action::Send(Frame::new(
            Command::CMD_VASISTAS_GET,
            vec![
                WppObject::WamVasistasGet(WamVasistasGet {
                    utc_start: from.0 as u32,
                    max: 0,
                }),
                WppObject::VasistasType(VasistasType {
                    value: category.0 as i32,
                }),
            ],
        ))]
    }
}

fn delete_frame(signal: &Signal) -> Frame {
    Frame::new(
        Command::CMD_STORED_MEASURE_SIGNAL_DEL,
        vec![
            WppObject::Id(Id { value: 1 }),
            WppObject::StoredSignalMeta(StoredSignalMeta {
                ..signal.meta.clone()
            }),
        ],
    )
}

/// SHA-1, so the crate keeps no dependencies. Only ever fed a challenge.
fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let bit_len = (data.len() as u64) * 8;
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bit_len.to_be_bytes());

    for block in message.chunks_exact(64) {
        let mut w = [0u32; 80];
        for (i, word) in block.chunks_exact(4).enumerate() {
            w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, word) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*word);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    let mut out = [0u8; 20];
    for (i, word) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::Null;

    fn credentials() -> Credentials {
        Credentials {
            mac: "a4:7e:fa:44:d6:10".to_string(),
            secret: "gUf8Np69A4GvJxjY1XOcIHKQm2HcPZnO".to_string(),
        }
    }

    #[test]
    fn sha1_matches_known_vectors() {
        assert_eq!(
            sha1(b"abc").to_vec(),
            hex(b"a9993e364706816aba3e25717850c26c9cd0d89d")
        );
        assert_eq!(
            sha1(b"").to_vec(),
            hex(b"da39a3ee5e6b4b0d3255bfef95601890afd80709")
        );
    }

    fn hex(s: &[u8]) -> Vec<u8> {
        s.chunks(2)
            .map(|p| u8::from_str_radix(std::str::from_utf8(p).unwrap(), 16).unwrap())
            .collect()
    }

    /// The exchange captured from the real watch.
    #[test]
    fn the_challenge_answer_matches_the_capture() {
        let challenge = [
            244, 197, 79, 127, 24, 111, 82, 130, 216, 87, 5, 54, 35, 63, 193, 35,
        ];
        assert_eq!(
            credentials().answer(&challenge),
            vec![
                84, 20, 165, 52, 232, 6, 253, 184, 77, 32, 105, 86, 199, 96, 220, 232, 42, 76, 25,
                32
            ]
        );
    }

    #[test]
    fn connecting_probes_then_answers_the_challenge() {
        let mut client = Client::new(credentials(), vec![(Category(8), UnixTime(1000))]);
        let actions = client.handle(Event::Connected);
        assert!(matches!(actions[0], Action::Send(ref f) if f.command == Command::CMD_PROBE));

        let challenge = Frame::new(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![1; 16],
            })],
        );
        let actions = client.handle(Event::Frame {
            frame: challenge,
            received_at: UnixMillis(0),
        });
        let Action::Send(reply) = &actions[0] else {
            panic!("expected a reply, got {actions:?}")
        };
        assert_eq!(reply.command, Command::CMD_PROBE_CHALLENGE);
        assert!(reply
            .objects
            .iter()
            .any(|o| matches!(o, WppObject::ProbeChallengeResponse(_))));
    }

    fn authenticated() -> Client {
        let mut client = Client::new(credentials(), vec![(Category(8), UnixTime(1000))]);
        client.handle(Event::Connected);
        client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(
                Command::CMD_PROBE_CHALLENGE,
                vec![WppObject::ProbeChallenge(ProbeChallenge {
                    mac: "a4:7e:fa:44:d6:10".to_string(),
                    challenge: vec![1; 16],
                })],
            ),
        });
        client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(Command::CMD_PROBE, Vec::new()),
        });
        client
    }

    #[test]
    fn a_null_reply_ends_the_category_and_finishes_the_sync() {
        let mut client = authenticated();
        let actions = client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]),
        });
        assert!(actions
            .iter()
            .any(|a| matches!(a, Action::Send(f) if f.command == Command::CMD_SYNC_OK)));
    }

    /// The watermark must only move past data the host has committed.
    #[test]
    fn the_watermark_advances_past_the_newest_record_seen() {
        use crate::objects::{VasistasHeartrate, WamVasistasHead};
        let mut client = authenticated();
        client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(
                Command::CMD_VASISTAS_GET,
                vec![
                    WppObject::WamVasistasHead(WamVasistasHead { utc: 5000 }),
                    WppObject::VasistasHeartrate(VasistasHeartrate {
                        heartrate: 62,
                        quality: 4,
                        temperature: 0,
                    }),
                ],
            ),
        });
        assert_eq!(client.watermarks(), vec![(Category(8), UnixTime(5001))]);
    }

    #[test]
    fn stored_samples_carry_their_window_timestamp() {
        use crate::objects::{VasistasHeartrate, WamVasistasHead};
        let mut client = authenticated();
        let actions = client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(
                Command::CMD_VASISTAS_GET,
                vec![
                    WppObject::WamVasistasHead(WamVasistasHead { utc: 5000 }),
                    WppObject::VasistasHeartrate(VasistasHeartrate {
                        heartrate: 62,
                        quality: 4,
                        temperature: 0,
                    }),
                ],
            ),
        });
        let stored = actions.iter().find_map(|a| match a {
            Action::Store { records, .. } => Some(records),
            _ => None,
        });
        assert_eq!(
            stored.unwrap()[0],
            Record::Sample {
                measured_at: UnixMillis(5_000_000),
                kind: SampleKind::HeartRate,
                value: 62,
                quality: Some(4),
                source: Source::Stored,
            }
        );
    }

    /// Nothing may be deleted from the watch before it is durable here.
    #[test]
    fn a_delete_waits_for_the_store_to_be_confirmed() {
        use crate::objects::{StoredSignalData, StoredSignalMetaExtend};
        let mut client = authenticated();
        let meta = StoredSignalMeta {
            r#type: 7,
            sampling_freq: 300,
            format: 0,
            size: 2,
            resolution: 14,
            channel: 1,
        };
        let actions = client.handle(Event::Frame {
            received_at: UnixMillis(0),
            frame: Frame::new(
                Command::CMD_STORED_MEASURE_SIGNAL_GET,
                vec![
                    WppObject::StoredSignalMeta(meta),
                    WppObject::StoredSignalMetaExtend(StoredSignalMetaExtend {
                        duration: 1,
                        total_size: 4,
                        filter_bank: 0,
                    }),
                    WppObject::StoredSignalData(StoredSignalData {
                        samples: vec![1, 0, 2, 0],
                    }),
                ],
            ),
        });
        let token = actions
            .iter()
            .find_map(|a| match a {
                Action::Store { token, .. } => Some(*token),
                _ => None,
            })
            .expect("the ecg should be handed over for storage");
        assert!(
            !actions.iter().any(|a| matches!(a, Action::Delete(_))),
            "must not delete before the store is confirmed"
        );

        let actions = client.handle(Event::Stored { token });
        assert!(matches!(
            actions.as_slice(),
            [Action::Delete(f)] if f.command == Command::CMD_STORED_MEASURE_SIGNAL_DEL
        ));
    }

    fn frame(command: Command, objects: Vec<WppObject>) -> Event {
        Event::Frame {
            frame: Frame::new(command, objects),
            received_at: UnixMillis(0),
        }
    }

    fn sent(actions: &[Action]) -> Vec<Command> {
        actions
            .iter()
            .filter_map(|a| match a {
                Action::Send(f) => Some(f.command),
                _ => None,
            })
            .collect()
    }

    fn stored(actions: &[Action]) -> Vec<Record> {
        actions
            .iter()
            .filter_map(|a| match a {
                Action::Store { records, .. } => Some(records.clone()),
                _ => None,
            })
            .flatten()
            .collect()
    }

    /// A whole session driven message by message, asserting what the client
    /// sends at each step and the state it ends in.
    #[test]
    fn a_scripted_session_reaches_the_expected_end_state() {
        use crate::objects::{Null, VasistasCbt, VasistasHeartrate, WamVasistasHead};

        let mut client = Client::new(credentials(), vec![(Category(8), UnixTime(4000))]);
        assert_eq!(client.phase(), Phase::Idle);

        // 1. link up -> probe
        let actions = client.handle(Event::Connected);
        assert_eq!(sent(&actions), vec![Command::CMD_PROBE]);
        assert_eq!(client.phase(), Phase::Probing);

        // 2. the watch challenges us -> we answer it
        let actions = client.handle(frame(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: "a4:7e:fa:44:d6:10".to_string(),
                challenge: vec![
                    244, 197, 79, 127, 24, 111, 82, 130, 216, 87, 5, 54, 35, 63, 193, 35,
                ],
            })],
        ));
        assert_eq!(sent(&actions), vec![Command::CMD_PROBE_CHALLENGE]);
        assert_eq!(client.phase(), Phase::Authenticating);
        let Action::Send(reply) = &actions[0] else {
            panic!()
        };
        assert!(reply.objects.contains(&WppObject::ProbeChallengeResponse(
            ProbeChallengeResponse {
                answer: vec![
                    84, 20, 165, 52, 232, 6, 253, 184, 77, 32, 105, 86, 199, 96, 220, 232, 42, 76,
                    25, 32
                ],
            }
        )));

        // 3. the watch accepts -> we start asking for history from the watermark
        let actions = client.handle(frame(Command::CMD_PROBE, vec![]));
        assert_eq!(sent(&actions), vec![Command::CMD_VASISTAS_GET]);
        assert_eq!(client.phase(), Phase::Syncing);
        assert_eq!(client.current(), Some((Category(8), UnixTime(4000))));
        let Action::Send(request) = &actions[0] else {
            panic!()
        };
        assert!(request.objects.contains(&WppObject::WamVasistasGet(
            WamVasistasGet {
                utc_start: 4000,
                max: 0,
            }
        )));

        // 4. a window of samples -> stored, and the next request moves past it
        let actions = client.handle(frame(
            Command::CMD_VASISTAS_GET,
            vec![
                WppObject::WamVasistasHead(WamVasistasHead { utc: 5000 }),
                WppObject::VasistasHeartrate(VasistasHeartrate {
                    heartrate: 62,
                    quality: 4,
                    temperature: 0,
                }),
                WppObject::VasistasCbt(VasistasCbt {
                    algo: 0,
                    attrib: 1,
                    temperature: 37255,
                }),
            ],
        ));
        assert_eq!(sent(&actions), vec![Command::CMD_VASISTAS_GET]);
        assert_eq!(
            stored(&actions),
            vec![
                Record::Sample {
                    measured_at: UnixMillis(5_000_000),
                    kind: SampleKind::HeartRate,
                    value: 62,
                    quality: Some(4),
                    source: Source::Stored,
                },
                Record::Sample {
                    measured_at: UnixMillis(5_000_000),
                    kind: SampleKind::CoreTemperature,
                    value: 37255,
                    quality: None,
                    source: Source::Stored,
                },
            ]
        );
        assert_eq!(client.current(), Some((Category(8), UnixTime(5001))));

        // 5. nothing left -> the sync closes out
        let actions = client.handle(frame(Command::CMD_VASISTAS_GET, vec![WppObject::Null(Null {})]));
        assert_eq!(sent(&actions), vec![Command::CMD_SYNC_OK]);

        assert_eq!(client.phase(), Phase::Finished);
        assert_eq!(client.current(), None);
        assert_eq!(client.pending_deletes(), 0);
        assert_eq!(client.watermarks(), vec![]);
    }
}
