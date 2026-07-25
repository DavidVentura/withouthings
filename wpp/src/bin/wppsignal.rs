//! Extract stored biosignals (ECG) from a capture into CSV.
//!
//! Usage: wppsignal <btsnoop_hci.log> [--out-dir DIR]
//!
//! A signal arrives as a StoredSignalMeta describing the encoding, a
//! StoredSignalMetaExtend giving its total length, then StoredSignalData
//! chunks. Samples are signed 16-bit little-endian ADC counts, interleaved
//! across the leads the signal type includes.

use std::collections::BTreeMap;
use std::process::ExitCode;

use wpp::capture::{frames, StreamItem};
use wpp::objects::{
    StoredMeasureMeta, StoredSignalMeta, StoredSignalMetaExtend, UnitConversionParameters,
};
use wpp::WppObject;

/// Leads interleaved in a signal, from EcgSignalType.includedLeads in the app.
fn leads(signal_type: u16) -> Option<&'static [&'static str]> {
    match signal_type {
        1 => Some(&["DI"]),
        6 => Some(&["DII", "DIII"]),
        7 => Some(&["DI_FILTERED"]),
        8 => Some(&["DII", "DII_FILTERED", "DIII", "DIII_FILTERED"]),
        13 => Some(&["DI", "DI_FILTERED"]),
        _ => None,
    }
}

fn signal_type_name(signal_type: u16) -> &'static str {
    match signal_type {
        1 => "DI",
        6 => "DII_DIII",
        7 => "DI_FILTERED",
        8 => "DII_DIII_FILTERED",
        13 => "DI_DI_FILTERED",
        _ => "UNKNOWN",
    }
}

#[derive(Default)]
struct Pending {
    meta: Option<StoredSignalMeta>,
    extend: Option<StoredSignalMetaExtend>,
    units: Option<UnitConversionParameters>,
    measure: Option<StoredMeasureMeta>,
    data: Vec<u8>,
}

impl Pending {
    fn ready(&self) -> bool {
        match (&self.meta, &self.extend) {
            (Some(_), Some(extend)) => {
                !self.data.is_empty() && self.data.len() >= extend.total_size as usize
            }
            _ => false,
        }
    }
}

struct Signal {
    meta: StoredSignalMeta,
    extend: StoredSignalMetaExtend,
    units: Option<UnitConversionParameters>,
    measure: Option<StoredMeasureMeta>,
    data: Vec<u8>,
}

/// Split interleaved samples into one column per lead.
fn split_leads(data: &[u8], lead_count: usize) -> Vec<Vec<i16>> {
    let mut columns = vec![Vec::new(); lead_count];
    for (index, pair) in data.chunks_exact(2).enumerate() {
        let sample = i16::from_le_bytes([pair[0], pair[1]]);
        columns[index % lead_count].push(sample);
    }
    columns
}

