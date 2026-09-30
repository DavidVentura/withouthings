-- Something the wearer wants to read their measurements against: a change of
-- medication, an illness, a move. Not protocol data, and about the person
-- rather than any one watch, so it belongs to no device.
CREATE TABLE note (
    id    INTEGER PRIMARY KEY,
    at_ms INTEGER NOT NULL,
    text  TEXT NOT NULL CHECK (length(text) > 0)
) STRICT;
