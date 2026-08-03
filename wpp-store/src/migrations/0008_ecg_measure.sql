-- What the watch concluded about a recording, one row per `StoredMeasureData`.
--
-- The watch runs the classifier itself — its firmware logs the result as
-- `[ECG DIAGNOSIS] ECGSW2 WS Diagnosis` — and sends the answer alongside the
-- waveform. Type 11 is the median heart rate it read and 130 the rhythm
-- classification (`ConstantsWs.AFIB_*`).
--
-- A table rather than two columns on `ecg` because the watch may send measures
-- this build does not interpret — the ECG-intervals feature adds PR, QRS, QT
-- and QTc — and a value kept whole can be read later, while a value never
-- stored cannot.
--
-- Raw as sent: the reading is `value * 10^exponent`.
CREATE TABLE ecg_measure (
    ecg_id   INTEGER NOT NULL REFERENCES ecg(id) ON DELETE CASCADE,
    type     INTEGER NOT NULL,
    value    INTEGER NOT NULL,
    exponent INTEGER NOT NULL,
    PRIMARY KEY (ecg_id, type)
) STRICT, WITHOUT ROWID;
