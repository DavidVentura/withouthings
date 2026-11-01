use std::sync::Mutex;

use wpp::capture::{FrameReassembler, StreamItem};
use wpp::client::Secret;

use crate::service::credentials_of;
use wpp::pairing::{Pairing, PairingState};
use wpp::Frame;

use crate::records::{Transport, WatchError};

#[derive(uniffi::Record, Debug, Clone, PartialEq, Eq)]
pub struct KnownWatch {
    pub mac: String,
    pub secret: String,
}

#[derive(uniffi::Enum, Debug, Clone, PartialEq, Eq)]
pub enum PairingProgress {
    Idle,
    Probing,
    Associating,
    Readopting,
    Paired { mac: String, secret: String },
    AlreadyAssociated,
    UnreadableIdentity,
}

impl PairingProgress {
    fn of(state: &PairingState) -> PairingProgress {
        match state {
            PairingState::Idle => PairingProgress::Idle,
            PairingState::Probing => PairingProgress::Probing,
            PairingState::Associating { .. } | PairingState::FinishingSetup { .. } => {
                PairingProgress::Associating
            }
            PairingState::Readopting(_) => PairingProgress::Readopting,
            PairingState::Paired(credentials) => PairingProgress::Paired {
                mac: credentials.mac().as_str().to_string(),
                secret: credentials.secret().as_str().to_string(),
            },
            PairingState::AlreadyAssociated => PairingProgress::AlreadyAssociated,
            PairingState::UnreadableIdentity => PairingProgress::UnreadableIdentity,
        }
    }
}

#[derive(uniffi::Object)]
pub struct PairingService {
    inner: Mutex<PairingInner>,
    transport: Box<dyn Transport>,
}

struct PairingInner {
    pairing: Pairing,
    reassembler: FrameReassembler,
}

#[uniffi::export]
impl PairingService {
    #[uniffi::constructor]
    pub fn new(
        secret: String,
        account_id: u32,
        known: Vec<KnownWatch>,
        transport: Box<dyn Transport>,
    ) -> Result<Self, WatchError> {
        let known = known
            .into_iter()
            .map(|w| credentials_of(&w.mac, w.secret))
            .collect::<Result<Vec<_>, _>>()?;
        let secret = Secret::parse(secret).map_err(|err| WatchError::Protocol {
            reason: err.to_string(),
        })?;
        let pairing =
            Pairing::new(secret, account_id, known).map_err(|err| WatchError::Protocol {
                reason: format!("{err:?}"),
            })?;
        Ok(PairingService {
            inner: Mutex::new(PairingInner {
                pairing,
                reassembler: FrameReassembler::new(),
            }),
            transport,
        })
    }

    pub fn on_connected(&self) {
        self.step(|inner| {
            inner.reassembler.reset();
            inner.pairing.on_connected()
        });
    }

    pub fn on_bytes(&self, bytes: Vec<u8>) {
        self.step(|inner| {
            let items = inner.reassembler.push(&bytes);
            items
                .into_iter()
                .filter_map(|item| match item {
                    StreamItem::Frame { frame, .. } => Some(frame),
                    StreamItem::Desync { .. } => None,
                })
                .flat_map(|frame| inner.pairing.on_frame(&frame))
                .collect()
        });
    }

    pub fn on_disconnected(&self) {
        self.step(|inner| {
            inner.reassembler.reset();
            inner.pairing.on_disconnected();
            Vec::new()
        });
    }

    pub fn progress(&self) -> PairingProgress {
        PairingProgress::of(self.inner.lock().unwrap().pairing.state())
    }
}

impl PairingService {
    fn step(&self, act: impl FnOnce(&mut PairingInner) -> Vec<Frame>) {
        let (frames, moved) = {
            let mut inner = self.inner.lock().unwrap();
            let before = inner.pairing.state().clone();
            let frames = act(&mut inner);
            let moved = *inner.pairing.state() != before;
            (frames, moved)
        };
        for message in frames {
            let wire = message.to_wire().expect("pairing objects fit a frame");
            self.transport.write(wire.iter().map(Frame::to_bytes).collect());
        }
        if moved {
            self.transport.changed();
        }
    }
}
