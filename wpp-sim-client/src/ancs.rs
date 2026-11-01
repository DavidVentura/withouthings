//! The phone's side of ANCS, over the simulator's ANCS channel.
//!
//! On that service the watch is the GATT client, so there is nothing here that
//! looks like the WPP request/reply above: the phone announces a notification
//! on the notification source, the watch asks for the attributes it wants by
//! writing the control point, and the phone answers in fragments on the data
//! source. The channel carries those three messages and the pipe plays the
//! GATT in between, so what crosses the socket is exactly the characteristic
//! values a real link would carry.
//!
//! Frames are [len u16 big-endian][kind][payload]: 0x01 an announcement, 0x02
//! a control-point write from the watch, 0x03 a data source fragment, 0x04 the
//! watch has subscribed to both sources.

use std::time::{Duration, Instant};

use wpp::ancs::{self, Category, ControlPoint, NotificationCenter, NotificationId};

use crate::pipe::Pipe;

const ANNOUNCE: u8 = 0x01;
const REQUEST: u8 = 0x02;
const ATTRIBUTES: u8 = 0x03;
const SUBSCRIBED: u8 = 0x04;

/// One data source notification, which is the ATT payload of the twenty-three
/// byte MTU the link comes up with. The watch reassembles across fragments, so
/// this bounds the writes rather than the answer.
const FRAGMENT: usize = 20;
const POLL: Duration = Duration::from_millis(200);

/// The phone: the notification store and the socket the watch reaches it over.
pub struct Phone {
    pipe: Pipe,
    center: NotificationCenter,
}

impl Phone {
    pub fn connect(endpoint: &str) -> std::io::Result<Phone> {
        Ok(Phone {
            pipe: Pipe::connect(endpoint)?,
            center: NotificationCenter::new(),
        })
    }

    /// Wait for the watch to discover ANCS and subscribe to both sources. A
    /// notification announced before it has is delivered to nobody: the
    /// firmware logs it as an hvx on a handle it registered no callback for.
    pub fn subscribed(&mut self, within: Duration) -> std::io::Result<bool> {
        let deadline = Instant::now() + within;
        while Instant::now() < deadline {
            let Some((kind, _)) = self.receive()? else {
                continue;
            };
            if kind == SUBSCRIBED {
                println!("[ancs] the watch subscribed to the notification and data sources");
                return Ok(true);
            }
            println!("[ancs] frame of kind 0x{kind:02x} while waiting for the subscriptions");
        }
        Ok(false)
    }

    pub fn post(
        &mut self,
        app_id: &str,
        title: &str,
        message: &str,
        category: Category,
    ) -> std::io::Result<NotificationId> {
        let (id, announcement) = self.center.post(
            app_id.to_string(),
            title.to_string(),
            String::new(),
            message.to_string(),
            category,
        );
        println!(
            "[ancs] posted notification {} {title:?} from {app_id}",
            id.0
        );
        self.send(ANNOUNCE, &announcement)?;
        Ok(id)
    }

    pub fn dismiss(&mut self, id: NotificationId) -> std::io::Result<bool> {
        let Some(announcement) = self.center.dismiss(id) else {
            println!("[ancs] no live notification {}", id.0);
            return Ok(false);
        };
        println!("[ancs] dismissed notification {}", id.0);
        self.send(ANNOUNCE, &announcement)?;
        Ok(true)
    }

    /// Answer every attribute request the watch writes until it has been quiet
    /// for `quiet`, and report how many were served. A request for a
    /// notification the phone no longer holds is answered with nothing, which
    /// is what a dismissed notification has to say.
    pub fn serve(&mut self, quiet: Duration) -> std::io::Result<usize> {
        let mut served = 0;
        let mut deadline = Instant::now() + quiet;
        while Instant::now() < deadline {
            let Some((kind, payload)) = self.receive()? else {
                continue;
            };
            if kind != REQUEST {
                println!("[ancs] frame of kind 0x{kind:02x} from the watch, ignored");
                continue;
            }
            served += 1;
            deadline = Instant::now() + quiet;
            self.answer(&payload)?;
        }
        Ok(served)
    }

    fn answer(&mut self, write: &[u8]) -> std::io::Result<()> {
        let request = match ControlPoint::parse(write) {
            Ok(request) => request,
            Err(reason) => {
                println!("[ancs] the watch wrote {write:02x?}, which is not a request: {reason}");
                return Ok(());
            }
        };
        let wanted: Vec<String> = request
            .queries
            .iter()
            .map(|query| match query.max_len {
                Some(max) => format!("{:?}(<={max})", query.attribute),
                None => format!("{:?}", query.attribute),
            })
            .collect();
        let Some(notification) = self.center.get(request.id) else {
            println!(
                "[ancs] request for notification {}: {} -- gone, nothing to say",
                request.id.0,
                wanted.join(", ")
            );
            return Ok(());
        };
        println!(
            "[ancs] request for notification {}: {}",
            request.id.0,
            wanted.join(", ")
        );
        let response = request.response(notification);
        for fragment in ancs::fragments(&response, FRAGMENT) {
            self.send(ATTRIBUTES, &fragment)?;
        }
        Ok(())
    }

    fn send(&mut self, kind: u8, payload: &[u8]) -> std::io::Result<()> {
        let mut frame = vec![kind];
        frame.extend_from_slice(payload);
        self.pipe.write_message(&frame)
    }

    fn receive(&mut self) -> std::io::Result<Option<(u8, Vec<u8>)>> {
        let Some(message) = self.pipe.read_message(POLL)? else {
            return Ok(None);
        };
        let Some((kind, payload)) = message.split_first() else {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "an ANCS frame with no kind",
            ));
        };
        Ok(Some((*kind, payload.to_vec())))
    }
}
