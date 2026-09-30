-- The episodes behind the watch's "irregular rhythm, take an ECG" alert. The
-- watch keeps each until the phone deletes it, in a store of its own that is
-- only served to a request naming it (stored signal type 5).
--
-- Kept as the watch sends them, like `ecg`: the verdict is a measure (type
-- 139, `afib_class_names` minus one) and is read from `afib_episode_measure`
-- rather than fixed into a column, so a measure this build does not interpret
-- is still there to be read later.
CREATE TABLE afib_episode (
    id              INTEGER PRIMARY KEY,
    device_id       INTEGER NOT NULL REFERENCES device(id),
    measured_at     INTEGER NOT NULL,
    duration_secs   INTEGER NOT NULL,
    sampling_hz     INTEGER NOT NULL,
    sample_bytes    INTEGER NOT NULL,
    resolution_bits INTEGER NOT NULL,
    samples         BLOB    NOT NULL,
    UNIQUE (device_id, measured_at)
) STRICT;

-- Raw as sent: the reading is `value * 10^exponent`.
CREATE TABLE afib_episode_measure (
    episode_id INTEGER NOT NULL REFERENCES afib_episode(id) ON DELETE CASCADE,
    type       INTEGER NOT NULL,
    value      INTEGER NOT NULL,
    exponent   INTEGER NOT NULL,
    PRIMARY KEY (episode_id, type)
) STRICT, WITHOUT ROWID;
