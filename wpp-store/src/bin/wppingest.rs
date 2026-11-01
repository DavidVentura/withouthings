use std::process::ExitCode;

use wpp::capture::{frames, Direction, StreamItem};
use wpp::client::{Action, Category, Client, Credentials, Event};
use wpp_store::Store;

const CATEGORIES: [Category; 2] = [Category(8), Category(9)];

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let mac_at = args.iter().position(|a| a == "--mac");
    let positional: Vec<&String> = args
        .iter()
        .enumerate()
        .skip(1)
        .filter(|(index, a)| !a.starts_with("--") && mac_at.map(|at| at + 1) != Some(*index))
        .map(|(_, a)| a)
        .collect();
    let (Some(capture), Some(db_path)) = (positional.first(), positional.get(1)) else {
        eprintln!("usage: wppingest <btsnoop_hci.log> <out.db> --mac AA:BB:..");
        return ExitCode::FAILURE;
    };
    let Some(mac) = mac_at.and_then(|i| args.get(i + 1)).cloned() else {
        eprintln!("usage: wppingest <btsnoop_hci.log> <out.db> --mac AA:BB:..");
        return ExitCode::FAILURE;
    };

    let file = match std::fs::read(capture.as_str()) {
        Ok(bytes) => bytes,
        Err(err) => {
            eprintln!("{capture}: {err}");
            return ExitCode::FAILURE;
        }
    };
    let items = match frames(&file) {
        Ok(items) => items,
        Err(err) => {
            eprintln!("{capture}: {err}");
            return ExitCode::FAILURE;
        }
    };

    let mut store = match Store::open(db_path) {
        Ok(store) => store,
        Err(err) => {
            eprintln!("{db_path}: {err}");
            return ExitCode::FAILURE;
        }
    };
    let device = store.device(&mac).expect("device row");
    let watermarks = store.watermarks(device, &CATEGORIES).expect("watermarks");

    let mut client = Client::new(
        Credentials::new(&mac, String::new()),
        watermarks,
        store.features(device).expect("features"),
    );

    let (mut sends, mut deletes, mut batches, mut records) = (0usize, 0usize, 0usize, 0usize);
    for captured in &items {
        if captured.direction != Direction::Received {
            continue;
        }
        let StreamItem::Frame { frame, .. } = &captured.item else {
            continue;
        };
        let mut queue: std::collections::VecDeque<Action> = client
            .handle(Event::Frame {
                frame: frame.clone(),
                received_at: captured.received_at,
            })
            .into();
        while let Some(action) = queue.pop_front() {
            match action {
                Action::Send(_) => sends += 1,
                Action::Delete(_) => deletes += 1,
                Action::Finished | Action::Reconnect => {}
                Action::Store {
                    token,
                    records: batch,
                } => {
                    batches += 1;
                    records += batch.len();
                    let unstarted = match store.store(device, &batch) {
                        Ok(unstarted) => unstarted,
                        Err(err) => {
                            eprintln!("store failed: {err}");
                            return ExitCode::FAILURE;
                        }
                    };
                    for workout in unstarted {
                        eprintln!("{workout}");
                    }
                    queue.extend(client.handle(Event::Stored { token }));
                }
            }
        }
    }

    store
        .set_watermarks(device, &client.take_kept_watermarks())
        .expect("watermark");

    println!("frames replayed : {}", items.len());
    println!("store batches   : {batches} ({records} records)");
    println!("would send      : {sends} frames, {deletes} deletes");
    println!();
    for table in ["sample", "workout", "ecg", "sync_state"] {
        println!("{table:<12} {}", store.count(table).expect("the table is countable"));
    }
    ExitCode::SUCCESS
}
