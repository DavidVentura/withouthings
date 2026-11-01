use crate::client::{probe_frame, refuses, rejects_probe, Credentials, Mac, Secret};
use crate::objects::{AccountKey, AdvKey, Cmderror};
use crate::{Command, Frame, WppObject};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PairingError {
    AccountIdZero,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PairingState {
    Idle,
    Probing,
    Associating { mac: Mac },
    FinishingSetup { mac: Mac, keys: AccountKeys },
    Readopting(Credentials),
    Paired(Credentials),
    AlreadyAssociated,
    /// The watch named itself in a form no credential could be kept under.
    UnreadableIdentity,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccountKeys {
    Set,
    Unsupported,
}

#[derive(Debug)]
pub struct Pairing {
    secret: Secret,
    account_id: u32,
    known: Vec<Credentials>,
    state: PairingState,
}

impl Pairing {
    pub fn new(
        secret: Secret,
        account_id: u32,
        known: Vec<Credentials>,
    ) -> Result<Pairing, PairingError> {
        if account_id == 0 {
            return Err(PairingError::AccountIdZero);
        }
        Ok(Pairing {
            secret,
            account_id,
            known,
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

    pub fn on_disconnected(&mut self) {
        if !matches!(self.state, PairingState::Paired(_)) {
            self.state = PairingState::Idle;
        }
    }

    pub fn on_frame(&mut self, frame: &Frame) -> Vec<Frame> {
        let opcode = frame.command.opcode();
        if opcode == Command::CMD_ERROR && rejects_probe(frame) {
            self.state = PairingState::AlreadyAssociated;
            return Vec::new();
        }

        match &self.state {
            PairingState::Probing if opcode == Command::CMD_PROBE_CHALLENGE => {
                let Some(challenge) = frame.objects.iter().find_map(|o| match o {
                    WppObject::ProbeChallenge(c) => Some(c.clone()),
                    _ => None,
                }) else {
                    return Vec::new();
                };
                let identity = challenge.mac.to_ascii_lowercase();
                let Some(credentials) = self.known.iter().find(|c| c.mac().as_str() == identity).cloned()
                else {
                    self.state = PairingState::AlreadyAssociated;
                    return Vec::new();
                };
                let reply = credentials.challenge_reply(&challenge);
                self.state = PairingState::Readopting(credentials);
                vec![reply]
            }
            PairingState::Readopting(credentials) if opcode == Command::CMD_PROBE => {
                self.state = PairingState::Paired(credentials.clone());
                Vec::new()
            }
            PairingState::Probing if opcode == Command::CMD_PROBE => {
                let Some(reply) = frame.objects.iter().find_map(|o| match o {
                    WppObject::ProbeReply(r) => Some(r.clone()),
                    _ => None,
                }) else {
                    return Vec::new();
                };
                let Ok(mac) = Mac::parse(&reply.mac) else {
                    self.state = PairingState::UnreadableIdentity;
                    return Vec::new();
                };
                self.state = PairingState::Associating { mac };
                vec![Frame::new(
                    Command::CMD_ASSOCIATION_KEYS_SET,
                    vec![
                        WppObject::AccountKey(AccountKey {
                            id: self.account_id,
                            secret: self.secret.as_str().to_string(),
                        }),
                        WppObject::AdvKey(AdvKey {
                            secret: self.secret.as_str().to_string(),
                        }),
                    ],
                )]
            }
            PairingState::Associating { mac } if opcode == Command::CMD_ASSOCIATION_KEYS_SET => {
                self.state = PairingState::FinishingSetup {
                    mac: mac.clone(),
                    keys: AccountKeys::Set,
                };
                vec![Frame::new(Command::CMD_SETUP_OK, Vec::new())]
            }
            // Older firmware (original ScanWatch) has no account-key association:
            // it answers CMD_ASSOCIATION_KEYS_SET with CMDUNKN and authenticates
            // on a bare probe reply, as Gadgetbridge relies on. Skip the keys and
            // finish setup.
            PairingState::Associating { mac }
                if refuses(
                    frame,
                    Command::CMD_ASSOCIATION_KEYS_SET,
                    Cmderror::ERR_CMDUNKN,
                ) =>
            {
                self.state = PairingState::FinishingSetup {
                    mac: mac.clone(),
                    keys: AccountKeys::Unsupported,
                };
                vec![Frame::new(Command::CMD_SETUP_OK, Vec::new())]
            }
            PairingState::FinishingSetup { mac, .. } if opcode == Command::CMD_SETUP_OK => {
                self.state = PairingState::Paired(Credentials::new(mac.clone(), self.secret.clone()));
                Vec::new()
            }
            // Firmware that predates key association may predate SETUP_OK too.
            // A watch that took the keys and then refuses setup has not paired.
            PairingState::FinishingSetup {
                mac,
                keys: AccountKeys::Unsupported,
            } if refuses(frame, Command::CMD_SETUP_OK, Cmderror::ERR_CMDUNKN) => {
                self.state = PairingState::Paired(Credentials::new(mac.clone(), self.secret.clone()));
                Vec::new()
            }
            _ => Vec::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::ProbeChallengeResponse;
    use crate::objects::{ProbeChallenge, ProbeReply};

    const SECRET: &str = "gUf8Np69A4GvJxjY1XOcIHKQm2HcPZnO";
    const MAC: &str = "a4:7e:fa:44:d6:10";

    fn mac(text: &str) -> Mac {
        Mac::parse(text).unwrap()
    }

    fn secret(text: &str) -> Secret {
        Secret::parse(text.to_string()).unwrap()
    }

    fn pairing() -> Pairing {
        Pairing::new(secret(SECRET), 19071510, Vec::new()).unwrap()
    }

    fn knowing_the_watch() -> Pairing {
        Pairing::new(
            secret(&"a different key, of the very same length"[..crate::client::SECRET_LEN]),
            19071510,
            vec![Credentials::new(mac(&MAC.to_ascii_uppercase()), secret(SECRET))],
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
    fn a_reply_naming_no_mac_ends_in_an_unreadable_identity() {
        let mut pairing = pairing();
        pairing.on_connected();
        let mut reply = probe_reply();
        let Some(WppObject::ProbeReply(r)) = reply.objects.first_mut() else {
            panic!("a probe reply")
        };
        r.mac = "not a mac".to_string();
        assert!(pairing.on_frame(&reply).is_empty());
        assert_eq!(pairing.state(), &PairingState::UnreadableIdentity);
    }

    fn refusal(cmd: Command, err: i32) -> Frame {
        Frame::new(
            Command::CMD_ERROR,
            vec![WppObject::Cmderror(Cmderror { cmd: cmd.0, err })],
        )
    }

    fn paired() -> PairingState {
        PairingState::Paired(Credentials::new(mac(MAC), secret(SECRET)))
    }

    #[test]
    fn a_watch_without_key_association_is_set_up_without_keys() {
        let mut pairing = pairing();
        pairing.on_connected();
        pairing.on_frame(&probe_reply());
        let next = pairing.on_frame(&refusal(
            Command::CMD_ASSOCIATION_KEYS_SET,
            Cmderror::ERR_CMDUNKN,
        ));
        assert_eq!(next.len(), 1);
        assert_eq!(next[0].command, Command::CMD_SETUP_OK);
        pairing.on_frame(&Frame::new(Command::CMD_SETUP_OK, Vec::new()));
        assert_eq!(pairing.state(), &paired());
    }

    #[test]
    fn a_refused_setup_ok_after_skipped_keys_still_pairs() {
        let mut pairing = pairing();
        pairing.on_connected();
        pairing.on_frame(&probe_reply());
        pairing.on_frame(&refusal(
            Command::CMD_ASSOCIATION_KEYS_SET,
            Cmderror::ERR_CMDUNKN,
        ));
        let next = pairing.on_frame(&refusal(Command::CMD_SETUP_OK, Cmderror::ERR_CMDUNKN));
        assert!(next.is_empty());
        assert_eq!(pairing.state(), &paired());
    }

    #[test]
    fn a_refused_setup_ok_after_the_keys_were_taken_does_not_pair() {
        let mut pairing = pairing();
        pairing.on_connected();
        pairing.on_frame(&probe_reply());
        pairing.on_frame(&Frame::new(Command::CMD_ASSOCIATION_KEYS_SET, Vec::new()));
        let next = pairing.on_frame(&refusal(Command::CMD_SETUP_OK, Cmderror::ERR_CMDUNKN));
        assert!(next.is_empty());
        assert_eq!(
            pairing.state(),
            &PairingState::FinishingSetup {
                mac: mac(MAC),
                keys: AccountKeys::Set,
            }
        );
    }

    #[test]
    fn other_refusals_do_not_skip_the_keys() {
        let mut pairing = pairing();
        pairing.on_connected();
        pairing.on_frame(&probe_reply());
        let next = pairing.on_frame(&refusal(
            Command::CMD_ASSOCIATION_KEYS_SET,
            Cmderror::ERR_FAIL,
        ));
        assert!(next.is_empty());
        assert_eq!(
            pairing.state(),
            &PairingState::Associating {
                mac: mac(MAC),
            }
        );
    }

    #[test]
    fn a_free_watch_is_sent_the_keys_and_identifies_itself() {
        let mut pairing = pairing();
        let opening = pairing.on_connected();
        assert_eq!(opening[0].command, Command::CMD_PROBE);

        let sent = pairing.on_frame(&probe_reply());
        assert_eq!(sent[0].command, Command::CMD_ASSOCIATION_KEYS_SET);
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

        let sent = pairing.on_frame(&Frame::new(Command::CMD_ASSOCIATION_KEYS_SET, Vec::new()));
        assert_eq!(sent[0].command, Command::CMD_SETUP_OK);
        assert!(!matches!(pairing.state(), PairingState::Paired(_)));

        pairing.on_frame(&Frame::new(Command::CMD_SETUP_OK, Vec::new()));
        assert_eq!(
            pairing.state(),
            &PairingState::Paired(Credentials::new(mac(MAC), secret(SECRET)))
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
            &PairingState::Paired(Credentials::new(mac(MAC), secret(SECRET)))
        );
    }

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
