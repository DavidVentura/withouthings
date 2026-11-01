-- A frame too broken to declare a command has none; 0 was standing in for it.
-- SQLite cannot relax NOT NULL in place, and no other table refers to this one.
CREATE TABLE undecoded_frame_new (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES device(id),
    received_at INTEGER NOT NULL,
    command     INTEGER,
    payload     BLOB NOT NULL,
    splice_at   INTEGER
) STRICT;

INSERT INTO undecoded_frame_new (id, device_id, received_at, command, payload, splice_at)
SELECT id, device_id, received_at, NULLIF(command, 0), payload, splice_at
  FROM undecoded_frame;

DROP TABLE undecoded_frame;
ALTER TABLE undecoded_frame_new RENAME TO undecoded_frame;
