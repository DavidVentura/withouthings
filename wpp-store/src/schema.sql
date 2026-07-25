-- Values are stored exactly as the watch sent them. Conversion to physical
-- units happens on read, through wpp::units, so a corrected scale factor never
-- costs stored data.

CREATE TABLE IF NOT EXISTS device (
    id      INTEGER PRIMARY KEY,
    mac     TEXT NOT NULL UNIQUE,
    name    TEXT,
    model   INTEGER
) STRICT;

CREATE TABLE IF NOT EXISTS sample_kind (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    unit    TEXT NOT NULL
) STRICT;

INSERT OR IGNORE INTO sample_kind (id, name, unit) VALUES
    (1, 'heart_rate',       'bpm'),
    (2, 'core_temperature', 'millicelsius'),
    (3, 'hrv_sdnn',         'ms'),
    (4, 'hrv_rmssd',        'ms'),
    (5, 'respiratory_rate', 'breaths_per_minute'),
    (6, 'battery',          'percent'),
    (7, 'steps',            'count');

CREATE TABLE IF NOT EXISTS workout (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES device(id),
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    subcategory INTEGER NOT NULL,
    paused_secs INTEGER NOT NULL DEFAULT 0,
    UNIQUE (device_id, started_at)
) STRICT;

-- source distinguishes the 1 Hz live stream from the coarser stored series;
-- both can describe the same instant and neither should overwrite the other.
CREATE TABLE IF NOT EXISTS sample (
    device_id   INTEGER NOT NULL REFERENCES device(id),
    -- milliseconds: 1 Hz live pushes collide at one-second resolution
    measured_at INTEGER NOT NULL,
    kind        INTEGER NOT NULL REFERENCES sample_kind(id),
    source      INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    quality     INTEGER,
    PRIMARY KEY (device_id, measured_at, kind, source)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ecg (
    id              INTEGER PRIMARY KEY,
    device_id       INTEGER NOT NULL REFERENCES device(id),
    measured_at     INTEGER NOT NULL,
    signal_type     INTEGER NOT NULL,
    sampling_hz     INTEGER NOT NULL,
    lead_count      INTEGER NOT NULL,
    resolution_bits INTEGER NOT NULL,
    format          INTEGER NOT NULL,
    unit_offset     INTEGER,
    gain            INTEGER,
    qfix            INTEGER,
    declared_bytes  INTEGER NOT NULL,
    samples         BLOB    NOT NULL,
    UNIQUE (device_id, measured_at, signal_type)
) STRICT;

-- Advanced only once the records behind it are committed, so a crash re-reads
-- a window rather than skipping it.
CREATE TABLE IF NOT EXISTS sync_state (
    device_id      INTEGER NOT NULL REFERENCES device(id),
    category       INTEGER NOT NULL,
    synced_through INTEGER NOT NULL,
    PRIMARY KEY (device_id, category)
) STRICT, WITHOUT ROWID;

-- Kept only for frames carrying an object we could not decode; everything else
-- is already represented losslessly above.
CREATE TABLE IF NOT EXISTS undecoded_frame (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES device(id),
    received_at INTEGER NOT NULL,
    command     INTEGER NOT NULL,
    payload     BLOB NOT NULL
) STRICT;

-- Nothing on the wire links a sample to a workout; only its timestamp does.
CREATE VIEW IF NOT EXISTS workout_sample AS
SELECT w.id AS workout_id, s.*
  FROM workout w
  JOIN sample s
    ON s.device_id   = w.device_id
   AND s.measured_at >= w.started_at * 1000
   AND s.measured_at <= COALESCE(w.ended_at * 1000, 9223372036854775807);
