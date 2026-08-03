//! Adopting a watch: taking one on, whether or not it already has a key.
//!
//! Everything else in this crate assumes a [`Credentials`] already exists.
//! This is where one comes from.
//!
//! The watch gates commands on whether it holds an association secret. With
//! one it answers `CMD_PROBE` with a challenge and refuses everything but the
//! handshake; without one it answers the probe outright and serves every
//! command unauthenticated, which is the window `CMD_ASSOCIATION_KEYS_SET`
//! goes through. So there is no way to take an associated watch over, and no
//! confirmation to collect from an unassociated one — whoever reaches it first
//! sets the key.
//!
//! The secret is chosen by the phone rather than the watch, so it arrives from
//! outside: randomness belongs to the shell, and [`Pairing::new`] only checks
//! that what it was handed is the shape the watch stores.
//!
//! A watch that challenges is not necessarily a stranger. `ProbeChallenge.mac`
//! is the identity the hash is built on, so a watch we have a key for names
//! itself in the very frame that asks us to prove we know it — and answering
//! is all it takes to take it back on. That path needs nothing from the watch
//! and erases nothing, which is what separates re-adopting a watch from
//! claiming a free one.

use crate::client::{probe_frame, rejects_probe, Credentials};
use crate::objects::{AccountKey, AdvKey, ProbeChallenge, ProbeChallengeResponse};
use crate::{Command, Frame, WppObject};

/// The association key is a fixed-width field on the watch: 32 characters and
/// a terminator, in the `KL_SECRET` element of its settings database.
pub const SECRET_LEN: usize = 32;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PairingError {
    /// The secret is not the length the watch stores.
    SecretLength { given: usize },
    /// The secret has a byte the watch cannot hold in an ASCII field.
    SecretNotAscii,
    /// `AccountKey.id` of zero is how the watch is told there is no account to
    /// record, and it then keeps only the key.
    AccountIdZero,
}

/// How far the association has got.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PairingState {
    /// Nothing sent; no link yet.
    Idle,
    /// Probe sent, waiting to hear whether the watch is free.
    Probing,
    /// Keys sent, waiting for the watch to confirm it stored them. The
    /// identity the watch gave in its probe reply is kept here rather than
    /// beside the state, so there is no way to be waiting without it.
    Associating { mac: String },
    /// Keys stored; telling the watch setup is finished, which is what turns
    /// authentication back on. See [`Pairing::on_frame`].
    FinishingSetup { mac: String },
    /// A watch we already hold a key for challenged us, and the answer is
    /// away. Nothing has been changed on the watch and nothing will be.
    Readopting(Credentials),
    /// The watch holds our key. These are what every later session needs.
    Paired(Credentials),
    /// The watch challenged with an identity we have no key for, or refused
    /// the key we offered. Either way it belongs to something else, and the
    /// only way past it is a factory reset — which is the watch's owner's
    /// decision to make and not something a stranger can ask for.
    AlreadyAssociated,
}

/// The association conversation, as a pure state machine.
///
/// [`Pairing::on_frame`] takes what arrived and returns what to send, the same
/// shape as [`crate::Client`] and for the same reason: the association is
/// short but it is the one exchange that cannot be retried against a watch
/// that has moved on, so it is worth being able to test without a radio.
#[derive(Debug)]
pub struct Pairing {
    secret: String,
    account_id: u32,
    /// Watches already known, by the identity they challenge under.
    known: Vec<Credentials>,
    state: PairingState,
}

impl Pairing {
    /// `known` is every watch a key is already held for. Identities are
    /// lower-cased on the way in: the watch writes its address one way and a
    /// key typed in by hand may be another, and the difference must not decide
    /// whether a watch is recognised.
    pub fn new(
        secret: String,
        account_id: u32,
        known: Vec<Credentials>,
    ) -> Result<Pairing, PairingError> {
        if secret.len() != SECRET_LEN {
            return Err(PairingError::SecretLength {
                given: secret.len(),
            });
        }
        if !secret.is_ascii() {
            return Err(PairingError::SecretNotAscii);
        }
        if account_id == 0 {
            return Err(PairingError::AccountIdZero);
        }
        Ok(Pairing {
            secret,
            account_id,
            known: known
                .into_iter()
                .map(|c| Credentials {
                    mac: c.mac.to_ascii_lowercase(),
                    secret: c.secret,
                })
                .collect(),
            state: PairingState::Idle,
        })
    }

