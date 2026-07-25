//! Biosignal reassembly.
//!
//! A stored signal arrives as a [`StoredSignalMeta`] describing the encoding, a
//! [`StoredSignalMetaExtend`] giving its total length, then a run of
//! [`StoredSignalData`] chunks. Samples are signed 16-bit little-endian ADC
//! counts, interleaved across the leads the signal type carries; this mirrors
//! `EcgSampleParser.decodeRaw` in the Withings app.

use crate::objects::{
    StoredMeasureMeta, StoredSignalMeta, StoredSignalMetaExtend, UnitConversionParameters,
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

/// One reassembled signal and the descriptors that came with it.
#[derive(Debug, Clone, PartialEq)]
pub struct Signal {
    pub meta: StoredSignalMeta,
    pub extend: StoredSignalMetaExtend,
    pub units: Option<UnitConversionParameters>,
    pub measure: Option<StoredMeasureMeta>,
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
            WppObject::StoredMeasureMeta(measure) => self.pending.measure = Some(measure.clone()),
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
                    data: std::mem::take(&mut self.pending.data),
                });
            }
        }
        self.pending.data.clear();
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
            data: vec![1, 2, 3, 4],
        };
        assert_eq!(signal.format(), SampleFormat::Delta);
        assert!(signal.leads().is_empty());
    }
}