fn write_csv(dir: &str, index: usize, signal: &Signal) -> std::io::Result<String> {
    let names = leads(signal.meta.r#type).unwrap_or(&["CH0"]);
    let lead_count = signal.meta.channel.max(1) as usize;
    let names: Vec<String> = (0..lead_count)
        .map(|i| {
            names
                .get(i)
                .map(|s| s.to_string())
                .unwrap_or(format!("CH{i}"))
        })
        .collect();
    let columns = split_leads(&signal.data, lead_count);

    let time = signal.measure.as_ref().map(|m| m.time).unwrap_or(0);
    let path = format!(
        "{dir}/ecg_{time}_{}_{index}.csv",
        signal_type_name(signal.meta.r#type).to_lowercase()
    );

    let mut out = String::new();
    out.push_str(&format!(
        "# sampling_freq_hz={} resolution_bits={} sample_bytes={} channels={} duration_s={} total_size={}\n",
        signal.meta.sampling_freq,
        signal.meta.resolution,
        signal.meta.size,
        signal.meta.channel,
        signal.extend.duration,
        signal.extend.total_size
    ));
    if let Some(units) = &signal.units {
        out.push_str(&format!(
            "# offset={} gain={} qfix={} (raw counts; conversion is done in the app's native filter)\n",
            units.offset, units.gain, units.qfix
        ));
    }
    out.push_str(&format!("sample_index,time_s,{}\n", names.join(",")));

    let rows = columns.iter().map(|c| c.len()).min().unwrap_or(0);
    let freq = signal.meta.sampling_freq.max(1) as f64;
    for row in 0..rows {
        let values: Vec<String> = columns.iter().map(|c| c[row].to_string()).collect();
        out.push_str(&format!(
            "{row},{:.6},{}\n",
            row as f64 / freq,
            values.join(",")
        ));
    }
    std::fs::write(&path, out)?;
    Ok(path)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    let out_dir = match args.iter().position(|a| a == "--out-dir") {
        Some(i) => args.get(i + 1).cloned().unwrap_or_else(|| ".".to_string()),
        None => ".".to_string(),
    };
    let Some(path) = args.iter().skip(1).find(|a| !a.starts_with("--")) else {
        eprintln!("usage: wppsignal <btsnoop_hci.log> [--out-dir DIR]");
        return ExitCode::FAILURE;
    };

    let file = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(err) => {
            eprintln!("{path}: {err}");
            return ExitCode::FAILURE;
        }
    };
    let items = match frames(&file) {
        Ok(items) => items,
        Err(err) => {
            eprintln!("{path}: {err}");
            return ExitCode::FAILURE;
        }
    };

    let mut pending = Pending::default();
    let mut signals: Vec<Signal> = Vec::new();
    let mut live_ecg: Vec<u8> = Vec::new();

    let flush = |pending: &mut Pending, signals: &mut Vec<Signal>| {
        if let (Some(meta), Some(extend)) = (pending.meta.clone(), pending.extend.clone()) {
            if !pending.data.is_empty() {
                signals.push(Signal {
                    meta,
                    extend,
                    units: pending.units.clone(),
                    measure: pending.measure.clone(),
                    data: std::mem::take(&mut pending.data),
                });
            }
        }
        pending.data.clear();
    };

    for (_, _, item) in &items {
        let StreamItem::Frame { frame, .. } = item else {
            continue;
        };
        for object in &frame.objects {
            match object {
                WppObject::StoredSignalMeta(meta) => {
                    // The descriptor is repeated during a transfer; only a
                    // different one starts a new signal.
                    if pending.meta.as_ref() != Some(meta) {
                        flush(&mut pending, &mut signals);
                        pending.meta = Some(meta.clone());
                        pending.extend = None;
                        pending.units = None;
                    }
                }
                WppObject::StoredSignalMetaExtend(extend) => pending.extend = Some(extend.clone()),
                WppObject::UnitConversionParameters(units) => pending.units = Some(units.clone()),
                WppObject::StoredMeasureMeta(measure) => pending.measure = Some(measure.clone()),
                WppObject::StoredSignalData(chunk) => {
                    pending.data.extend_from_slice(&chunk.samples);
                    if pending.ready() {
                        flush(&mut pending, &mut signals);
                    }
                }
                WppObject::MeasureLiveEcg(chunk) => live_ecg.extend_from_slice(&chunk.samples),
                _ => {}
            }
        }
    }
    flush(&mut pending, &mut signals);

    if signals.is_empty() && live_ecg.is_empty() {
        println!("no stored signals in {path}");
        return ExitCode::SUCCESS;
    }

    let mut failures = 0;
    for (index, signal) in signals.iter().enumerate() {
        let expected = signal.extend.total_size as usize;
        let got = signal.data.len();
        let lead_count = signal.meta.channel.max(1) as usize;
        let samples = got / signal.meta.size.max(1) as usize;
        let per_lead = samples / lead_count;
        let seconds = per_lead as f64 / signal.meta.sampling_freq.max(1) as f64;

        println!(
            "signal {index}: type {} ({}), {} Hz, {}-bit, {} lead(s) [{}]",
            signal.meta.r#type,
            signal_type_name(signal.meta.r#type),
            signal.meta.sampling_freq,
            signal.meta.resolution,
            lead_count,
            leads(signal.meta.r#type).unwrap_or(&["?"]).join(", ")
        );
        println!(
            "  bytes {got}/{expected} {}, {samples} samples, {per_lead}/lead = {seconds:.2}s (declared {}s)",
            if got == expected { "COMPLETE" } else { "INCOMPLETE" },
            signal.extend.duration
        );
        if got != expected {
            failures += 1;
        }
        match write_csv(&out_dir, index, signal) {
            Ok(path) => println!("  wrote {path}"),
            Err(err) => {
                eprintln!("  writing csv: {err}");
                failures += 1;
            }
        }
    }

    if !live_ecg.is_empty() {
        let samples = split_leads(&live_ecg, 1).remove(0);
        let path = format!("{out_dir}/ecg_live.csv");
        let mut out = String::from("sample_index,raw\n");
        for (i, sample) in samples.iter().enumerate() {
            out.push_str(&format!("{i},{sample}\n"));
        }
        match std::fs::write(&path, out) {
            Ok(()) => println!(
                "live stream: {} bytes, {} samples -> {path}",
                live_ecg.len(),
                samples.len()
            ),
            Err(err) => {
                eprintln!("writing {path}: {err}");
                failures += 1;
            }
        }
    }

    let mut by_type: BTreeMap<u16, usize> = BTreeMap::new();
    for signal in &signals {
        *by_type.entry(signal.meta.r#type).or_default() += 1;
    }
    println!("\n{} stored signal(s): {:?}", signals.len(), by_type);

    if failures == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
