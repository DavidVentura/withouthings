//! Extract stored biosignals (ECG) from a capture into CSV.
//!
//! Usage: wppsignal <btsnoop_hci.log> [--out-dir DIR]
//!
//! Reassembly and sample decoding live in `wpp::signal`; this is only the file
//! and CSV handling around it.

use std::process::ExitCode;

use wpp::analysis::detect_r_peaks;
use wpp::capture::{frames, StreamItem};
use wpp::signal::{Recording, Signal, SignalCollector};
use wpp::units::{Millivolts, COUNTS_PER_MILLIVOLT};
use wpp::WppObject;

fn csv_for(signal: &Signal, recording: &Recording) -> String {
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
            "# offset={} gain={} qfix={} (gain is an analog front-end parameter, not a scale)\n",
            units.offset, units.gain, units.qfix
        ));
    }

    let columns = recording.leads();
    let names: Vec<String> = columns
        .iter()
        .enumerate()
        .map(|(i, column)| {
            column
                .lead
                .map(|l| l.name().to_string())
                .unwrap_or(format!("CH{i}"))
        })
        .collect();
    out.push_str(&format!(
        "# samples are microvolts: {} counts per mV\n",
        COUNTS_PER_MILLIVOLT
    ));
    let headers: Vec<String> = names.iter().map(|n| format!("{n}_uv,{n}_mv")).collect();
    out.push_str(&format!("sample_index,time_s,{}\n", headers.join(",")));

    let rows = columns.iter().map(|c| c.samples.len()).min().unwrap_or(0);
    let freq = recording.rate.as_f64();
    for row in 0..rows {
        let values: Vec<String> = columns
            .iter()
            .map(|c| format!("{},{:.3}", c.samples[row], Millivolts::from_counts(c.samples[row]).0))
            .collect();
        out.push_str(&format!(
            "{row},{:.6},{}\n",
            row as f64 / freq,
            values.join(",")
        ));
    }
    out
}

/// Rate derived from the waveform, next to the rate the watch reported.
fn report_heart_rate(recording: &Recording, reported: &[u16]) {
    let lead = recording.analysed();
    let peaks = detect_r_peaks(&lead.samples, recording.rate);
    let name = lead.lead.map(|l| l.name()).unwrap_or("lead 0");
    match peaks.heart_rate() {
        Some(bpm) => {
            let spread = peaks
                .rr_stddev()
                .map(|s| format!("{:.0} ms", s.0))
                .unwrap_or_else(|| "n/a".to_string());
            println!(
                "  {name}: {} R peaks -> {} bpm (RR sd {spread})",
                peaks.indices.len(),
                bpm.0
            );
        }
        None => println!("  {name}: no R peaks detected"),
    }
    if !reported.is_empty() {
        let mut sorted = reported.to_vec();
        sorted.sort_unstable();
        println!(
            "  watch reported: {} bpm median over {} LIVE_HR samples ({}-{})",
            sorted[sorted.len() / 2],
            sorted.len(),
            sorted[0],
            sorted[sorted.len() - 1]
        );
    }
}

fn report(index: usize, signal: &Signal, recording: &Recording) {
    let kind = signal.kind().map(|k| k.name()).unwrap_or("UNKNOWN");
    let leads: Vec<&str> = recording
        .leads()
        .iter()
        .enumerate()
        .map(|(i, column)| {
            column
                .lead
                .map(|l| l.name())
                .unwrap_or_else(|| ["CH0", "CH1", "CH2", "CH3"][i.min(3)])
        })
        .collect();
    println!(
        "signal {index}: type {} ({kind}), {} Hz, {}-bit, {} lead(s) [{}]",
        signal.meta.r#type,
        signal.sampling_freq(),
        signal.meta.resolution,
        recording.leads().len(),
        leads.join(", ")
    );
    println!(
        "  bytes {}/{} {}, {} samples/lead = {:.2}s (declared {}s)",
        signal.data.len(),
        signal.declared_size(),
        if signal.is_complete() {
            "COMPLETE"
        } else {
            "INCOMPLETE"
        },
        recording.samples_per_lead(),
        recording.seconds(),
        signal.extend.duration
    );
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    const USAGE: &str = "usage: wppsignal <btsnoop_hci.log> [--out-dir DIR]";
    let mut out_dir = ".".to_string();
    let mut capture = None;
    let mut arguments = args.iter().skip(1);
    while let Some(argument) = arguments.next() {
        match argument.as_str() {
            "--out-dir" => {
                let Some(dir) = arguments.next() else {
                    eprintln!("{USAGE}");
                    return ExitCode::FAILURE;
                };
                out_dir = dir.clone();
            }
            flag if flag.starts_with("--") => {
                eprintln!("unknown argument {flag}\n{USAGE}");
                return ExitCode::FAILURE;
            }
            _ => capture = Some(argument),
        }
    }
    let Some(path) = capture else {
        eprintln!("{USAGE}");
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

    let mut collector = SignalCollector::new();
    let mut reported_hr: Vec<u16> = Vec::new();
    for captured in &items {
        if let StreamItem::Frame { frame, .. } = &captured.item {
            for object in &frame.objects {
                collector.observe(object);
                if let WppObject::LiveHr(live) = object {
                    reported_hr.push(live.heart_rate().0);
                }
            }
        }
    }
    let (signals, live) = collector.finish();

    if signals.is_empty() && live.is_empty() {
        println!("no signals in {path}");
        return ExitCode::SUCCESS;
    }

    let mut failures = 0;
    for (index, signal) in signals.iter().enumerate() {
        let recording = match signal.recording() {
            Ok(recording) => recording,
            Err(err) => {
                println!("signal {index}: type {} not decoded: {err}", signal.meta.r#type);
                failures += 1;
                continue;
            }
        };
        report(index, signal, &recording);
        report_heart_rate(&recording, &reported_hr);
        if !signal.is_complete() {
            failures += 1;
        }
        let time = signal.measure.as_ref().map(|m| m.time).unwrap_or(0);
        let kind = signal
            .kind()
            .map(|k| k.name())
            .unwrap_or("unknown")
            .to_lowercase();
        let path = format!("{out_dir}/ecg_{time}_{kind}_{index}.csv");
        match std::fs::write(&path, csv_for(signal, &recording)) {
            Ok(()) => println!("  wrote {path}"),
            Err(err) => {
                eprintln!("  writing {path}: {err}");
                failures += 1;
            }
        }
    }

    if !live.is_empty() {
        let path = format!("{out_dir}/ecg_live.csv");
        let mut out = String::from("sample_index,raw\n");
        for (i, sample) in live.iter().enumerate() {
            out.push_str(&format!("{i},{sample}\n"));
        }
        match std::fs::write(&path, out) {
            Ok(()) => println!("live stream: {} samples -> {path}", live.len()),
            Err(err) => {
                eprintln!("writing {path}: {err}");
                failures += 1;
            }
        }
    }

    if failures == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