    pub fn state(&self) -> &PairingState {
        &self.state
    }

    pub fn on_connected(&mut self) -> Vec<Frame> {
        self.state = PairingState::Probing;
        vec![probe_frame()]
    }

    /// A link that died mid-association leaves nothing behind on either side:
    /// the watch stores the key or it does not, and a half-sent frame is not a
    /// key. Starting over from the probe is the whole recovery.
    pub fn on_disconnected(&mut self) {
        if !matches!(self.state, PairingState::Paired(_)) {
            self.state = PairingState::Idle;
        }
    }

    pub fn on_frame(&mut self, frame: &Frame) -> Vec<Frame> {
        let opcode = frame.command.opcode();
        // A refused probe ends it wherever we are: either the watch wants a
        // key we do not have, or the one we offered was stale.
        if opcode == Command::CMD_ERROR.0 && rejects_probe(frame) {
            self.state = PairingState::AlreadyAssociated;
            return Vec::new();
        }

        match &self.state {
            PairingState::Probing if opcode == Command::CMD_PROBE_CHALLENGE.0 => {
                let Some(challenge) = frame.objects.iter().find_map(|o| match o {
                    WppObject::ProbeChallenge(c) => Some(c.clone()),
                    _ => None,
                }) else {
                    return Vec::new();
                };
                let identity = challenge.mac.to_ascii_lowercase();
                let Some(credentials) = self.known.iter().find(|c| c.mac == identity).cloned()
                else {
                    self.state = PairingState::AlreadyAssociated;
                    return Vec::new();
                };
                let answer = credentials.answer(&challenge.challenge);
                self.state = PairingState::Readopting(credentials);
                vec![Frame::new(
                    Command::CMD_PROBE_CHALLENGE,
                    vec![
                        WppObject::ProbeChallengeResponse(ProbeChallengeResponse { answer }),
                        // Our own challenge, which the watch answers in kind.
                        WppObject::ProbeChallenge(ProbeChallenge {
                            mac: identity,
                            challenge: vec![0; 16],
                        }),
                    ],
                )]
            }
            // The watch accepted the answer. Nothing was written to it and
            // nothing needs to be: it has held this key all along.
            PairingState::Readopting(credentials) if opcode == Command::CMD_PROBE.0 => {
                self.state = PairingState::Paired(credentials.clone());
                Vec::new()
            }
            // Answering the probe with a reply rather than a challenge is what
            // an unassociated watch does, and the reply carries the identity
            // the authentication hash needs — which is not the address it
            // advertised under, so this is the only place it can be learnt.
            PairingState::Probing if opcode == Command::CMD_PROBE.0 => {
                let Some(reply) = frame.objects.iter().find_map(|o| match o {
                    WppObject::ProbeReply(r) => Some(r.clone()),
                    _ => None,
                }) else {
                    return Vec::new();
                };
                self.state = PairingState::Associating { mac: reply.mac };
                vec![Frame::new(
                    Command::CMD_ASSOCIATION_KEYS_SET,
                    vec![
                        WppObject::AccountKey(AccountKey {
                            id: self.account_id,
                            secret: self.secret.clone(),
                        }),
                        // The one the watch actually stores as its association
                        // secret; the account key above only records who set
                        // it. Sent as one message because that is how the
                        // official app sends it and the handler reads both.
                        WppObject::AdvKey(AdvKey {
                            secret: self.secret.clone(),
                        }),
                    ],
                )]
            }
            // A watch is unauthenticated for one of two reasons — it holds no
            // secret, or it is in factory mode — and a factory reset puts it
            // in the second. Storing a key does not take it back out: the
            // watch goes on serving anything that connects to it until it is
            // told setup is finished, which is what `CMD_SETUP_OK` does.
            // Sent after the keys and not before, or authentication comes back
            // on around a secret the watch does not have yet.
            PairingState::Associating { mac } if opcode == Command::CMD_ASSOCIATION_KEYS_SET.0 => {
                self.state = PairingState::FinishingSetup { mac: mac.clone() };
                vec![Frame::new(Command::CMD_SETUP_OK, Vec::new())]
            }
            PairingState::FinishingSetup { mac } if opcode == Command::CMD_SETUP_OK.0 => {
                self.state = PairingState::Paired(Credentials {
                    mac: mac.clone(),
                    secret: self.secret.clone(),
                });
                Vec::new()
            }
            _ => Vec::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::{Cmderror, ProbeChallenge, ProbeReply};

    const SECRET: &str = "gUf8Np69A4GvJxjY1XOcIHKQm2HcPZnO";
    const MAC: &str = "a4:7e:fa:44:d6:10";

    fn pairing() -> Pairing {
        Pairing::new(SECRET.to_string(), 19071510, Vec::new()).unwrap()
    }

    /// The same, but already holding this watch's key.
    fn knowing_the_watch() -> Pairing {
        Pairing::new(
            "a different key, of the very same length".to_string()[..SECRET_LEN].to_string(),
            19071510,
            vec![Credentials {
                // Upper case on purpose: a key typed in by hand is not written
                // the way the watch writes its own address.
                mac: MAC.to_ascii_uppercase(),
                secret: SECRET.to_string(),
            }],
        )
        .unwrap()
    }

    fn challenge() -> Frame {
        Frame::new(
            Command::CMD_PROBE_CHALLENGE,
            vec![WppObject::ProbeChallenge(ProbeChallenge {
                mac: MAC.to_string(),
                challenge: vec![
                    244, 197, 79, 127, 24, 111, 82, 130, 216, 87, 5, 54, 35, 63, 193, 35,
                ],
            })],
        )
    }

    fn probe_reply() -> Frame {
        Frame::new(
            Command::CMD_PROBE,
            vec![WppObject::ProbeReply(ProbeReply {
                vid: 0,
                pid: 0,
                name: "ScanWatch 2".to_string(),
                mac: MAC.to_string(),
                secret: String::new(),
                hard_version: 16777215,
                mfg_id: "00280074".to_string(),
                bl_version: 8,
                soft_version: 3411,
                rescue_version: 16777215,
            })],
        )
    }

    #[test]
    fn a_secret_of_the_wrong_length_is_refused() {
        assert_eq!(
            Pairing::new("short".to_string(), 1, Vec::new()).err(),
            Some(PairingError::SecretLength { given: 5 })
        );
    }

    #[test]
    fn a_free_watch_is_sent_the_keys_and_identifies_itself() {
        let mut pairing = pairing();
        let opening = pairing.on_connected();
        assert_eq!(opening[0].command, Command::CMD_PROBE);

        let sent = pairing.on_frame(&probe_reply());
        assert_eq!(sent[0].command, Command::CMD_ASSOCIATION_KEYS_SET);
        // The account key records who set it; the advertised key is what the
        // watch stores and challenges against.
        assert_eq!(
            sent[0].objects,
            vec![
                WppObject::AccountKey(AccountKey {
                    id: 19071510,
                    secret: SECRET.to_string(),
                }),
                WppObject::AdvKey(AdvKey {
                    secret: SECRET.to_string(),
                }),
            ]
        );

        // Storing the key is not the end of it: until setup is declared
        // finished the watch stays unauthenticated and serves anyone.
        let sent = pairing.on_frame(&Frame::new(Command::CMD_ASSOCIATION_KEYS_SET, Vec::new()));
        assert_eq!(sent[0].command, Command::CMD_SETUP_OK);
        assert!(!matches!(pairing.state(), PairingState::Paired(_)));

        pairing.on_frame(&Frame::new(Command::CMD_SETUP_OK, Vec::new()));
        assert_eq!(
            pairing.state(),
            &PairingState::Paired(Credentials {
                // Not the address it advertised under; the probe reply is the
                // only place this is ever stated.
                mac: MAC.to_string(),
                secret: SECRET.to_string(),
            })
        );
    }

    #[test]
    fn a_watch_that_challenges_with_an_unknown_identity_is_someone_elses() {
        let mut pairing = pairing();
        pairing.on_connected();
        let sent = pairing.on_frame(&challenge());
        assert!(sent.is_empty());
        assert_eq!(pairing.state(), &PairingState::AlreadyAssociated);
    }

    /// A watch we hold a key for names itself in the frame that challenges us,
    /// so taking it back on costs one round trip and changes nothing on it.
    #[test]
    fn a_watch_we_already_have_a_key_for_is_answered_and_taken_back() {
        let mut pairing = knowing_the_watch();
        pairing.on_connected();
        let sent = pairing.on_frame(&challenge());
        assert_eq!(sent[0].command, Command::CMD_PROBE_CHALLENGE);
        assert!(sent[0].objects.contains(&WppObject::ProbeChallengeResponse(
            ProbeChallengeResponse {
                answer: vec![
                    84, 20, 165, 52, 232, 6, 253, 184, 77, 32, 105, 86, 199, 96, 220, 232, 42, 76,
                    25, 32
                ],
            }
        )));

        pairing.on_frame(&Frame::new(Command::CMD_PROBE, Vec::new()));
        assert_eq!(
            pairing.state(),
            &PairingState::Paired(Credentials {
                mac: MAC.to_string(),
                // The key it already holds, not the fresh one this pairing was
                // given to hand out.
                secret: SECRET.to_string(),
            })
        );
    }

    /// A key that has gone stale — the watch was reset and re-paired to
    /// something else — looks like a refused probe, not like silence.
    #[test]
    fn a_key_the_watch_no_longer_holds_ends_the_attempt() {
        let mut pairing = knowing_the_watch();
        pairing.on_connected();
        pairing.on_frame(&challenge());
        pairing.on_frame(&Frame::new(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror {
                cmd: Command::CMD_PROBE_CHALLENGE.0,
                err: -5,
            })],
        ));
        assert_eq!(pairing.state(), &PairingState::AlreadyAssociated);
    }

