-- What the watch was last told to enable, as CMD_FEATURE_TAGS_SET_DEPRECATED_V2
-- carries it.
--
-- There is no read side for that command and the write carries the whole set,
-- so nothing can be recovered from the watch: this table is the only record of
-- what it is running, and an id absent here is off.
--
-- starts_at/expires_at are the wire's own encoding, unix seconds, both zero for
-- a feature left on permanently. A feature that holds a sensor on gets a window
-- instead, and the watch expires it without being told to.
CREATE TABLE feature (
    device_id  INTEGER NOT NULL REFERENCES device(id),
    feature_id INTEGER NOT NULL,
    starts_at  INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (device_id, feature_id),
    CHECK ((starts_at = 0) = (expires_at = 0))
) STRICT;

-- An empty set is a legitimate choice, so emptiness cannot be what tells a
-- device that has never been given defaults from one whose owner switched
-- everything off.
ALTER TABLE device ADD COLUMN features_seeded INTEGER NOT NULL DEFAULT 0;
