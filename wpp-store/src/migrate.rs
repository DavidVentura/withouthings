//! Schema versioning through `PRAGMA user_version`.
//!
//! `user_version` holds the number of migrations a database has run, so it is
//! the index into [`MIGRATIONS`] of the next one to apply. Each runs in its own
//! transaction together with the bump, so a database is never left between two.

use rusqlite::{Connection, Error};

const MIGRATIONS: &[&str] = &[
    include_str!("migrations/0001_initial.sql"),
    include_str!("migrations/0002_ecg_millis.sql"),
    include_str!("migrations/0003_undecoded_splice_at.sql"),
    include_str!("migrations/0004_activity_sleep_level.sql"),
    include_str!("migrations/0005_sample_window_and_context.sql"),
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

        // Running again must find nothing to do rather than replay anything.
        run(&conn).unwrap();
        assert_eq!(version(&conn), MIGRATIONS.len());
    }

    /// A database left partway through the sequence takes up where it stands
    /// and keeps what it holds.
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
}
