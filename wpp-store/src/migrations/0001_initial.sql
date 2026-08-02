-- Values are stored exactly as the watch sent them. Conversion to physical
-- units happens on read, through wpp::units, so a corrected scale factor never
-- costs stored data.

CREATE TABLE device (
    id      INTEGER PRIMARY KEY,
    mac     TEXT NOT NULL UNIQUE,
    name    TEXT,
    model   INTEGER
) STRICT;

CREATE TABLE sample_kind (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    unit    TEXT NOT NULL
) STRICT;

INSERT INTO sample_kind (id, name, unit) VALUES
    (1, 'heart_rate',       'bpm'),
    (2, 'core_temperature', 'millicelsius'),
    (3, 'hrv_sdnn',         'ms'),
    (4, 'hrv_rmssd',        'ms'),
    (5, 'respiratory_rate', 'breaths_per_minute'),
    (6, 'battery',          'percent'),
    (7, 'steps',            'count'),
    (8, 'battery_state',    'battery_state'),
    (9, 'battery_mv',       'millivolts'),
    (10, 'sleep_level',     'sleep_level'),
    (11, 'spo2',            'percent'),
    (12, 'ascent',          'centimetres'),
    (13, 'calories',        'hundredth_kcal'),
    (14, 'distance',        'centimetres'),
    (15, 'tracked_duration','seconds');

CREATE TABLE workout (
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
CREATE TABLE sample (
    device_id   INTEGER NOT NULL REFERENCES device(id),
    -- milliseconds: 1 Hz live pushes collide at one-second resolution
    measured_at INTEGER NOT NULL,
    kind        INTEGER NOT NULL REFERENCES sample_kind(id),
    source      INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    quality     INTEGER,
    PRIMARY KEY (device_id, measured_at, kind, source)
) STRICT, WITHOUT ROWID;

CREATE TABLE ecg (
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

CREATE TABLE sync_state (
    device_id      INTEGER NOT NULL REFERENCES device(id),
    category       INTEGER NOT NULL,
    synced_through INTEGER NOT NULL,
    PRIMARY KEY (device_id, category)
) STRICT, WITHOUT ROWID;

-- Frames that would not decode at all. An object we cannot read is kept inside
-- its frame as Unknown or Malformed and never reaches here; what does is a
-- frame whose objects did not tile its declared length, which is what a lost
-- notification leaves behind.
CREATE TABLE undecoded_frame (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES device(id),
    received_at INTEGER NOT NULL,
    command     INTEGER NOT NULL,
    payload     BLOB NOT NULL
) STRICT;

-- Nothing on the wire links a sample to a workout; only its timestamp does.
CREATE VIEW workout_sample AS
SELECT w.id AS workout_id, s.*
  FROM workout w
  JOIN sample s
    ON s.device_id   = w.device_id
   AND s.measured_at >= w.started_at * 1000
   AND s.measured_at <= COALESCE(w.ended_at * 1000, 9223372036854775807);

-- One window of the per-minute activity stream. A row rather than a set of
-- samples because the counters describe the window, not an instant, and the
-- window is not always a minute: idle stretches arrive compressed into one
-- long one, which is the difference between "no movement" and "no data".
CREATE TABLE activity_minute (
    device_id     INTEGER NOT NULL REFERENCES device(id),
    -- seconds, as the watch dates it
    started_at    INTEGER NOT NULL,
    duration_secs INTEGER NOT NULL,
    steps         INTEGER,
    distance      INTEGER,
    ascent        INTEGER,
    descent       INTEGER,
    calories      INTEGER,
    met           INTEGER,
    walk_level    INTEGER,
    run_level     INTEGER,
    -- The features the official app's classifier runs on. Useless without it,
    -- and gone from the watch's ring buffer within the day.
    reco_v1       INTEGER,
    reco_v2       INTEGER,
    PRIMARY KEY (device_id, started_at)
) STRICT, WITHOUT ROWID;

-- Set boundaries a user marks with the stopwatch. Not protocol data; it is
-- what makes a workout trace legible as sets rather than one long line.
CREATE TABLE marker (
    device_id INTEGER NOT NULL REFERENCES device(id),
    at_ms     INTEGER NOT NULL,
    edge      INTEGER NOT NULL,
    PRIMARY KEY (device_id, at_ms)
) STRICT, WITHOUT ROWID;
