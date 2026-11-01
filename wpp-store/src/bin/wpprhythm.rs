use std::process::ExitCode;

use wpp::rhythm::{
    read_ecg, read_pulse, EcgVerdict, IntervalKind, PulseVerdict, Rhythm, WATCH_RATE_WITHIN_MS,
};
use wpp::units::{Bpm, Duration, UnixMillis};
use wpp::SampleKind;
use wpp_store::Store;

fn describe(rhythm: &Rhythm) -> String {
    let wave = rhythm
        .wave
        .as_ref()
        .map(|wave| {
            format!(
                " wave {:.1}/min chance {:.3} share {:.2}",
                wave.per_minute, wave.chance, wave.share
            )
        })
        .unwrap_or_default();
    let pattern = format!("{:?}{wave}", rhythm.pattern);
    format!(
        "{pattern} {} bpm variation {:.3} early {}",
        rhythm.rate.0, rhythm.variation, rhythm.early_beats
    )
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let Some(path) = args.get(1) else {
        eprintln!("usage: wpprhythm <watch.db copy>");
        return ExitCode::FAILURE;
    };
    let store = match Store::open(path) {
        Ok(store) => store,
        Err(err) => {
            eprintln!("{path}: {err}");
            return ExitCode::FAILURE;
        }
    };
    let device_id = store.first_device().unwrap().expect("no device");

    let newest = store.afib_episodes(device_id).unwrap();
    let readings = match newest.first() {
        Some(row) => store.afib_readings_up_to(device_id, row.id, UnixMillis(0)).unwrap(),
        None => Vec::new(),
    };
    for row in readings.iter().filter(|r| r.duration > Duration::ZERO) {
        let Ok(raw) = row.intervals() else {
            println!("pulse {:4} corrupt row", row.id);
            continue;
        };
        let watch = store
            .nearest_sample(
                device_id,
                SampleKind::HeartRate,
                row.measured_at,
                Duration::from_millis(WATCH_RATE_WITHIN_MS),
            )
            .unwrap();
        let Some(watch) = watch.filter(|bpm| *bpm > 0) else {
            println!("pulse {:4} no watch rate", row.id);
            continue;
        };
        let reading = read_pulse(&raw, Bpm(watch as u16));
        let merged = reading
            .intervals
            .iter()
            .filter(|i| i.kind == IntervalKind::Merged)
            .count();
        let verdict = match &reading.verdict {
            PulseVerdict::Assessed(rhythm) => describe(rhythm),
            other => format!("{other:?}"),
        };
        println!("pulse {:4} merged {merged:2} {verdict}", row.id);
    }

    let ecgs = store.ecgs(device_id).unwrap();
    for unreadable in &ecgs.unreadable {
        println!("{unreadable}");
    }
    for row in ecgs.rows {
        let id = row.id.0;
        let stored = store.ecg(row.id).unwrap().unwrap();
        let reading = read_ecg(&stored.recording.analysed().samples, stored.recording.rate);
        let p_waves = reading.beats.iter().filter(|b| b.p_wave).count();
        let verdict = match &reading.verdict {
            EcgVerdict::Assessed(rhythm) => describe(rhythm),
            other => format!("{other:?}"),
        };
        println!(
            "ecg {id} beats {} p waves {p_waves} {verdict}",
            reading.beats.len()
        );
    }
    ExitCode::SUCCESS
}
