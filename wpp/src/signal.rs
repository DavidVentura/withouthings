//! Biosignal reassembly.
//!
//! A stored signal arrives as a [`StoredSignalMeta`] describing the encoding, a
//! [`StoredSignalMetaExtend`] giving its total length, then a run of
//! [`StoredSignalData`] chunks. Samples are signed 16-bit little-endian ADC
//! counts, interleaved across the leads the signal type carries; this mirrors
//! `EcgSampleParser.decodeRaw` in the Withings app.

use crate::objects::{
    StoredMeasureData, StoredMeasureMeta, StoredSignalMeta, StoredSignalMetaExtend,
    UnitConversionParameters,
};
use crate::WppObject;

/// A single ECG lead, from `EcgLeadType` in the app.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lead {
    Di,
    DiFiltered,
    Dii,
    DiiFiltered,
    Diii,
    DiiiFiltered,
}

impl Lead {
    pub fn name(self) -> &'static str {
        match self {
            Lead::Di => "DI",
            Lead::DiFiltered => "DI_FILTERED",
            Lead::Dii => "DII",
            Lead::DiiFiltered => "DII_FILTERED",
            Lead::Diii => "DIII",
            Lead::DiiiFiltered => "DIII_FILTERED",
        }
    }
}

/// What a signal's `StoredSignalMeta.type` means, from `EcgSignalType`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignalKind {
    Di,
    DiiDiii,
    DiFiltered,
    DiiDiiiFiltered,
    DiDiFiltered,
}

impl SignalKind {
    pub fn from_type_id(type_id: u16) -> Option<SignalKind> {
        match type_id {
            1 => Some(SignalKind::Di),
            6 => Some(SignalKind::DiiDiii),
            7 => Some(SignalKind::DiFiltered),
            8 => Some(SignalKind::DiiDiiiFiltered),
            13 => Some(SignalKind::DiDiFiltered),
            _ => None,
        }
    }

    /// Leads interleaved in the sample stream, in order, from
    /// `EcgSignalType.includedLeads`.
    pub fn leads(self) -> &'static [Lead] {
        match self {
            SignalKind::Di => &[Lead::Di],
            SignalKind::DiiDiii => &[Lead::Dii, Lead::Diii],
            SignalKind::DiFiltered => &[Lead::DiFiltered],
            SignalKind::DiiDiiiFiltered => {
                &[Lead::Dii, Lead::DiiFiltered, Lead::Diii, Lead::DiiiFiltered]
            }
            SignalKind::DiDiFiltered => &[Lead::Di, Lead::DiFiltered],
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            SignalKind::Di => "DI",
            SignalKind::DiiDiii => "DII_DIII",
            SignalKind::DiFiltered => "DI_FILTERED",
            SignalKind::DiiDiiiFiltered => "DII_DIII_FILTERED",
            SignalKind::DiDiFiltered => "DI_DI_FILTERED",
        }
    }
}

/// Sample encoding, from `StoredSignalMeta.format`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleFormat {
    /// Plain samples.
    Raw,
    /// Compressed; the app decodes these in `libecg`, which this crate does not
    /// reimplement.
    Delta,
}

impl SampleFormat {
    pub fn from_meta(format: u8) -> SampleFormat {
        match format {
            3 => SampleFormat::Delta,
            _ => SampleFormat::Raw,
        }
    }
}

/// What the watch concluded about a recording, from `StoredMeasureData.type`.
///
/// These are `ConstantsWs.MEASURE_TYPE_*` in the app; only the two the watch
/// sends with an ECG are named here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MeasureType(pub u16);

impl MeasureType {
    /// The median rate the watch read off its own recording.
    pub const HEART_RATE: MeasureType = MeasureType(11);
    /// The rhythm classification. Computed **on the watch** — the firmware
    /// logs it as `[ECG DIAGNOSIS] ECGSW2 WS Diagnosis` — not by the phone and
    /// not by a server.
    pub const AFIB_RESULT: MeasureType = MeasureType(130);
}

/// The rhythm the watch reports, from `ConstantsWs.AFIB_*`.
///
/// The codes are finer than any app shows: Health Mate collapses them into
/// three outcomes through its own string table. They are kept apart here
/// because the watch draws the distinction, and a reading of "normal heart
/// rate" is a different claim from "no atrial fibrillation".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rhythm {
    /// 0, 9, 10.
    NoAfib,
    /// 1, 11, 12.
    Afib,
    /// 2, 8.
    Inconclusive,
    /// 5, 3 — too noisy to read.
    PoorRecording,
    /// 6, 7 — the rate itself put the recording outside what the classifier
    /// will judge.
    RateOutOfRange,
    /// -3, -2, -1, 4, and anything unrecognised: the watch declined to say.
    NoResult,
}