    /// Firmware that refuses the probe outright rather than challenging says
    /// the same thing, and saying nothing here would leave the screen waiting
    /// on a watch that has already answered.
    #[test]
    fn a_refused_probe_is_the_same_answer() {
        let mut pairing = pairing();
        pairing.on_connected();
        pairing.on_frame(&Frame::new(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror {
                cmd: Command::CMD_PROBE.0,
                err: -5,
            })],
        ));
        assert_eq!(pairing.state(), &PairingState::AlreadyAssociated);
    }

    /// The watch asks for a sync every couple of seconds while it has anything
    /// pending, association or not. Answering it is the paired client's job.
    #[test]
    fn chatter_before_the_reply_is_ignored() {
        let mut pairing = pairing();
        pairing.on_connected();
        let sent = pairing.on_frame(&Frame::new(
            Command::CMD_SYNC_REQUEST.with_channel(crate::Channel::SlaveRequest),
            Vec::new(),
        ));
        assert!(sent.is_empty());
        assert_eq!(pairing.state(), &PairingState::Probing);
    }

    #[test]
    fn a_link_lost_before_the_keys_land_starts_over() {
        let mut pairing = pairing();
        pairing.on_connected();
        pairing.on_frame(&probe_reply());
        pairing.on_disconnected();
        assert_eq!(pairing.state(), &PairingState::Idle);
        assert_eq!(pairing.on_connected()[0].command, Command::CMD_PROBE);
    }
}
