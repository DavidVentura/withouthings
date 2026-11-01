//! The simulator's sockets: one message per [len u16 big-endian][payload].

use std::io::{self, ErrorKind, Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

const LENGTH_BYTES: usize = 2;

pub struct Pipe {
    socket: TcpStream,
    received: Vec<u8>,
}

impl Pipe {
    pub fn connect(endpoint: &str) -> io::Result<Pipe> {
        let socket = TcpStream::connect(endpoint)?;
        socket.set_nodelay(true)?;
        Ok(Pipe { socket, received: Vec::new() })
    }

    pub fn write_message(&mut self, payload: &[u8]) -> io::Result<()> {
        let mut message = Vec::with_capacity(payload.len() + LENGTH_BYTES);
        message.extend_from_slice(&(payload.len() as u16).to_be_bytes());
        message.extend_from_slice(payload);
        self.socket.write_all(&message)
    }

    /// None when no whole message arrived in time. A part of one stays buffered
    /// for the next read, which is what keeps the stream's framing across a
    /// timeout. A closed socket is an error, since no wait can end it.
    pub fn read_message(&mut self, within: Duration) -> io::Result<Option<Vec<u8>>> {
        let deadline = Instant::now() + within;
        loop {
            if let Some(message) = take_message(&mut self.received) {
                return Ok(Some(message));
            }
            let Some(left) = deadline.checked_duration_since(Instant::now()).filter(|left| !left.is_zero()) else {
                return Ok(None);
            };
            self.socket.set_read_timeout(Some(left))?;
            let mut chunk = [0u8; 4096];
            match self.socket.read(&mut chunk) {
                Ok(0) => return Err(io::Error::new(ErrorKind::UnexpectedEof, "the pipe closed")),
                Ok(read) => self.received.extend_from_slice(&chunk[..read]),
                Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => return Ok(None),
                Err(e) => return Err(e),
            }
        }
    }
}

fn take_message(buffer: &mut Vec<u8>) -> Option<Vec<u8>> {
    let header = buffer.get(..LENGTH_BYTES)?;
    let length = u16::from_be_bytes([header[0], header[1]]) as usize;
    if buffer.len() < LENGTH_BYTES + length {
        return None;
    }
    let message = buffer[LENGTH_BYTES..LENGTH_BYTES + length].to_vec();
    buffer.drain(..LENGTH_BYTES + length);
    Some(message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn connected() -> (Pipe, TcpStream) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let pipe = Pipe::connect(&listener.local_addr().unwrap().to_string()).unwrap();
        (pipe, listener.accept().unwrap().0)
    }

    #[test]
    fn a_timeout_inside_a_message_loses_nothing() {
        let (mut pipe, mut peer) = connected();
        peer.write_all(&[0, 3, 7]).unwrap();
        assert_eq!(pipe.read_message(Duration::from_millis(50)).unwrap(), None);
        peer.write_all(&[8, 9, 0, 1, 5]).unwrap();
        let within = Duration::from_secs(5);
        assert_eq!(pipe.read_message(within).unwrap(), Some(vec![7, 8, 9]));
        assert_eq!(pipe.read_message(within).unwrap(), Some(vec![5]));
    }

    #[test]
    fn a_closed_pipe_is_an_error_rather_than_a_quiet_one() {
        let (mut pipe, peer) = connected();
        drop(peer);
        let closed = pipe.read_message(Duration::from_secs(5)).unwrap_err();
        assert_eq!(closed.kind(), ErrorKind::UnexpectedEof);
    }
}
