//! SQLite persistence for [`wpp::Record`].
//!
//! The write path is idempotent: re-syncing a window that was already stored
//! is a no-op, which the paged history walk guarantees will happen.

use rusqlite::{params, Connection, OptionalExtension};
use wpp::client::{Category, Record};
use wpp::signal::Signal;
use wpp::units::UnixTime;

pub use rusqlite::Error;

/// `sample_kind.battery_state`, and the value meaning a charger is attached.
const CHARGING_KIND: i64 = 8;
const CHARGING_STATE: i64 = 0;

pub struct Store {
    conn: Connection,
}

impl Store {
    pub fn open(path: &str) -> Result<Store, Error> {
        let conn = Connection::open(path)?;
        Store::prepare(conn)
    }

    pub fn open_in_memory() -> Result<Store, Error> {
        Store::prepare(Connection::open_in_memory()?)
    }

    fn prepare(conn: Connection) -> Result<Store, Error> {
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.execute_batch(include_str!("schema.sql"))?;
        Ok(Store { conn })
    }

    pub fn device(&self, mac: &str) -> Result<i64, Error> {
        self.conn.execute(
            "INSERT OR IGNORE INTO device (mac) VALUES (?1)",
            params![mac],
        )?;
        self.conn
            .query_row("SELECT id FROM device WHERE mac = ?1", params![mac], |r| {
                r.get(0)
            })
    }

    /// Persist a batch atomically. Only after this returns may the caller tell
    /// the client the data is durable, and only then may anything be deleted
    /// from the watch.
    pub fn store(&mut self, device_id: i64, records: &[Record]) -> Result<(), Error> {
        let tx = self.conn.transaction()?;
        for record in records {
            match record {
                Record::Sample {
                    measured_at,
                    kind,
                    value,
                    quality,
                    source,
                } => {
                    // Every sample is an observation at an instant, so a
                    // repeat of one is a duplicate.
                    tx.execute(
                        "INSERT INTO sample (device_id, measured_at, kind, source, value, quality)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6)
                         ON CONFLICT DO NOTHING",
                        params![
                            device_id,
                            measured_at.0,
                            kind.id(),
                            source.id(),
                            value,
                            quality
                        ],
                    )?;
                }
                Record::WorkoutStarted {
                    started_at,
                    subcategory,
                } => {
                    tx.execute(
                        "INSERT INTO workout (device_id, started_at, subcategory)
                         VALUES (?1, ?2, ?3)
                         ON CONFLICT DO NOTHING",
                        params![device_id, started_at.0, subcategory],
                    )?;
                }
                Record::WorkoutEnded {
                    started_at,
                    ended_at,
                    paused_secs,
                } => {
                    // The stop message may arrive without a start having been
                    // seen, so insert rather than assume the row exists.
                    tx.execute(
                        "INSERT INTO workout (device_id, started_at, ended_at, subcategory, paused_secs)
                         VALUES (?1, ?2, ?3, 0, ?4)
                         ON CONFLICT (device_id, started_at)
                         DO UPDATE SET ended_at = ?3, paused_secs = ?4",
                        params![device_id, started_at.0, ended_at.0, paused_secs],
                    )?;
                }
                Record::Ecg(signal) => store_ecg(&tx, device_id, signal)?,
            }
        }
        tx.commit()
    }

    /// Watermarks to resume from, one per category the watch serves.
    pub fn watermarks(
        &self,
        device_id: i64,
        categories: &[Category],
    ) -> Result<Vec<(Category, UnixTime)>, Error> {
        categories
            .iter()
            .map(|category| {
                let at: Option<i64> = self
                    .conn
                    .query_row(
                        "SELECT synced_through FROM sync_state
                          WHERE device_id = ?1 AND category = ?2",
                        params![device_id, category.0],
                        |r| r.get(0),
                    )
                    .optional()?;
                Ok((*category, UnixTime(at.unwrap_or(0))))
            })
            .collect()
    }

    pub fn set_watermark(
        &self,
        device_id: i64,
        category: Category,
        through: UnixTime,
    ) -> Result<(), Error> {
        self.conn.execute(
            "INSERT INTO sync_state (device_id, category, synced_through)
             VALUES (?1, ?2, ?3)
             ON CONFLICT (device_id, category)
             DO UPDATE SET synced_through = max(synced_through, ?3)",
            params![device_id, category.0, through.0],
        )?;
        Ok(())
    }

