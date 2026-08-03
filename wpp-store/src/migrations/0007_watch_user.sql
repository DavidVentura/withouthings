-- Who the watch says is wearing it, as it answers CMD_TRACKER_USER_GET.
--
-- Its own table rather than columns on `device` because it describes a person
-- and not the hardware, and because the watch takes it back only as a whole
-- record: a row here is exactly what one write sends.
--
-- Values are the wire's: weight in grams, height in centimetres, birth in unix
-- seconds. gender is uninterpreted and kept so a write can preserve it.
CREATE TABLE watch_user (
    device_id  INTEGER PRIMARY KEY REFERENCES device(id),
    user_id    INTEGER NOT NULL,
    weight     INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    gender     INTEGER NOT NULL,
    birth      INTEGER NOT NULL,
    first_name TEXT NOT NULL
) STRICT;
