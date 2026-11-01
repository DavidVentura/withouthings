//! Each migration runs in its own transaction together with the `user_version`
//! bump, so a database is never left between two if the process dies partway.

use rusqlite::{Connection, Error};

const MIGRATIONS: &[&str] = &[
    include_str!("migrations/0001_initial.sql"),
    include_str!("migrations/0002_ecg_millis.sql"),
    include_str!("migrations/0003_undecoded_splice_at.sql"),
    include_str!("migrations/0004_activity_sleep_level.sql"),
    include_str!("migrations/0005_sample_window_and_context.sql"),
    include_str!("migrations/0006_device_identity.sql"),
    include_str!("migrations/0007_watch_user.sql"),
    include_str!("migrations/0008_ecg_measure.sql"),
    include_str!("migrations/0009_feature.sql"),
    include_str!("migrations/0010_drop_unnamed_features.sql"),
    include_str!("migrations/0011_track_point.sql"),
    include_str!("migrations/0012_afib_episode.sql"),
    include_str!("migrations/0013_spo2_check_and_attrib.sql"),
    include_str!("migrations/0014_note.sql"),
    include_str!("migrations/0015_drop_workout_sample.sql"),
    include_str!("migrations/0016_undecoded_command_optional.sql"),
];

pub fn run(conn: &Connection) -> Result<(), Error> {
    let applied = conn.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))? as usize;
    assert!(
        applied <= MIGRATIONS.len(),
        "database is at schema version {applied}, newer than the {} this build knows",
        MIGRATIONS.len()
    );

    for (index, sql) in MIGRATIONS.iter().enumerate().skip(applied) {
        conn.execute_batch(&format!(
            "BEGIN;
             {sql}
             PRAGMA user_version = {};
             COMMIT;",
            index + 1
        ))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn version(conn: &Connection) -> usize {
        conn.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
            .unwrap() as usize
    }

    #[test]
    fn a_new_database_runs_every_migration() {
        let conn = Connection::open_in_memory().unwrap();
        run(&conn).unwrap();
        assert_eq!(version(&conn), MIGRATIONS.len());

        run(&conn).unwrap();
        assert_eq!(version(&conn), MIGRATIONS.len());
    }

    #[test]
    fn a_partly_migrated_database_carries_its_rows_forward() {
        let conn = Connection::open_in_memory().unwrap();
        for sql in &MIGRATIONS[..2] {
            conn.execute_batch(sql).unwrap();
        }
        conn.pragma_update(None, "user_version", 2).unwrap();
        conn.execute("INSERT INTO device (mac) VALUES ('a4:7e:fa:44:d6:10')", [])
            .unwrap();
        conn.execute(
            "INSERT INTO sample (device_id, measured_at, kind, source, value)
             VALUES (1, 1000, 1, 0, 62)",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        assert_eq!(version(&conn), MIGRATIONS.len());
        let (value, window): (i64, Option<i64>) = conn
            .query_row("SELECT value, window_secs FROM sample", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .unwrap();
        assert_eq!(value, 62);
        assert_eq!(window, None);
    }

    #[test]
    #[should_panic(expected = "newer than")]
    fn a_database_from_a_later_build_is_refused() {
        let conn = Connection::open_in_memory().unwrap();
        run(&conn).unwrap();
        conn.pragma_update(None, "user_version", MIGRATIONS.len() as i64 + 1)
            .unwrap();
        run(&conn).unwrap();
    }

    #[test]
    fn dropping_the_view_and_relaxing_the_command_keeps_every_row() {
        let conn = Connection::open_in_memory().unwrap();
        for sql in &MIGRATIONS[..14] {
            conn.execute_batch(sql).unwrap();
        }
        conn.pragma_update(None, "user_version", 14).unwrap();
        conn.execute_batch(
            "INSERT INTO device (mac) VALUES ('a4:7e:fa:44:d6:10');
             INSERT INTO sample (device_id, measured_at, kind, source, value)
                 VALUES (1, 1000, 1, 0, 62);
             INSERT INTO workout (device_id, started_at, subcategory)
                 VALUES (1, 1, 16);
             INSERT INTO undecoded_frame (id, device_id, received_at, command, payload, splice_at)
                 VALUES (7, 1, 500, 0, x'01', NULL), (9, 1, 600, 321, x'0203', 4);",
        )
        .unwrap();

        run(&conn).unwrap();

        let mut frames = conn
            .prepare("SELECT id, device_id, received_at, command, payload, splice_at FROM undecoded_frame ORDER BY id")
            .unwrap();
        let rows: Vec<(i64, i64, i64, Option<i64>, Vec<u8>, Option<i64>)> = frames
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        assert_eq!(
            rows,
            vec![
                (7, 1, 500, None, vec![1], None),
                (9, 1, 600, Some(321), vec![2, 3], Some(4)),
            ]
        );
        let count = |table: &str| -> i64 {
            conn.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0)).unwrap()
        };
        assert_eq!((count("sample"), count("workout")), (1, 1));
        let views: i64 = conn
            .query_row("SELECT count(*) FROM sqlite_master WHERE name = 'workout_sample'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(views, 0);
        conn.execute(
            "INSERT INTO undecoded_frame (device_id, received_at, command, payload) VALUES (1, 700, NULL, x'04')",
            [],
        )
        .unwrap();
    }
}
