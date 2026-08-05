mod migrate;

use std::collections::{BTreeSet, HashMap};

use rusqlite::{params, Connection, OptionalExtension};
use wpp::activity::Minute;
use wpp::client::{Category, DeviceIdentity, Record, Source, UserProfile};
use wpp::signal::Signal;
use wpp::units::UnixTime;

pub use rusqlite::Error;

const CHARGING_KIND: i64 = 8;
const CHARGING_STATE: i64 = 0;

const LEVEL_MAX_GAP_MS: i64 = 10 * 60 * 1000;

/// How far back a window may start and still overlap the span asked for. The
/// overlap test alone leaves `started_at` unbounded below, so SQLite walks
/// every row before the span and the cost grows with the whole history rather
/// than the span. The watch compresses idle stretches into single windows, the
/// longest seen being most of a night.
const LONGEST_WINDOW_SECS: i64 = 2 * 24 * 60 * 60;

pub struct Store {
    conn: Connection,
}

pub struct ActiveWorkout {
    pub id: i64,
    pub started_at: UnixTime,
    pub subcategory: i64,
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
        // The database lives on shared storage, which is FUSE-backed and cannot
        // be relied on to mmap the -shm file WAL normally needs. Taking the
        // exclusive lock first keeps that index in heap memory instead, at the
        // cost of allowing only this one connection for as long as it is open.
        conn.pragma_update(None, "locking_mode", "EXCLUSIVE")?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        migrate::run(&conn)?;
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

    pub fn identity(&self, device_id: i64) -> Result<Option<DeviceIdentity>, Error> {
        self.conn
            .query_row(
                "SELECT name, firmware, bootloader, hardware, rescue
                   FROM device WHERE id = ?1 AND firmware IS NOT NULL",
                params![device_id],
                |r| {
                    Ok(DeviceIdentity {
                        name: r.get(0)?,
                        firmware: r.get(1)?,
                        bootloader: r.get(2)?,
                        hardware: r.get(3)?,
                        rescue: r.get(4)?,
                    })
                },
            )
            .optional()
    }

    pub fn watch_user(&self, device_id: i64) -> Result<Option<UserProfile>, Error> {
        self.conn
            .query_row(
                "SELECT user_id, weight, height, gender, birth, first_name
                   FROM watch_user WHERE device_id = ?1",
                params![device_id],
                |r| {
                    Ok(UserProfile {
                        id: r.get(0)?,
                        weight: r.get(1)?,
                        height: r.get(2)?,
                        gender: r.get(3)?,
                        birth: r.get(4)?,
                        first_name: r.get(5)?,
                    })
                },
            )
            .optional()
    }

