use std::process::ExitCode;

use wpp::capture::{frames, Direction, StreamItem};
use wpp::client::{Action, Category, Client, Credentials, Event};
use wpp_store::Store;

const CATEGORIES: [Category; 2] = [Category(8), Category(9)];

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let positional: Vec<&String> = args
        .iter()
        .skip(1)
        .filter(|a| !a.starts_with("--"))
        .collect();
    let (Some(capture), Some(db_path)) = (positional.first(), positional.get(1)) else {
        eprintln!("usage: wppingest <btsnoop_hci.log> <out.db> [--mac AA:BB:..]");
        return ExitCode::FAILURE;
    };
    let mac = match args.iter().position(|a| a == "--mac") {
        Some(i) => args.get(i + 1).cloned().unwrap_or_default(),
        None => "unknown".to_string(),
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
        Credentials {
            mac: mac.clone(),
            secret: String::new(),
        },
        watermarks,
    );

    let (mut sends, mut deletes, mut batches, mut records) = (0usize, 0usize, 0usize, 0usize);
    for captured in &items {
        if captured.direction != Direction::Received {
            continue;
        }
        let StreamItem::Frame { frame, .. } = &captured.item else {
            continue;
        };
        let mut queue = client.handle(Event::Frame {
            frame: frame.clone(),
            received_at: captured.received_at,
        });
        while let Some(action) = queue.pop() {
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
                    if let Err(err) = store.store(device, &batch) {
                        eprintln!("store failed: {err}");
                        return ExitCode::FAILURE;
                    }
                    queue.extend(client.handle(Event::Stored { token }));
                }
            }
        }
    }

    for (category, through) in client.watermarks() {
        store
            .set_watermark(device, category, through)
            .expect("watermark");
    }

    println!("frames replayed : {}", items.len());
    println!("store batches   : {batches} ({records} records)");
    println!("would send      : {sends} frames, {deletes} deletes");
    println!();
    for table in ["sample", "workout", "ecg", "sync_state"] {
        println!("{table:<12} {}", store.count(table).unwrap_or(-1));
    }
    ExitCode::SUCCESS
}