    /// Most recent value of a kind, as (measured_at_ms, value).
    pub fn latest(&self, device_id: i64, kind: i64) -> Result<Option<(i64, i64)>, Error> {
        self.conn
            .query_row(
                "SELECT measured_at, value FROM sample
                  WHERE device_id = ?1 AND kind = ?2
                  ORDER BY measured_at DESC LIMIT 1",
                params![device_id, kind],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
    }

    /// One kind of sample over a window, reduced to at most `max_points`.
    ///
    /// Reduction keeps the minimum and maximum of each bucket rather than an
    /// average: averaging flattens the recovery dips between sets, which is
    /// what the trace is being read for.
    pub fn series(
        &self,
        device_id: i64,
        kind: i64,
        from_ms: i64,
        to_ms: i64,
        max_points: u32,
    ) -> Result<Vec<(i64, i64, i64)>, Error> {
        let total: i64 = self.conn.query_row(
            "SELECT count(*) FROM sample
              WHERE device_id = ?1 AND kind = ?4 AND measured_at BETWEEN ?2 AND ?3",
            params![device_id, from_ms, to_ms, kind],
            |r| r.get(0),
        )?;

        let max_points = max_points.max(2) as i64;
        if total <= max_points {
            let mut stmt = self.conn.prepare(
                "SELECT measured_at, value, source FROM sample
                  WHERE device_id = ?1 AND kind = ?4 AND measured_at BETWEEN ?2 AND ?3
                  ORDER BY measured_at",
            )?;
            let rows = stmt.query_map(params![device_id, from_ms, to_ms, kind], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?))
            })?;
            let inside = rows.collect::<Result<Vec<_>, _>>()?;
            return self.with_neighbours(device_id, kind, from_ms, to_ms, inside);
        }

        let buckets = max_points / 2;
        let width = ((to_ms - from_ms) / buckets).max(1);
        let mut stmt = self.conn.prepare(
            "SELECT measured_at, value, source FROM sample
              WHERE device_id = ?1 AND kind = ?5 AND measured_at BETWEEN ?2 AND ?3
                AND (measured_at, value) IN (
                    SELECT measured_at, min(value) FROM sample
                     WHERE device_id = ?1 AND kind = ?5 AND measured_at BETWEEN ?2 AND ?3
                     GROUP BY (measured_at - ?2) / ?4
                    UNION ALL
                    SELECT measured_at, max(value) FROM sample
                     WHERE device_id = ?1 AND kind = ?5 AND measured_at BETWEEN ?2 AND ?3
                     GROUP BY (measured_at - ?2) / ?4
                )
              ORDER BY measured_at",
        )?;
        let rows = stmt.query_map(params![device_id, from_ms, to_ms, width, kind], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })?;
        let inside = rows.collect::<Result<Vec<_>, _>>()?;
        self.with_neighbours(device_id, kind, from_ms, to_ms, inside)
    }

    /// The same series with the nearest sample on each side of the window.
    ///
    /// Without them the trace starts and stops at the edges of the plot, so
    /// panning makes the ends jump as points cross the boundary. The extra
    /// points are drawn off-screen and only exist to carry the line out there.
    fn with_neighbours(
        &self,
        device_id: i64,
        kind: i64,
        from_ms: i64,
        to_ms: i64,
        inside: Vec<(i64, i64, i64)>,
    ) -> Result<Vec<(i64, i64, i64)>, Error> {
        let before = self
            .conn
            .query_row(
                "SELECT measured_at, value, source FROM sample
                  WHERE device_id = ?1 AND kind = ?2 AND measured_at < ?3
                  ORDER BY measured_at DESC LIMIT 1",
                params![device_id, kind, from_ms],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        let after = self
            .conn
            .query_row(
                "SELECT measured_at, value, source FROM sample
                  WHERE device_id = ?1 AND kind = ?2 AND measured_at > ?3
                  ORDER BY measured_at ASC LIMIT 1",
                params![device_id, kind, to_ms],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;

        let mut out = Vec::with_capacity(inside.len() + 2);
        out.extend(before);
        out.extend(inside);
        out.extend(after);
        Ok(out)
    }

    /// Oldest and newest sample of a kind, for framing an initial window.
    pub fn extent(&self, device_id: i64, kind: i64) -> Result<Option<(i64, i64)>, Error> {
        self.conn
            .query_row(
                "SELECT min(measured_at), max(measured_at) FROM sample
                  WHERE device_id = ?1 AND kind = ?2",
                params![device_id, kind],
                |r| Ok((r.get::<_, Option<i64>>(0)?, r.get::<_, Option<i64>>(1)?)),
            )
            .map(|(lo, hi)| match (lo, hi) {
                (Some(lo), Some(hi)) => Some((lo, hi)),
                _ => None,
            })
    }

    /// Workouts newest first, as (id, started_at, ended_at, subcategory).
    pub fn workouts(
        &self,
        device_id: i64,
        limit: u32,
    ) -> Result<Vec<(i64, i64, Option<i64>, i64)>, Error> {
        let mut stmt = self.conn.prepare(
            "SELECT id, started_at, ended_at, subcategory FROM workout
              WHERE device_id = ?1 ORDER BY started_at DESC LIMIT ?2",
        )?;
        let rows = stmt.query_map(params![device_id, limit], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
        })?;
        rows.collect()
    }

    /// The workout still running, if any.
    pub fn active_workout(&self, device_id: i64) -> Result<Option<(i64, i64, i64)>, Error> {
        self.conn
            .query_row(
                "SELECT id, started_at, subcategory FROM workout
                  WHERE device_id = ?1 AND ended_at IS NULL
                  ORDER BY started_at DESC LIMIT 1",
                params![device_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
    }

    pub fn mark_set(&self, device_id: i64, at_ms: i64, edge: i64) -> Result<(), Error> {
        self.conn.execute(
            "INSERT INTO marker (device_id, at_ms, edge) VALUES (?1, ?2, ?3)
             ON CONFLICT DO NOTHING",
            params![device_id, at_ms, edge],
        )?;
        Ok(())
    }

    /// Set boundaries inside a window, as (at_ms, edge).
    pub fn markers(
        &self,
        device_id: i64,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<Vec<(i64, i64)>, Error> {
        let mut stmt = self.conn.prepare(
            "SELECT at_ms, edge FROM marker
              WHERE device_id = ?1 AND at_ms BETWEEN ?2 AND ?3 ORDER BY at_ms",
        )?;
        let rows = stmt.query_map(params![device_id, from_ms, to_ms], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })?;
        rows.collect()
    }

    /// One recording: metadata plus its interleaved samples.
    /// When the watch was on a charger, as (start, end) with an open end for a
    /// charge still running.
    ///
    /// Derived from the `battery_state` series rather than stored separately:
    /// the watch reports a state, and a charge is the stretch over which that
    /// state was CHARGING. The sample before the window is included so a charge
    /// already under way is not missed.
    pub fn charge_periods(
        &self,
        device_id: i64,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<Vec<(i64, Option<i64>)>, Error> {
        let mut stmt = self.conn.prepare(
            "SELECT measured_at, value FROM sample
              WHERE device_id = ?1 AND kind = ?2
                AND measured_at <= ?4
                AND measured_at >= COALESCE(
                    (SELECT max(measured_at) FROM sample
                      WHERE device_id = ?1 AND kind = ?2 AND measured_at < ?3), ?3)
              ORDER BY measured_at",
        )?;
        let rows = stmt.query_map(params![device_id, CHARGING_KIND, from_ms, to_ms], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))
        })?;

        let mut periods: Vec<(i64, Option<i64>)> = Vec::new();
        let mut open: Option<i64> = None;
        let mut last_at = from_ms;
        for row in rows {
            let (at, state) = row?;
            match (open, state == CHARGING_STATE) {
                (None, true) => open = Some(at.max(from_ms)),
                (Some(start), false) => {
                    periods.push((start, Some(at)));
                    open = None;
                }
                _ => {}
            }
            last_at = at;
        }
        // Still charging at the last reading: leave it open rather than
        // inventing an end the data does not support.
        if let Some(start) = open {
            periods.push((
                start,
                if last_at >= to_ms {
                    None
                } else {
                    Some(last_at)
                },
            ));
        }
        Ok(periods)
    }

    /// Recordings the app can draw, newest first.
    ///
    /// Only signal types with a known lead layout: the same transfer path also
    /// carries other stored measurements, which are not waveforms.
    pub fn ecgs(&self, device_id: i64) -> Result<Vec<(i64, i64, i64, i64, i64)>, Error> {
        let mut stmt = self.conn.prepare(
            "SELECT id, measured_at, signal_type, sampling_hz, length(samples)
               FROM ecg
              WHERE device_id = ?1 AND signal_type IN (1, 6, 7, 8, 13)
              ORDER BY measured_at DESC",
        )?;
        let rows = stmt.query_map(params![device_id], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
        })?;
        rows.collect()
    }

    pub fn ecg(&self, id: i64) -> Result<Option<(i64, i64, i64, i64, Vec<u8>)>, Error> {
        self.conn
            .query_row(
                "SELECT measured_at, signal_type, sampling_hz, lead_count, samples
                   FROM ecg WHERE id = ?1",
                params![id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .optional()
    }

    pub fn count(&self, table: &str) -> Result<i64, Error> {
        self.conn
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
    }

    pub fn connection(&self) -> &Connection {
        &self.conn
    }
}

fn store_ecg(tx: &rusqlite::Transaction<'_>, device_id: i64, signal: &Signal) -> Result<(), Error> {
    // Protocol timestamps are Unix seconds; every table here is milliseconds.
    let measured_at = signal
        .measure
        .as_ref()
        .map(|m| m.time as i64 * 1000)
        .unwrap_or(0);
    let (offset, gain, qfix) = match &signal.units {
        Some(u) => (Some(u.offset), Some(u.gain), Some(u.qfix)),
        None => (None, None, None),
    };
    tx.execute(
        "INSERT INTO ecg (device_id, measured_at, signal_type, sampling_hz, lead_count,
                          resolution_bits, format, unit_offset, gain, qfix,
                          declared_bytes, samples)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
         ON CONFLICT DO NOTHING",
        params![
            device_id,
            measured_at,
            signal.meta.r#type,
            signal.meta.sampling_freq,
            signal.lead_count() as i64,
            signal.meta.resolution,
            signal.meta.format,
            offset,
            gain,
            qfix,
            signal.declared_size() as i64,
            signal.data,
        ],
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use wpp::client::{SampleKind, Source};
    use wpp::units::UnixMillis;

    fn sample(at: i64, value: i64, source: Source) -> Record {
        Record::Sample {
            measured_at: UnixMillis(at),
            kind: SampleKind::HeartRate,
            value,
            quality: Some(4),
            source,
        }
    }

    #[test]
    fn re_storing_the_same_window_does_not_duplicate() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let batch = vec![
            sample(1000, 62, Source::Stored),
            sample(1060, 65, Source::Stored),
        ];
        store.store(device, &batch).unwrap();
        store.store(device, &batch).unwrap();
        assert_eq!(store.count("sample").unwrap(), 2);
    }

    /// The same instant seen live and in the stored series are two different
    /// observations, not a duplicate.
    #[test]
    fn live_and_stored_samples_coexist() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        store
            .store(
                device,
                &[
                    sample(1000, 62, Source::Stored),
                    sample(1000, 63, Source::Live),
                ],
            )
            .unwrap();
        assert_eq!(store.count("sample").unwrap(), 2);
    }

    #[test]
    fn a_workout_stop_completes_the_row_its_start_created() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        store
            .store(
                device,
                &[Record::WorkoutStarted {
                    started_at: UnixTime(1784998983),
                    subcategory: 16,
                }],
            )
            .unwrap();
        store
            .store(
                device,
                &[Record::WorkoutEnded {
                    started_at: UnixTime(1784998983),
                    ended_at: UnixTime(1784999069),
                    paused_secs: 0,
                }],
            )
            .unwrap();
        let (ended, subcategory): (i64, i64) = store
            .connection()
            .query_row("SELECT ended_at, subcategory FROM workout", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .unwrap();
        assert_eq!(store.count("workout").unwrap(), 1);
        assert_eq!(ended, 1784999069);
        assert_eq!(subcategory, 16, "the stop must not clobber the category");
    }

    #[test]
    fn samples_inside_a_workout_are_found_by_time_alone() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        store
            .store(
                device,
                &[
                    Record::WorkoutStarted {
                        started_at: UnixTime(1000),
                        subcategory: 16,
                    },
                    Record::WorkoutEnded {
                        started_at: UnixTime(1000),
                        ended_at: UnixTime(1100),
                        paused_secs: 0,
                    },
                    // milliseconds, against a workout spanning 1000..1100 s
                    sample(999_000, 60, Source::Live),
                    sample(1_050_000, 120, Source::Live),
                    sample(1_200_000, 61, Source::Live),
                ],
            )
            .unwrap();
        let inside: i64 = store
            .connection()
            .query_row("SELECT count(*) FROM workout_sample", [], |r| r.get(0))
            .unwrap();
        assert_eq!(inside, 1, "only the sample within the workout window");
    }

    /// The points on either side are off-screen; without them the trace stops
    /// at the edge of the plot and panning makes the ends jump.
    #[test]
    fn a_series_reaches_one_point_past_each_end_of_the_window() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        store
            .store(
                device,
                &[
                    sample(1_000, 60, Source::Stored),
                    sample(2_000, 61, Source::Stored),
                    sample(3_000, 62, Source::Stored),
                    sample(4_000, 63, Source::Stored),
                    sample(5_000, 64, Source::Stored),
                ],
            )
            .unwrap();

        let kind = wpp::client::SampleKind::HeartRate.id();
        let series = store.series(device, kind, 2_500, 3_500, 100).unwrap();
        let times: Vec<i64> = series.iter().map(|(t, _, _)| *t).collect();
        assert_eq!(times, vec![2_000, 3_000, 4_000], "one either side of 3000");

        // At the ends of the data there is nothing to reach for, and asking
        // must not invent a point or fail.
        let head = store.series(device, kind, 0, 1_500, 100).unwrap();
        assert_eq!(
            head.iter().map(|(t, _, _)| *t).collect::<Vec<_>>(),
            vec![1_000, 2_000]
        );
        let tail = store.series(device, kind, 4_500, 9_000, 100).unwrap();
        assert_eq!(
            tail.iter().map(|(t, _, _)| *t).collect::<Vec<_>>(),
            vec![4_000, 5_000]
        );
    }

    /// A charge is a stretch of the state series, not an event, so it has to
    /// survive a window that starts or ends in the middle of one.
    #[test]
    fn charging_periods_come_out_of_the_state_series() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let state = |at: i64, v: i64| Record::Sample {
            measured_at: wpp::units::UnixMillis(at),
            kind: wpp::client::SampleKind::BatteryState,
            value: v,
            quality: None,
            source: Source::Live,
        };
        // ok, ok, charging, charging, ok, then charging to the end
        store
            .store(
                device,
                &[
                    state(1_000, 2),
                    state(2_000, 0),
                    state(3_000, 0),
                    state(4_000, 2),
                    state(5_000, 0),
                    state(6_000, 0),
                ],
            )
            .unwrap();

        assert_eq!(
            store.charge_periods(device, 0, 6_000).unwrap(),
            vec![(2_000, Some(4_000)), (5_000, None)],
            "the last charge is still running, so it has no end"
        );

        // A window opening mid-charge still shows one, from its own start.
        assert_eq!(
            store.charge_periods(device, 2_500, 4_500).unwrap(),
            vec![(2_500, Some(4_000))]
        );
    }

    #[test]
    fn foreign_keys_are_enforced() {
        let mut store = Store::open_in_memory().unwrap();
        let err = store.store(999, &[sample(1000, 62, Source::Stored)]);
        assert!(err.is_err(), "an unknown device must be rejected");
    }

    #[test]
    fn a_watermark_never_moves_backwards() {
        let store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        store
            .set_watermark(device, Category(8), UnixTime(5000))
            .unwrap();
        store
            .set_watermark(device, Category(8), UnixTime(4000))
            .unwrap();
        assert_eq!(
            store.watermarks(device, &[Category(8)]).unwrap(),
            vec![(Category(8), UnixTime(5000))]
        );
    }
}