    pub fn store(&mut self, device_id: i64, records: &[Record]) -> Result<(), Error> {
        let records = thin_levels(self.newest_levels(device_id, records)?, records);
        let tx = self.conn.transaction()?;
        for record in records {
            match record {
                Record::Sample {
                    measured_at,
                    kind,
                    value,
                    quality,
                    source,
                    window_secs,
                    context,
                } => {
                    tx.execute(
                        "INSERT INTO sample
                             (device_id, measured_at, kind, source, value, quality,
                              window_secs, context)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
                         ON CONFLICT DO NOTHING",
                        params![
                            device_id,
                            measured_at.0,
                            kind.id(),
                            source.id(),
                            value,
                            quality,
                            window_secs,
                            context
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
                    tx.execute(
                        "INSERT INTO workout (device_id, started_at, ended_at, subcategory, paused_secs)
                         VALUES (?1, ?2, ?3, 0, ?4)
                         ON CONFLICT (device_id, started_at)
                         DO UPDATE SET ended_at = ?3, paused_secs = ?4",
                        params![device_id, started_at.0, ended_at.0, paused_secs],
                    )?;
                }
                Record::WorkoutDropped { started_at } => {
                    tx.execute(
                        "DELETE FROM workout WHERE device_id = ?1 AND started_at = ?2",
                        params![device_id, started_at.0],
                    )?;
                }
                Record::Activity(minute) => {
                    tx.execute(
                        "INSERT INTO activity_minute (device_id, started_at, duration_secs,
                             steps, distance, ascent, descent, calories, met,
                             walk_level, run_level, reco_v1, reco_v2, sleep_level)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
                         ON CONFLICT (device_id, started_at) DO UPDATE SET
                             steps       = COALESCE(excluded.steps,       steps),
                             distance    = COALESCE(excluded.distance,    distance),
                             ascent      = COALESCE(excluded.ascent,      ascent),
                             descent     = COALESCE(excluded.descent,     descent),
                             calories    = COALESCE(excluded.calories,    calories),
                             met         = COALESCE(excluded.met,         met),
                             walk_level  = COALESCE(excluded.walk_level,  walk_level),
                             run_level   = COALESCE(excluded.run_level,   run_level),
                             reco_v1     = COALESCE(excluded.reco_v1,     reco_v1),
                             reco_v2     = COALESCE(excluded.reco_v2,     reco_v2),
                             sleep_level = COALESCE(excluded.sleep_level, sleep_level)",
                        params![
                            device_id,
                            minute.at.0,
                            minute.duration_secs,
                            minute.steps,
                            minute.distance,
                            minute.ascent,
                            minute.descent,
                            minute.calories,
                            minute.met,
                            minute.walk_level,
                            minute.run_level,
                            minute.reco_v1,
                            minute.reco_v2,
                            minute.sleep_level,
                        ],
                    )?;
                }
                Record::Ecg(signal) => store_ecg(&tx, device_id, signal)?,
                Record::User(profile) => {
                    tx.execute(
                        "INSERT INTO watch_user
                             (device_id, user_id, weight, height, gender, birth, first_name)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
                         ON CONFLICT (device_id) DO UPDATE SET
                             user_id = excluded.user_id, weight = excluded.weight,
                             height = excluded.height, gender = excluded.gender,
                             birth = excluded.birth, first_name = excluded.first_name",
                        params![
                            device_id,
                            profile.id,
                            profile.weight,
                            profile.height,
                            profile.gender,
                            profile.birth,
                            profile.first_name,
                        ],
                    )?;
                }
                Record::Identity(identity) => {
                    tx.execute(
                        "UPDATE device
                            SET name = ?2, firmware = ?3,
                                bootloader = ?4, hardware = ?5, rescue = ?6
                          WHERE id = ?1",
                        params![
                            device_id,
                            identity.name,
                            identity.firmware,
                            identity.bootloader,
                            identity.hardware,
                            identity.rescue,
                        ],
                    )?;
                }
            }
        }
        tx.commit()
    }

    fn newest_levels(
        &self,
        device_id: i64,
        records: &[Record],
    ) -> Result<HashMap<i64, (i64, i64)>, Error> {
        let kinds: BTreeSet<i64> = records
            .iter()
            .filter_map(|record| match record {
                Record::Sample {
                    kind,
                    source: Source::Live,
                    ..
                } if kind.is_level() => Some(kind.id()),
                _ => None,
            })
            .collect();

        let mut newest = HashMap::new();
        for kind in kinds {
            let last = self
                .conn
                .query_row(
                    "SELECT measured_at, value FROM sample
                      WHERE device_id = ?1 AND kind = ?2 AND source = ?3
                      ORDER BY measured_at DESC LIMIT 1",
                    params![device_id, kind, Source::Live.id()],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()?;
            if let Some(row) = last {
                newest.insert(kind, row);
            }
        }
        Ok(newest)
    }

    pub fn store_undecoded(
        &self,
        device_id: i64,
        received_at: i64,
        command: i64,
        payload: &[u8],
        splice_at: Option<i64>,
    ) -> Result<(), Error> {
        self.conn.execute(
            "INSERT INTO undecoded_frame (device_id, received_at, command, payload, splice_at)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![device_id, received_at, command, payload, splice_at],
        )?;
        Ok(())
    }

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

    /// The largest value in each window between consecutive `edges_ms`, so
    /// `edges_ms` holds one more entry than the result. Reads the samples
    /// themselves rather than [`Store::series`], whose bucketing may drop the
    /// peak a window is being asked for.
    pub fn windowed_max(
        &self,
        device_id: i64,
        kind: i64,
        edges_ms: &[i64],
    ) -> Result<Vec<Option<i64>>, Error> {
        let (Some(&first), Some(&last)) = (edges_ms.first(), edges_ms.last()) else {
            return Ok(Vec::new());
        };
        let mut stmt = self.conn.prepare(
            "SELECT measured_at, value FROM sample
              WHERE device_id = ?1 AND kind = ?4
                AND measured_at >= ?2 AND measured_at < ?3
              ORDER BY measured_at",
        )?;
        let rows = stmt.query_map(params![device_id, first, last, kind], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))
        })?;

