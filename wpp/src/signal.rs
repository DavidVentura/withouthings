use crate::objects::{
    StoredMeasureData, StoredMeasureMeta, StoredSignalMeta, StoredSignalMetaExtend,
    UnitConversionParameters,
};
use std::num::NonZeroUsize;

use crate::units::{Bpm, LeadCount, SamplingHz, UnixTime};
use crate::WppObject;

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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleFormat {
    Raw,
    Delta,
}

impl SampleFormat {
    pub const DELTA_META: u8 = 3;

    pub fn from_meta(format: u8) -> Option<SampleFormat> {
        match format {
            0..=2 => Some(SampleFormat::Raw),
            SampleFormat::DELTA_META => Some(SampleFormat::Delta),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MeasureType(pub u16);

impl MeasureType {
    pub const HEART_RATE: MeasureType = MeasureType(11);
    pub const AFIB_RESULT: MeasureType = MeasureType(130);
    pub const SPO2: MeasureType = MeasureType(54);
    pub const PPG_AFIB_RESULT: MeasureType = MeasureType(139);
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EcgMeasure {
    pub kind: MeasureType,
    pub value: i64,
    pub exponent: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WatchVerdict {
    pub rate: Option<Bpm>,
    pub rhythm: Option<Rhythm>,
}

impl WatchVerdict {
    pub fn of(measures: &[EcgMeasure]) -> WatchVerdict {
        let reading = |kind: MeasureType| {
            measures
                .iter()
                .find(|measure| measure.kind == kind)
                .map(|measure| measure.value as f64 * 10f64.powi(measure.exponent as i32))
        };
        WatchVerdict {
            rate: reading(MeasureType::HEART_RATE)
                .filter(|bpm| *bpm > 0.0)
                .map(|bpm| Bpm(bpm.round() as u16)),
            rhythm: reading(MeasureType::AFIB_RESULT).and_then(|code| Rhythm::of(code as i32)),
        }
    }
}

/// StoredMeasureMeta.attrib on the reading that confirmed a run: the reply
/// builder at 0x6370c sets it from the header flag the store writer takes from
/// its confirmed out-parameter.
pub const STORED_MEASURE_ATTRIB_CONFIRMED: i64 = 7;

/// Where the watch keeps a signal between syncs. `CMD_STORED_MEASURE_SIGNAL_GET`
/// (handler 0x6e21c) serves a store only to a request whose StoredSignalMeta
/// names its type, one record per request, and `..._DEL` routes on the same
/// type; the type of the signal inside a record (an ECG is 7, say) is not it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignalStore {
    Ecg,
    /// Spot checks taken on the watch, gss file 0xf.
    Spo2,
    /// The episodes behind the "irregular rhythm, take an ECG" alert.
    PpgAfib,
}

impl SignalStore {
    pub fn type_id(self) -> u16 {
        match self {
            SignalStore::Ecg => 1,
            SignalStore::Spo2 => 4,
            SignalStore::PpgAfib => 5,
        }
    }
}

/// How the watch turns PPG AFib readings into an alert: the store writer at
/// 0x63394 counts each reading the classifier calls AFib and confirms the one
/// that makes `RUN` within `WINDOW` of the first counted, then counts afresh.
/// Both come from the constant table at 0xb0e88 that main_task hands to
/// 0x63098.
pub struct PpgAfibRule;

impl PpgAfibRule {
    pub const RUN: usize = 10;
    pub const WINDOW_SECS: i64 = 86_400;

    /// The readings the watch counted towards the confirmed one at `confirmed`,
    /// oldest first and ending with it: those within the window before it, back
    /// to the previous confirmed reading. `readings` is (time, confirmed) in
    /// time order. A normal reading between them also restarts the count, and
    /// the watch keeps no record of those, so this is what it counted at most.
    pub fn counted(readings: &[(UnixTime, bool)], confirmed: usize) -> std::ops::Range<usize> {
        let (at, _) = readings[confirmed];
        let earliest = at.0 - Self::WINDOW_SECS;
        let mut start = confirmed;
        while start > 0 && confirmed - start + 1 < Self::RUN {
            let (previous, was_confirmed) = readings[start - 1];
            if was_confirmed || previous.0 < earliest {
                break;
            }
            start -= 1;
        }
        start..confirmed + 1
    }
}

/// The intervals between successive beats of a PPG AFib reading, in
/// milliseconds. The store writer keeps each as the difference of two peak
/// positions in samples, `size` bytes little-endian, at the reading's own rate.
pub fn beat_intervals_ms(samples: &[u8], size: NonZeroUsize, sampling_hz: SamplingHz) -> Vec<u32> {
    samples
        .chunks_exact(size.get())
        .map(|bytes| {
            let counts = bytes
                .iter()
                .rev()
                .fold(0u32, |value, byte| (value << 8) | u32::from(*byte));
            counts * 1000 / u32::from(sampling_hz.get())
        })
        .collect()
}

/// The PPG classifier's verdict, in the order of `afib_class_names` (0xb4824),
/// which the firmware indexes with the verdict plus one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PpgRhythm {
    Undefined,
    SinusRhythm,
    Afib,
    Other,
    Noise,
}

impl PpgRhythm {
    pub fn of(code: i32) -> Option<PpgRhythm> {
        match code {
            -1 => Some(PpgRhythm::Undefined),
            0 => Some(PpgRhythm::SinusRhythm),
            1 => Some(PpgRhythm::Afib),
            2 => Some(PpgRhythm::Other),
            3 => Some(PpgRhythm::Noise),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rhythm {
    NoAfib,
    Afib,
    Inconclusive,
    PoorRecording,
    RateOutOfRange,
    NoResult,
}

impl Rhythm {
    pub fn of(code: i32) -> Option<Rhythm> {
        match code {
            0 | 9 | 10 => Some(Rhythm::NoAfib),
            1 | 11 | 12 => Some(Rhythm::Afib),
            2 | 8 => Some(Rhythm::Inconclusive),
            3 | 5 => Some(Rhythm::PoorRecording),
            6 | 7 => Some(Rhythm::RateOutOfRange),
            -2 | -1 | 4 => Some(Rhythm::NoResult),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Signal {
    pub meta: StoredSignalMeta,
    pub extend: StoredSignalMetaExtend,
    pub units: Option<UnitConversionParameters>,
    pub measure: Option<StoredMeasureMeta>,
    pub measures: Vec<StoredMeasureData>,
    pub data: Vec<u8>,
}

impl Signal {
    pub fn kind(&self) -> Option<SignalKind> {
        SignalKind::from_type_id(self.meta.r#type)
    }

    pub fn sampling_freq(&self) -> u16 {
        self.meta.sampling_freq
    }

    pub fn declared_size(&self) -> usize {
        self.extend.total_size as usize
    }

    pub fn is_complete(&self) -> bool {
        self.data.len() == self.declared_size()
    }

    pub fn recording(&self) -> Result<Recording, RecordingError> {
        Recording::decode(
            self.meta.r#type,
            self.meta.format,
            self.meta.sampling_freq,
            self.meta.channel as usize,
            &self.data,
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecordingError {
    /// Nothing here decodes these, and read as plain samples they are a wrong
    /// waveform.
    DeltaCoded,
    UnknownFormat(u8),
    NoSamplingRate,
    NoLeads,
}

impl std::fmt::Display for RecordingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RecordingError::DeltaCoded => write!(f, "delta-coded samples"),
            RecordingError::UnknownFormat(code) => write!(f, "unknown sample format {code}"),
            RecordingError::NoSamplingRate => write!(f, "a sampling rate of zero"),
            RecordingError::NoLeads => write!(f, "no leads"),
        }
    }
}

impl std::error::Error for RecordingError {}

#[derive(Debug, Clone, PartialEq)]
pub struct RecordedLead {
    /// None past the leads the signal type names, or for a type not known.
    pub lead: Option<Lead>,
    pub samples: Vec<i16>,
}

/// A waveform taken apart into its leads, however it arrived.
#[derive(Debug, Clone, PartialEq)]
pub struct Recording {
    pub kind: Option<SignalKind>,
    pub rate: SamplingHz,
    leads: Vec<RecordedLead>,
}

impl Recording {
    pub fn decode(
        type_id: u16,
        format: u8,
        sampling_hz: u16,
        lead_count: usize,
        bytes: &[u8],
    ) -> Result<Recording, RecordingError> {
        match SampleFormat::from_meta(format) {
            None => return Err(RecordingError::UnknownFormat(format)),
            Some(SampleFormat::Delta) => return Err(RecordingError::DeltaCoded),
            Some(SampleFormat::Raw) => {}
        }
        let rate = SamplingHz::new(sampling_hz).ok_or(RecordingError::NoSamplingRate)?;
        let count = LeadCount::new(lead_count).ok_or(RecordingError::NoLeads)?.get();
        let kind = SignalKind::from_type_id(type_id);
        let names = kind.map_or(&[][..], SignalKind::leads);
        let mut leads: Vec<RecordedLead> = (0..count)
            .map(|i| RecordedLead {
                lead: names.get(i).copied(),
                samples: Vec::with_capacity(bytes.len() / 2 / count),
            })
            .collect();
        for (index, pair) in bytes.chunks_exact(2).enumerate() {
            leads[index % count]
                .samples
                .push(i16::from_le_bytes([pair[0], pair[1]]));
        }
        Ok(Recording { kind, rate, leads })
    }

    pub fn leads(&self) -> &[RecordedLead] {
        &self.leads
    }

    /// The raw lead clips on this watch while the filtered one holds the whole
    /// complex, so that is the one to read beats from.
    pub fn analysed(&self) -> &RecordedLead {
        self.leads
            .iter()
            .find(|l| matches!(l.lead, Some(Lead::DiFiltered | Lead::DiiFiltered)))
            .unwrap_or(&self.leads[0])
    }

    pub fn samples_per_lead(&self) -> usize {
        self.leads[0].samples.len()
    }

    pub fn seconds(&self) -> f64 {
        self.samples_per_lead() as f64 / self.rate.as_f64()
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
                if self.pending.meta.as_ref() != Some(meta) {
                    self.close();
                    self.pending.meta = Some(meta.clone());
                    self.pending.extend = None;
                    self.pending.units = None;
                }
            }
            WppObject::StoredSignalMetaExtend(extend) => self.pending.extend = Some(extend.clone()),
            WppObject::UnitConversionParameters(units) => self.pending.units = Some(units.clone()),
            WppObject::StoredMeasureMeta(measure) => {
                if self.pending.measure.as_ref() != Some(measure) {
                    self.pending.measure = Some(measure.clone());
                    self.pending.measures.clear();
                }
            }
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

    pub fn transfer_progress(&self) -> Option<(usize, usize)> {
        let declared = self.pending.extend.as_ref()?.total_size as usize;
        if self.pending.data.is_empty() {
            return None;
        }
        Some((self.pending.data.len(), declared))
    }

    pub fn take_completed(&mut self) -> Vec<Signal> {
        std::mem::take(&mut self.signals)
    }

    pub fn take_live(&mut self) -> Vec<i16> {
        std::mem::take(&mut self.live_ecg)
            .chunks_exact(2)
            .map(|p| i16::from_le_bytes([p[0], p[1]]))
            .collect()
    }

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

    fn measure(kind: MeasureType, value: i64, exponent: i64) -> EcgMeasure {
        EcgMeasure { kind, value, exponent }
    }

    #[test]
    fn a_verdict_applies_each_measures_exponent() {
        let verdict = WatchVerdict::of(&[
            measure(MeasureType::HEART_RATE, 7240, -2),
            measure(MeasureType::AFIB_RESULT, 1, 0),
        ]);
        assert_eq!(verdict.rate, Some(Bpm(72)));
        assert_eq!(verdict.rhythm, Rhythm::of(1));
    }

    #[test]
    fn a_verdict_without_a_usable_rate_or_rhythm_has_none() {
        assert_eq!(
            WatchVerdict::of(&[]),
            WatchVerdict { rate: None, rhythm: None }
        );
        let zero_rate = WatchVerdict::of(&[measure(MeasureType::HEART_RATE, 0, 0)]);
        assert_eq!(zero_rate.rate, None);
        let unknown_code = WatchVerdict::of(&[measure(MeasureType::AFIB_RESULT, 12345, 0)]);
        assert_eq!(unknown_code.rhythm, None);
    }
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
            data: vec![0x01, 0x00, 0xff, 0xff, 0x02, 0x00, 0xfe, 0xff],
        };
        let recording = signal.recording().unwrap();
        assert_eq!(
            recording.leads(),
            &[
                RecordedLead { lead: Some(Lead::Di), samples: vec![1, 2] },
                RecordedLead { lead: Some(Lead::DiFiltered), samples: vec![-1, -2] },
            ]
        );
        assert_eq!(recording.analysed().lead, Some(Lead::DiFiltered));
        assert!(signal.is_complete());
        assert_eq!(recording.samples_per_lead(), 2);
    }

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
        assert_eq!(signal.recording(), Err(RecordingError::DeltaCoded));
    }

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

#[cfg(test)]
mod afib_rule_tests {
    use super::*;

    fn at(minutes: i64, confirmed: bool) -> (UnixTime, bool) {
        (UnixTime(minutes * 60), confirmed)
    }

    #[test]
    fn a_full_run_is_the_confirmed_reading_and_the_nine_before_it() {
        let readings: Vec<_> = (0..12).map(|i| at(i * 10, i == 11)).collect();
        assert_eq!(PpgAfibRule::counted(&readings, 11), 2..12);
    }

    #[test]
    fn the_count_stops_at_the_previous_confirmation() {
        let readings = vec![at(0, false), at(10, true), at(20, false), at(30, true)];
        assert_eq!(PpgAfibRule::counted(&readings, 3), 2..4);
    }

    #[test]
    fn readings_older_than_the_window_are_not_counted() {
        let readings = vec![at(0, false), at(25 * 60, false), at(26 * 60, true)];
        assert_eq!(PpgAfibRule::counted(&readings, 2), 1..3);
    }

    #[test]
    fn a_row_that_cannot_be_a_recording_says_why() {
        assert_eq!(Recording::decode(7, 9, 300, 1, &[]), Err(RecordingError::UnknownFormat(9)));
        assert_eq!(Recording::decode(7, 0, 0, 1, &[]), Err(RecordingError::NoSamplingRate));
        assert_eq!(Recording::decode(7, 0, 300, 0, &[]), Err(RecordingError::NoLeads));
    }

    #[test]
    fn a_lead_count_past_the_names_still_decodes_and_reads_the_first() {
        let recording = Recording::decode(99, 0, 300, 2, &[1, 0, 2, 0]).unwrap();
        assert_eq!(recording.leads()[1].lead, None);
        assert_eq!(recording.analysed().samples, vec![1]);
    }

    #[test]
    fn rhythm_codes_outside_the_table_are_not_a_result() {
        assert_eq!(Rhythm::of(9), Some(Rhythm::NoAfib));
        assert_eq!(Rhythm::of(-1), Some(Rhythm::NoResult));
        assert_eq!(Rhythm::of(77), None);
    }

    #[test]
    fn intervals_are_peak_distances_at_the_readings_rate() {
        let size = |bytes| NonZeroUsize::new(bytes).unwrap();
        let hz = |hz| SamplingHz::new(hz).unwrap();
        assert_eq!(beat_intervals_ms(&[85, 110], size(1), hz(100)), vec![850, 1100]);
        assert_eq!(beat_intervals_ms(&[0x2c, 0x01], size(2), hz(1000)), vec![300]);
    }
}
