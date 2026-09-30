-- Each PPG AFib record is one background reading the classifier called AFib,
-- not an alert: the watch alerts once a run of them is confirmed, and marks
-- that record by its StoredMeasureMeta attrib (7, "confirmed"; the header flag
-- the store writer at 0x63394 sets from its confirmed out-parameter). Null on
-- rows kept before the attrib was.
ALTER TABLE afib_episode ADD COLUMN attrib INTEGER;
ALTER TABLE afib_episode ADD COLUMN unit_offset INTEGER;
ALTER TABLE afib_episode ADD COLUMN gain INTEGER;
ALTER TABLE afib_episode ADD COLUMN qfix INTEGER;
ALTER TABLE afib_episode_measure RENAME COLUMN episode_id TO parent_id;

-- SpO2 spot checks taken on the watch, stored signal type 4, kept as sent like
-- the episodes; the reading is measure 54.
CREATE TABLE spo2_check (
    id              INTEGER PRIMARY KEY,
    device_id       INTEGER NOT NULL REFERENCES device(id),
    measured_at     INTEGER NOT NULL,
    duration_secs   INTEGER NOT NULL,
    sampling_hz     INTEGER NOT NULL,
    sample_bytes    INTEGER NOT NULL,
    resolution_bits INTEGER NOT NULL,
    samples         BLOB    NOT NULL,
    attrib          INTEGER,
    unit_offset     INTEGER,
    gain            INTEGER,
    qfix            INTEGER,
    UNIQUE (device_id, measured_at)
) STRICT;

-- Raw as sent: the reading is `value * 10^exponent`.
CREATE TABLE spo2_check_measure (
    parent_id INTEGER NOT NULL REFERENCES spo2_check(id) ON DELETE CASCADE,
    type      INTEGER NOT NULL,
    value     INTEGER NOT NULL,
    exponent  INTEGER NOT NULL,
    PRIMARY KEY (parent_id, type)
) STRICT, WITHOUT ROWID;