        let mut found = vec![None; edges_ms.len() - 1];
        let mut window = 0;
        for row in rows {
            let (at, value) = row?;
            while at >= edges_ms[window + 1] {
                window += 1;
            }
            found[window] = Some(found[window].map_or(value, |seen: i64| seen.max(value)));
        }
        Ok(found)
    }

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

    /// Must match [`Store::activity_minutes`]'s windowing exactly: disagreeing
    /// reports a night as having data and then draws it empty, or skips over
    /// one while it still holds some.
    pub fn has_staging(&self, device_id: i64, from_secs: i64, to_secs: i64) -> Result<bool, Error> {
        self.conn.query_row(
            "SELECT EXISTS (SELECT 1 FROM activity_minute
                             WHERE device_id = ?1 AND started_at <= ?3
                               AND started_at >= ?2 - ?4
                               AND started_at + duration_secs >= ?2
                               AND sleep_level IS NOT NULL)",
            params![device_id, from_secs, to_secs, LONGEST_WINDOW_SECS],
            |r| r.get(0),
        )
    }

    pub fn activity_minutes(
        &self,
        device_id: i64,
        from_secs: i64,
        to_secs: i64,
    ) -> Result<Vec<Minute>, Error> {
        let mut stmt = self.conn.prepare(
            "SELECT started_at, duration_secs, steps, distance, ascent, descent,
                    calories, met, walk_level, run_level, reco_v1, reco_v2,
                    sleep_level
               FROM activity_minute
              WHERE device_id = ?1 AND started_at <= ?3
                AND started_at >= ?2 - ?4
                AND started_at + duration_secs >= ?2
              ORDER BY started_at",
        )?;
        let rows = stmt.query_map(params![device_id, from_secs, to_secs, LONGEST_WINDOW_SECS], |r| {
            Ok(Minute {
                at: UnixTime(r.get(0)?),
                duration_secs: r.get(1)?,
                steps: r.get(2)?,
                distance: r.get(3)?,
                ascent: r.get(4)?,
                descent: r.get(5)?,
                calories: r.get(6)?,
                met: r.get(7)?,
                walk_level: r.get(8)?,
                run_level: r.get(9)?,
                reco_v1: r.get(10)?,
                reco_v2: r.get(11)?,
                sleep_level: r.get(12)?,
            })
        })?;
        rows.collect()
    }

    pub fn active_workout(&self, device_id: i64) -> Result<Option<ActiveWorkout>, Error> {
        self.conn
            .query_row(
                "SELECT id, started_at, subcategory FROM workout
                  WHERE device_id = ?1 AND ended_at IS NULL
                  ORDER BY started_at DESC LIMIT 1",
                params![device_id],
                |r| {
                    Ok(ActiveWorkout {
                        id: r.get(0)?,
                        started_at: UnixTime(r.get(1)?),
                        subcategory: r.get(2)?,
                    })
                },
            )
            .optional()
    }

    pub fn delete_workout(&self, device_id: i64, id: i64) -> Result<(), Error> {
        let tx = self.conn.unchecked_transaction()?;
        tx.execute(
            "DELETE FROM marker
              WHERE device_id = ?1
                AND at_ms >= (SELECT started_at * 1000 FROM workout
                               WHERE id = ?2 AND device_id = ?1)
                AND at_ms <= (SELECT COALESCE(ended_at, started_at) * 1000 FROM workout
                               WHERE id = ?2 AND device_id = ?1)",
            params![device_id, id],
        )?;
        tx.execute(
            "DELETE FROM workout WHERE device_id = ?1 AND id = ?2",
            params![device_id, id],
        )?;
        tx.commit()?;
        Ok(())
    }

    pub fn mark_set(&self, device_id: i64, at_ms: i64, edge: i64) -> Result<(), Error> {
        self.conn.execute(
            "INSERT INTO marker (device_id, at_ms, edge) VALUES (?1, ?2, ?3)
             ON CONFLICT DO NOTHING",
            params![device_id, at_ms, edge],
        )?;
        Ok(())
    }

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
        }
        if let Some(start) = open {
            periods.push((start, None));
        }
        Ok(periods)
    }

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

    pub fn ecg_measures(&self, ecg_id: i64) -> Result<Vec<(i64, i64, i64)>, Error> {
        let mut stmt = self
            .conn
            .prepare("SELECT type, value, exponent FROM ecg_measure WHERE ecg_id = ?1")?;
        let rows = stmt.query_map(params![ecg_id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
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

fn thin_levels(mut newest: HashMap<i64, (i64, i64)>, records: &[Record]) -> Vec<&Record> {
    let mut kept = Vec::with_capacity(records.len());
    for record in records {
        let Record::Sample {
            measured_at,
            kind,
            value,
            source: Source::Live,
            ..
        } = record
        else {
            kept.push(record);
            continue;
        };
        if !kind.is_level() {
            kept.push(record);
            continue;
        }
        if let Some((last_at, last_value)) = newest.get(&kind.id()) {
            if value == last_value && measured_at.0 - last_at < LEVEL_MAX_GAP_MS {
                continue;
            }
        }
        newest.insert(kind.id(), (measured_at.0, *value));
        kept.push(record);
    }
    kept
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

    let ecg_id: i64 = tx.query_row(
        "SELECT id FROM ecg WHERE device_id = ?1 AND measured_at = ?2 AND signal_type = ?3",
        params![device_id, measured_at, signal.meta.r#type],
        |r| r.get(0),
    )?;
    for measure in &signal.measures {
        tx.execute(
            "INSERT INTO ecg_measure (ecg_id, type, value, exponent)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT DO NOTHING",
            params![ecg_id, measure.r#type, measure.value, measure.exponent],
        )?;
    }
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
            window_secs: Some(60),
            context: None,
        }
    }

    #[test]
    fn an_activity_window_survives_the_round_trip_and_re_storing_it() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let minute = Minute {
            duration_secs: 60,
            steps: Some(94),
            distance: Some(7180),
            calories: Some(245),
            met: Some(290),
            walk_level: Some(2),
            ..Minute::opened(UnixTime(5000))
        };
        let batch = vec![
            Record::Activity(minute),
            Record::Activity(Minute {
                duration_secs: 960,
                ..Minute::opened(UnixTime(5060))
            }),
        ];
        store.store(device, &batch).unwrap();
        store.store(device, &batch).unwrap();
        let read = store.activity_minutes(device, 0, 10_000).unwrap();
        assert_eq!(read.len(), 2);
        assert_eq!(read[0], minute);
    }

    #[test]
    fn a_window_straddling_the_start_of_the_span_is_read() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        store
            .store(
                device,
                &[Record::Activity(Minute {
                    duration_secs: 900,
                    steps: Some(30),
                    ..Minute::opened(UnixTime(1000))
                })],
            )
            .unwrap();
        assert_eq!(store.activity_minutes(device, 1500, 5000).unwrap().len(), 1);
        assert_eq!(store.activity_minutes(device, 2000, 5000).unwrap().len(), 0);
    }

    #[test]
    fn each_window_reports_its_own_peak() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        store
            .store(
                device,
                &[
                    sample(1000, 62, Source::Stored),
                    sample(1500, 88, Source::Stored),
                    sample(2500, 71, Source::Stored),
                    sample(4000, 90, Source::Stored),
                ],
            )
            .unwrap();
        let kind = SampleKind::HeartRate.id();
        let found = store
            .windowed_max(device, kind, &[1000, 2000, 3000, 4000])
            .unwrap();
        assert_eq!(found, vec![Some(88), Some(71), None]);
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
    fn a_dropped_workout_takes_the_row_its_start_created_with_it() {
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
                &[Record::WorkoutDropped {
                    started_at: UnixTime(1784998983),
                }],
            )
            .unwrap();
        assert_eq!(store.count("workout").unwrap(), 0);
    }

    #[test]
    fn deleting_a_workout_keeps_its_samples_and_drops_its_marks() {
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
                    sample(1_050_000, 120, Source::Live),
                ],
            )
            .unwrap();
        store.mark_set(device, 1_020_000, 0).unwrap();
        store.mark_set(device, 1_040_000, 1).unwrap();
        store.mark_set(device, 1_200_000, 0).unwrap();

        let id = store.workouts(device, 10).unwrap()[0].0;
        store.delete_workout(device, id).unwrap();

        assert_eq!(store.count("workout").unwrap(), 0);
        assert_eq!(store.count("sample").unwrap(), 1);
        assert_eq!(store.count("marker").unwrap(), 1);
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
            window_secs: None,
            context: None,
        };
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

        assert_eq!(
            store.charge_periods(device, 2_500, 4_500).unwrap(),
            vec![(2_500, Some(4_000))]
        );
    }

    fn battery(at: i64, value: i64) -> Record {
        Record::Sample {
            measured_at: UnixMillis(at),
            kind: SampleKind::BatteryPercent,
            value,
            quality: None,
            source: Source::Live,
            window_secs: None,
            context: None,
        }
    }

    #[test]
    fn a_level_that_is_not_moving_is_stored_every_ten_minutes() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let batch: Vec<Record> = (0..90)
            .map(|i| battery(i * 20_000, if i < 60 { 84 } else { 83 }))
            .collect();
        store.store(device, &batch).unwrap();

        let kept: Vec<(i64, i64)> = store
            .series(device, SampleKind::BatteryPercent.id(), 0, 1_800_000, 1000)
            .unwrap()
            .iter()
            .map(|(at, value, _)| (*at, *value))
            .collect();
        assert_eq!(
            kept,
            vec![(0, 84), (600_000, 84), (1_200_000, 83)],
            "the first reading, a restatement ten minutes on, then the change"
        );
    }

    #[test]
    fn a_repeat_arriving_in_a_later_batch_is_still_dropped() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        for i in 0..40 {
            store.store(device, &[battery(i * 20_000, 84)]).unwrap();
        }
        assert_eq!(
            store.count("sample").unwrap(),
            2,
            "the first reading and the ten-minute restatement"
        );
    }

    #[test]
    fn instants_and_stored_series_pass_through_untouched() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        store
            .store(
                device,
                &[
                    sample(1_000, 62, Source::Live),
                    sample(21_000, 62, Source::Live),
                    Record::Sample {
                        measured_at: UnixMillis(1_000),
                        kind: SampleKind::BatteryPercent,
                        value: 84,
                        quality: None,
                        source: Source::Stored,
                        window_secs: None,
                        context: None,
                    },
                    Record::Sample {
                        measured_at: UnixMillis(21_000),
                        kind: SampleKind::BatteryPercent,
                        value: 84,
                        quality: None,
                        source: Source::Stored,
                        window_secs: None,
                        context: None,
                    },
                ],
            )
            .unwrap();
        assert_eq!(store.count("sample").unwrap(), 4);
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