impl Rhythm {
    pub fn of(code: i32) -> Rhythm {
        match code {
            0 | 9 | 10 => Rhythm::NoAfib,
            1 | 11 | 12 => Rhythm::Afib,
            2 | 8 => Rhythm::Inconclusive,
            3 | 5 => Rhythm::PoorRecording,
            6 | 7 => Rhythm::RateOutOfRange,
            _ => Rhythm::NoResult,
        }
    }
}

/// One reassembled signal and the descriptors that came with it.
#[derive(Debug, Clone, PartialEq)]
pub struct Signal {
    pub meta: StoredSignalMeta,
    pub extend: StoredSignalMetaExtend,
    pub units: Option<UnitConversionParameters>,
    pub measure: Option<StoredMeasureMeta>,
    /// What the watch worked out about the recording, in wire form. Empty for
    /// a recording it said nothing about.
    pub measures: Vec<StoredMeasureData>,
    pub data: Vec<u8>,
}

impl Signal {
    pub fn kind(&self) -> Option<SignalKind> {
        SignalKind::from_type_id(self.meta.r#type)
    }

    pub fn format(&self) -> SampleFormat {
        SampleFormat::from_meta(self.meta.format)
    }

    pub fn sampling_freq(&self) -> u16 {
        self.meta.sampling_freq
    }

    /// Bytes the descriptor said this signal would occupy.
    pub fn declared_size(&self) -> usize {
        self.extend.total_size as usize
    }

    /// Whether every declared byte arrived. A short signal is still decodable,
    /// it just stops early.
    pub fn is_complete(&self) -> bool {
        self.data.len() == self.declared_size()
    }

    pub fn lead_count(&self) -> usize {
        self.meta.channel.max(1) as usize
    }

    /// Samples per lead.
    pub fn samples_per_lead(&self) -> usize {
        self.data.len() / 2 / self.lead_count()
    }

    pub fn duration_secs(&self) -> f64 {
        self.samples_per_lead() as f64 / self.sampling_freq().max(1) as f64
    }

    /// De-interleave into one column per lead. Leads are named when the signal
    /// type is known, and fall back to positional channels when it is not.
    ///
    /// Returns nothing for [`SampleFormat::Delta`], whose samples are
    /// compressed rather than plain.
    pub fn leads(&self) -> Vec<(Option<Lead>, Vec<i16>)> {
        if self.format() == SampleFormat::Delta {
            return Vec::new();
        }
        let count = self.lead_count();
        let names = self.kind().map(|k| k.leads()).unwrap_or(&[]);
        let mut columns: Vec<(Option<Lead>, Vec<i16>)> = (0..count)
            .map(|i| (names.get(i).copied(), Vec::new()))
            .collect();
        for (index, pair) in self.data.chunks_exact(2).enumerate() {
            columns[index % count]
                .1
                .push(i16::from_le_bytes([pair[0], pair[1]]));
        }
        columns
    }
}

#[derive(Default)]
struct Pending {
    meta: Option<StoredSignalMeta>,
    extend: Option<StoredSignalMetaExtend>,
    units: Option<UnitConversionParameters>,
    measure: Option<StoredMeasureMeta>,
    measures: Vec<StoredMeasureData>,
    data: Vec<u8>,
}

/// Reassembles signals from a stream of decoded objects.
#[derive(Default)]
pub struct SignalCollector {
    pending: Pending,
    signals: Vec<Signal>,
    live_ecg: Vec<u8>,
}

impl SignalCollector {
    pub fn new() -> Self {
        SignalCollector::default()
    }

    /// Drop a transfer that will never finish.
    ///
    /// A signal arrives across many frames; if the link dies partway the bytes
    /// held here belong to nothing, and the next transfer would append to them.
    pub fn reset(&mut self) {
        self.pending = Pending::default();
        self.live_ecg.clear();
    }

