-- Where the phone was, not what the watch measured: the ScanWatch has no
-- receiver of its own, so a route only exists because the phone was carried
-- along. Recorded while a workout that travels over ground is running.
--
-- Degrees are stored to seven decimal places, about a centimetre, which is
-- finer than any consumer receiver resolves and keeps the column an integer.
-- Everything derived from these rows -- distance, speed, pace -- is computed
-- on read, so a change to the filtering does not leave old sessions on the
-- old arithmetic.
CREATE TABLE track_point (
    device_id    INTEGER NOT NULL REFERENCES device(id),
    at_ms        INTEGER NOT NULL,
    lat_e7       INTEGER NOT NULL,
    lon_e7       INTEGER NOT NULL,
    -- Null where the fix carried no such field, which a receiver reports
    -- separately from the position and does not always have.
    altitude_cm  INTEGER,
    accuracy_cm  INTEGER,
    speed_mm_s   INTEGER,
    bearing_cdeg INTEGER,
    PRIMARY KEY (device_id, at_ms)
) STRICT, WITHOUT ROWID;

-- Nothing links a fix to a workout but its timestamp: the workout row is
-- written when the watch answers, seconds after the fixes start arriving.
CREATE VIEW workout_track AS
SELECT w.id AS workout_id, t.*
  FROM workout w
  JOIN track_point t
    ON t.device_id = w.device_id
   AND t.at_ms    >= w.started_at * 1000
   AND t.at_ms    <= COALESCE(w.ended_at * 1000, 9223372036854775807);
