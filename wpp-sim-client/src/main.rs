use std::env;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use wpp::client::probe_frame;
use wpp::commands::Command;
use wpp::frame::Frame;

// The simulator's WPP pipe, one message per [len u16 big-endian][payload].
// Inbound a message is one GATT write to the WPP characteristic, outbound one
// notification, so both directions carry the byte stream a real link would.
const DEFAULT_ENDPOINT: &str = "127.0.0.1:7788";
const ATT_MTU: usize = 23;
const WRITE_LIMIT: usize = ATT_MTU - 3;
const REPLY_TIMEOUT: Duration = Duration::from_secs(45);

struct Link {
    socket: TcpStream,
    received: Vec<u8>,
}

impl Link {
    fn connect(endpoint: &str) -> std::io::Result<Link> {
        let socket = TcpStream::connect(endpoint)?;
        socket.set_nodelay(true)?;
        socket.set_read_timeout(Some(REPLY_TIMEOUT))?;
        Ok(Link { socket, received: Vec::new() })
    }

    fn send(&mut self, frame: &Frame) -> std::io::Result<()> {
        println!("-> {:?}", frame.command.opcode_name());
        for part in frame.to_wire() {
            for write in part.to_bytes().chunks(WRITE_LIMIT) {
                let mut message = Vec::with_capacity(write.len() + 2);
                message.extend_from_slice(&(write.len() as u16).to_be_bytes());
                message.extend_from_slice(write);
                self.socket.write_all(&message)?;
            }
        }
        Ok(())
    }

    fn receive(&mut self) -> std::io::Result<Frame> {
        loop {
            let mut header = [0u8; 2];
            self.socket.read_exact(&mut header)?;
            let mut notification = vec![0u8; u16::from_be_bytes(header) as usize];
            self.socket.read_exact(&mut notification)?;
            self.received.extend_from_slice(&notification);
            // Anything short of a whole frame is a reply still arriving, since
            // the watch notifies in MTU-sized pieces.
            let Ok((frame, rest)) = Frame::parse_prefix(&self.received) else {
                continue;
            };
            let consumed = self.received.len() - rest.len();
            self.received.drain(..consumed);
            return Ok(frame);
        }
    }
}

fn main() -> std::io::Result<()> {
    let endpoint = env::args().nth(1).unwrap_or_else(|| DEFAULT_ENDPOINT.to_string());
    let mut link = Link::connect(&endpoint)?;
    println!("connected to {endpoint}");

    for request in [probe_frame(), Frame::new(Command::CMD_BATTERY_STATUS, Vec::new())] {
        link.send(&request)?;
        let reply = link.receive()?;
        println!("<- {:?}", reply.command.opcode_name());
        for object in &reply.objects {
            println!("   {object:?}");
        }
    }
    Ok(())
}