    pub fn observe(&mut self, object: &WppObject) {
        match object {
            WppObject::StoredSignalMeta(meta) => {
                // The descriptor is repeated during a transfer; only a
                // different one starts a new signal.
                if self.pending.meta.as_ref() != Some(meta) {
                    self.close();
                    self.pending.meta = Some(meta.clone());
                    self.pending.extend = None;
                    self.pending.units = None;
                }
            }
            WppObject::StoredSignalMetaExtend(extend) => self.pending.extend = Some(extend.clone()),
            WppObject::UnitConversionParameters(units) => self.pending.units = Some(units.clone()),
            // Identifies the recording the values below belong to, so a new
            // one leaves the previous recording's conclusions behind.
            WppObject::StoredMeasureMeta(measure) => {
                if self.pending.measure.as_ref() != Some(measure) {
                    self.pending.measure = Some(measure.clone());
                    self.pending.measures.clear();
                }
            }
            // The watch's own conclusions about the recording — its median rate
            // and its rhythm classification. They arrive with the measure that
            // announces the recording, before the waveform is asked for.
            WppObject::StoredMeasureData(data) => {
                if !self.pending.measures.contains(data) {
                    self.pending.measures.push(data.clone());
                }
            }
            WppObject::StoredSignalData(chunk) => {
                self.pending.data.extend_from_slice(&chunk.samples);
                let done = self
                    .pending
                    .extend
                    .as_ref()
                    .is_some_and(|e| self.pending.data.len() >= e.total_size as usize);
                if done {
                    self.close();
                }
            }
            WppObject::MeasureLiveEcg(chunk) => self.live_ecg.extend_from_slice(&chunk.samples),
            _ => {}
        }
    }

    fn close(&mut self) {
        if let (Some(meta), Some(extend)) = (self.pending.meta.clone(), self.pending.extend.clone())
        {
            if !self.pending.data.is_empty() {
                self.signals.push(Signal {
                    meta,
                    extend,
                    units: self.pending.units.clone(),
                    measure: self.pending.measure.clone(),
                    measures: self.pending.measures.clone(),
                    data: std::mem::take(&mut self.pending.data),
                });
            }
        }
        self.pending.data.clear();
    }

    /// Bytes received and expected for a transfer in progress, if one is.
    /// A signal spans hundreds of frames, so this is the one place a sync has
    /// an exact completion figure rather than an estimate.
    pub fn transfer_progress(&self) -> Option<(usize, usize)> {
        let declared = self.pending.extend.as_ref()?.total_size as usize;
        if self.pending.data.is_empty() {
            return None;
        }
        Some((self.pending.data.len(), declared))
    }

    /// Signals finished so far, leaving any transfer still in progress alone.
    /// A signal spans many frames, so draining must not disturb the pending one.
    pub fn take_completed(&mut self) -> Vec<Signal> {
        std::mem::take(&mut self.signals)
    }

    /// Live samples received so far, clearing them from the collector.
    pub fn take_live(&mut self) -> Vec<i16> {
        std::mem::take(&mut self.live_ecg)
            .chunks_exact(2)
            .map(|p| i16::from_le_bytes([p[0], p[1]]))
            .collect()
    }

