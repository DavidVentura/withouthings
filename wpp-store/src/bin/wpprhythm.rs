use std::process::ExitCode;

use wpp::rhythm::{
    read_ecg, read_pulse, EcgVerdict, IntervalKind, PulseVerdict, Rhythm, WATCH_RATE_WITHIN_MS,
};
use wpp::signal::{Lead, SignalKind};
use wpp::units::{Bpm, Millis};
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
    let device_id: i64 = store
        .connection()
        .query_row("SELECT id FROM device ORDER BY id LIMIT 1", [], |r| {
            r.get(0)
        })
        .expect("no device");

    let newest = store.afib_episodes(device_id).unwrap();
    let readings = match newest.first() {
        Some(row) => store.afib_readings_up_to(device_id, row.id, 0).unwrap(),
        None => Vec::new(),
    };
    for row in readings.iter().filter(|r| r.duration_secs > 0) {
        let raw: Vec<Millis> = wpp::signal::beat_intervals_ms(
            &row.samples,
            row.sample_bytes as usize,
            row.sampling_hz as u16,
        )
        .into_iter()
        .map(|ms| Millis(ms as f64))
        .collect();
        let watch = store
            .nearest_sample(
                device_id,
                SampleKind::HeartRate.id(),
                row.measured_at_ms,
                WATCH_RATE_WITHIN_MS,
            )
            .unwrap();
        let Some(watch) = watch else {
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

    for (id, ..) in store.ecgs(device_id).unwrap() {
        let (_, signal_type, hz, lead_count, bytes) = store.ecg(id).unwrap().unwrap();
        let lead_count = lead_count.max(1) as usize;
        let leads = SignalKind::from_type_id(signal_type as u16)
            .map(|k| k.leads())
            .unwrap_or(&[]);
        let column = leads
            .iter()
            .position(|l| matches!(l, Lead::DiFiltered | Lead::DiiFiltered))
            .unwrap_or(0);
        let samples: Vec<i16> = bytes
            .chunks_exact(2)
            .skip(column)
            .step_by(lead_count)
            .map(|pair| i16::from_le_bytes([pair[0], pair[1]]))
            .collect();
        let reading = read_ecg(&samples, hz as u16);
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
