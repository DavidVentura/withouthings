//! End to end: a phone that reconnects at once, having left requests unread,
//! is still answered. The pipe stands in for the radio, so what it delivers on a
//! disconnect is what the watch believes about the link.

mod common;

use std::fs;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::Path;
use std::time::Duration;

use common::{client, renode_binary, repository, require, scratch, tail, Renode, Rig};
use wpp::{Command, Frame};

const BOOT_TIMEOUT: Duration = Duration::from_secs(60);
const WPP_PIPE: (&str, u16) = ("127.0.0.1", 7788);
/// Each injected event takes the pipe a few ms, so these are still queued when
/// the next client arrives.
const UNREAD_REQUESTS: usize = 20;
const ANSWER_TIMEOUT: Duration = Duration::from_secs(60);

/// What the pipe counted as still queued when each client left. Without it a
/// queue that had already drained would pass this test for the wrong reason.
fn undelivered_at_departure(directory: &Path) -> Vec<usize> {
    let log = fs::read_to_string(directory.join("log")).expect("Renode keeps a log");
    log.lines()
        .filter_map(|line| line.split("WPP client left ").nth(1))
        .map(|rest| {
            rest.split(' ')
                .next()
                .and_then(|count| count.parse().ok())
                .expect("the pipe logs a count")
        })
        .collect()
}

fn leave_requests_behind() {
    let mut socket = TcpStream::connect(WPP_PIPE).expect("the pipe accepts the connection");
    socket.set_read_timeout(Some(ANSWER_TIMEOUT)).unwrap();
    let request = Frame::new(Command::CMD_BATTERY_STATUS, Vec::new()).to_bytes();
    let mut framed = (request.len() as u16).to_be_bytes().to_vec();
    framed.extend_from_slice(&request);
    // The answer shows the watch holds this link, so it has a disconnect to miss.
    socket.write_all(&framed).expect("the pipe takes the write");
    let mut length = [0u8; 2];
    socket.read_exact(&mut length).expect("the watch answers");
    socket.write_all(&framed.repeat(UNREAD_REQUESTS)).expect("the pipe takes the writes");
}

#[test]
#[ignore = "needs the Renode rig; run with --ignored"]
fn a_reconnect_over_unread_requests_is_answered() {
    require(&[
        ("Renode", &renode_binary()),
        ("the flash dump", &repository().join("renode-sim/external_flash.bin")),
    ]);
    let directory = scratch("reconnect-test");
    Rig::stock().generate(&directory, "rig");
    let renode = Renode::start(&directory, "include @scripts/wpp-pipe.resc");
    renode.wait_for("Add WPPS chars.", BOOT_TIMEOUT).expect("the watch boots");

    let (first, output) = client(&directory, &["--probe-only"]);
    assert!(first, "the first probe failed:\n{}", tail(&output, 20));

    leave_requests_behind();
    std::thread::sleep(Duration::from_millis(100));
    let departed = undelivered_at_departure(&directory);
    assert!(
        departed.last().is_some_and(|count| *count > 0),
        "the client left nothing queued, so this proves nothing: {departed:?}"
    );
    let (answered, output) = client(&directory, &["--sync"]);
    assert!(answered, "the reconnect went unanswered:\n{}", tail(&output, 20));
}