    /// Completed signals, plus the live ECG stream if the capture held one.
    pub fn finish(mut self) -> (Vec<Signal>, Vec<i16>) {
        self.close();
        let live = self
            .live_ecg
            .chunks_exact(2)
            .map(|p| i16::from_le_bytes([p[0], p[1]]))
            .collect();
        (self.signals, live)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::objects::StoredSignalData;

    fn meta(type_id: u16, channels: u8) -> StoredSignalMeta {
        StoredSignalMeta {
            r#type: type_id,
            sampling_freq: 300,
            format: 0,
            size: 2,
            resolution: 14,
            channel: channels,
        }
    }

    /// Two leads interleaved sample-by-sample, as the ScanWatch 2 sends them.
    #[test]
    fn two_lead_samples_de_interleave() {
        let signal = Signal {
            meta: meta(13, 2),
            extend: StoredSignalMetaExtend {
                duration: 0,
                total_size: 8,
                filter_bank: 0,
            },
            units: None,
            measure: None,
            measures: Vec::new(),
            // 1, -1, 2, -2 as little-endian i16
            data: vec![0x01, 0x00, 0xff, 0xff, 0x02, 0x00, 0xfe, 0xff],
        };
        assert_eq!(
            signal.leads(),
            vec![
                (Some(Lead::Di), vec![1, 2]),
                (Some(Lead::DiFiltered), vec![-1, -2]),
            ]
        );
        assert!(signal.is_complete());
        assert_eq!(signal.samples_per_lead(), 2);
    }

    /// A descriptor repeated mid-transfer must not discard what came before.
    #[test]
    fn a_repeated_descriptor_continues_the_same_signal() {
        let mut collector = SignalCollector::new();
        collector.observe(&WppObject::StoredSignalMeta(meta(7, 1)));
        collector.observe(&WppObject::StoredSignalMetaExtend(StoredSignalMetaExtend {
            duration: 1,
            total_size: 8,
            filter_bank: 0,
        }));
        collector.observe(&WppObject::StoredSignalData(StoredSignalData {
            samples: vec![1, 0, 2, 0],
        }));
        collector.observe(&WppObject::StoredSignalMeta(meta(7, 1)));
        collector.observe(&WppObject::StoredSignalData(StoredSignalData {
            samples: vec![3, 0, 4, 0],
        }));

        let (signals, _) = collector.finish();
        assert_eq!(signals.len(), 1);
        assert_eq!(signals[0].data.len(), 8);
        assert!(signals[0].is_complete());
    }

    #[test]
    fn a_different_descriptor_starts_a_new_signal() {
        let mut collector = SignalCollector::new();
        for type_id in [7u16, 13] {
            collector.observe(&WppObject::StoredSignalMeta(meta(type_id, 1)));
            collector.observe(&WppObject::StoredSignalMetaExtend(StoredSignalMetaExtend {
                duration: 1,
                total_size: 4,
                filter_bank: 0,
            }));
            collector.observe(&WppObject::StoredSignalData(StoredSignalData {
                samples: vec![1, 0, 2, 0],
            }));
        }
        let (signals, _) = collector.finish();
        assert_eq!(signals.len(), 2);
        assert_eq!(signals[0].kind(), Some(SignalKind::DiFiltered));
        assert_eq!(signals[1].kind(), Some(SignalKind::DiDiFiltered));
    }

    #[test]
    fn delta_encoded_signals_are_not_decoded_as_plain_samples() {
        let mut m = meta(7, 1);
        m.format = 3;
        let signal = Signal {
            meta: m,
            extend: StoredSignalMetaExtend {
                duration: 0,
                total_size: 4,
                filter_bank: 0,
            },
            units: None,
            measure: None,
            measures: Vec::new(),
            data: vec![1, 2, 3, 4],
        };
        assert_eq!(signal.format(), SampleFormat::Delta);
        assert!(signal.leads().is_empty());
    }

    /// The watch announces a recording with its own conclusions about it, then
    /// the waveform is fetched. The verdict must survive that gap, and must not
    /// leak onto the next recording.
    #[test]
    fn a_recording_keeps_the_verdict_that_was_announced_with_it() {
        use crate::objects::{StoredMeasureData, StoredMeasureMeta};

        let announce = |uid: u32, code: i32| {
            [
                WppObject::StoredMeasureMeta(StoredMeasureMeta {
                    uid,
                    user_id_cnt: 0,
                    user_id: Vec::new(),
                    attrib: 0,
                    time: 1_700_000_000,
                }),
                WppObject::StoredMeasureData(StoredMeasureData {
                    value: 62,
                    r#type: MeasureType::HEART_RATE.0,
                    exponent: 0,
                }),
                WppObject::StoredMeasureData(StoredMeasureData {
                    value: code,
                    r#type: MeasureType::AFIB_RESULT.0,
                    exponent: 0,
                }),
            ]
        };

        let mut collector = SignalCollector::new();
        for object in announce(1, 9) {
            collector.observe(&object);
        }
        collector.observe(&WppObject::StoredSignalMeta(meta(13, 2)));
        collector.observe(&WppObject::StoredSignalMetaExtend(StoredSignalMetaExtend {
            duration: 0,
            total_size: 4,
            filter_bank: 0,
        }));
        collector.observe(&WppObject::StoredSignalData(StoredSignalData {
            samples: vec![1, 0, 2, 0],
        }));

        let first = collector.take_completed();
        assert_eq!(first.len(), 1);
        let codes: Vec<(u16, i32)> = first[0]
            .measures
            .iter()
            .map(|m| (m.r#type, m.value))
            .collect();
        assert_eq!(codes, vec![(11, 62), (130, 9)]);

        // A different recording replaces them rather than adding to them.
        for object in announce(2, 5) {
            collector.observe(&object);
        }
        let mut second = meta(13, 2);
        second.size = 4;
        collector.observe(&WppObject::StoredSignalMeta(second));
        collector.observe(&WppObject::StoredSignalMetaExtend(StoredSignalMetaExtend {
            duration: 0,
            total_size: 4,
            filter_bank: 0,
        }));
        collector.observe(&WppObject::StoredSignalData(StoredSignalData {
            samples: vec![3, 0, 4, 0],
        }));
        let next = collector.take_completed();
        assert_eq!(next.len(), 1);
        assert_eq!(
            next[0].measures.iter().map(|m| m.value).collect::<Vec<_>>(),
            vec![62, 5],
        );
    }
}
