use std::ops::Range;

use wpp::activity::{self, Session, SleepLevel};
use wpp::client::{SampleKind, Source, UserProfile, BATTERY_STATE_CHARGING};
use wpp::energy::{self, Beat, Reading, Wearer};
use wpp::heart::{self, AtRest, Effort};
use wpp::rhythm::{self, EcgReading, StoredPulse, WATCH_RATE_WITHIN_MS};
use wpp::signal::{PpgAfibRule, WatchVerdict, STORED_MEASURE_ATTRIB_CONFIRMED};
use wpp::sleep::{self, Night, Score};
use wpp::sport::{self, SportId};
use wpp::track;
use wpp::units::{
    Bpm, Duration, Edges, Hundredths, Kilocalories, Met, Metres, Millis, UnixMillis, UnixTime,
};

use crate::{
    AfibReadingRow, DeviceId, EcgId, EcgSummaryRow, Error, SeriesPoint, Store, StoredEcg,
    UnreadableEcg, UnreadableReading, WorkoutId, WorkoutRow,
};

const DAY: Duration = Duration::from_secs(24 * 60 * 60);

/// A heart still coming down from a session is not the waking rate either.
const RECOVERY: Duration = Duration::from_secs(10 * 60);

/// The watch asks for an ECG with the alert; one taken within this is taken as
/// the answer to it.
const ECG_AFTER_ALERT: Duration = Duration::from_secs(30 * 60);

const CHARGE_STATE_FRESH: Duration = Duration::from_secs(180);

pub struct Workout {
    pub row: WorkoutRow,
    pub calories: Option<Kilocalories>,
}

pub struct WorkoutTrendEntry {
    pub workout: Workout,
    pub effort: Option<Effort>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NightSummary {
    pub asleep: Range<UnixMillis>,
    pub asleep_for: Duration,
    pub score: Option<Score>,
    pub heart: Option<heart::Asleep>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BatteryLevel {
    pub percent: u32,
    pub at: UnixMillis,
    pub charging: Option<bool>,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct EcgSummary {
    pub id: EcgId,
    pub measured_at: UnixMillis,
    pub seconds: f64,
    pub leads: u32,
    pub verdict: WatchVerdict,
}

#[derive(Debug, Clone, PartialEq)]
pub struct EcgSummaries {
    pub summaries: Vec<EcgSummary>,
    pub unreadable: Vec<UnreadableEcg>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct EcgDetail {
    pub stored: StoredEcg,
    pub verdict: WatchVerdict,
    pub reading: EcgReading,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AfibReading {
    pub id: i64,
    pub measured_at: UnixMillis,
    pub pulse: StoredPulse,
    pub asleep: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AfibAlert {
    pub measured_at: UnixMillis,
    pub counted: u32,
    pub empty: u32,
    pub readings: Vec<AfibReading>,
    pub ecg_after: Option<EcgSummary>,
    pub unreadable_readings: Vec<UnreadableReading>,
    pub unreadable_ecgs: Vec<UnreadableEcg>,
}

pub fn battery(
    store: &Store,
    device_id: DeviceId,
    now: UnixMillis,
) -> Result<Option<BatteryLevel>, Error> {
    let Some(level) = store.latest(device_id, SampleKind::BatteryPercent)? else {
        return Ok(None);
    };
    let charging = store
        .latest(device_id, SampleKind::BatteryState)?
        .filter(|state| now - state.at < CHARGE_STATE_FRESH)
        .map(|state| state.value == BATTERY_STATE_CHARGING);
    Ok(Some(BatteryLevel {
        percent: level.value as u32,
        at: level.at,
        charging,
    }))
}

pub fn beats_between(
    store: &Store,
    device_id: DeviceId,
    from: UnixMillis,
    to: UnixMillis,
) -> Result<Vec<Beat>, Error> {
    Ok(store
        .samples_between(device_id, SampleKind::HeartRate, from, to)?
        .into_iter()
        .map(|point| Beat {
            at: point.at,
            rate: Bpm(point.value as u16),
        })
        .collect())
}

pub fn session_ceiling(
    store: &Store,
    device_id: DeviceId,
    sport: SportId,
    from: UnixMillis,
    to: UnixMillis,
) -> Result<Met, Error> {
    let speed = match sport::limits(sport) {
        None => None,
        Some(limits) => {
            let fixes = store.track_points(device_id, from, to)?;
            track::clean(&fixes, limits).average_speed()
        }
    };
    Ok(sport::session_ceiling(sport, speed))
}

pub fn workout_calories(
    store: &Store,
    device_id: DeviceId,
    user: Option<&UserProfile>,
    sport: SportId,
    from: UnixMillis,
    to: UnixMillis,
) -> Result<Option<Kilocalories>, Error> {
    let Some(wearer) = user.and_then(|user| Wearer::of(user, from.to_seconds())) else {
        return Ok(None);
    };
    let beats = beats_between(store, device_id, from, to)?;

    let ceiling = session_ceiling(store, device_id, sport, from, to)?;
    Ok(Some(energy::burned(wearer, ceiling, &beats)))
}

fn session_end(row: &WorkoutRow, now: UnixMillis) -> UnixMillis {
    row.ended_at.map_or(now, UnixTime::to_millis)
}

fn workout_of(
    store: &Store,
    device_id: DeviceId,
    user: Option<&UserProfile>,
    row: WorkoutRow,
    now: UnixMillis,
) -> Result<Workout, Error> {
    let calories = workout_calories(
        store,
        device_id,
        user,
        row.sport(),
        row.started_at.to_millis(),
        session_end(&row, now),
    )?;
    Ok(Workout { row, calories })
}

pub fn workouts(
    store: &Store,
    device_id: DeviceId,
    limit: u32,
    now: UnixMillis,
) -> Result<Vec<Workout>, Error> {
    let user = store.watch_user(device_id)?;
    store
        .workouts(device_id, limit)?
        .into_iter()
        .map(|row| workout_of(store, device_id, user.as_ref(), row, now))
        .collect()
}

pub fn workouts_between(
    store: &Store,
    device_id: DeviceId,
    from: UnixMillis,
    to: UnixMillis,
    now: UnixMillis,
) -> Result<Vec<Workout>, Error> {
    let user = store.watch_user(device_id)?;
    store
        .workouts_between(device_id, from.to_seconds(), to.to_seconds())?
        .into_iter()
        .map(|row| workout_of(store, device_id, user.as_ref(), row, now))
        .collect()
}

pub fn workout(
    store: &Store,
    device_id: DeviceId,
    id: WorkoutId,
    now: UnixMillis,
) -> Result<Option<Workout>, Error> {
    let user = store.watch_user(device_id)?;
    store
        .workout(device_id, id)?
        .map(|row| workout_of(store, device_id, user.as_ref(), row, now))
        .transpose()
}

pub fn active_workout(
    store: &Store,
    device_id: DeviceId,
    now: UnixMillis,
) -> Result<Option<Workout>, Error> {
    let Some(active) = store.active_workout(device_id)? else {
        return Ok(None);
    };
    let user = store.watch_user(device_id)?;
    let row = WorkoutRow {
        id: active.id,
        started_at: active.started_at,
        ended_at: None,
        subcategory: active.subcategory,
    };
    workout_of(store, device_id, user.as_ref(), row, now).map(Some)
}

pub fn workout_trend(
    store: &Store,
    device_id: DeviceId,
    sport: SportId,
    from: UnixMillis,
    to: UnixMillis,
) -> Result<Vec<WorkoutTrendEntry>, Error> {
    let user = store.watch_user(device_id)?;
    store
        .workouts_between(device_id, from.to_seconds(), to.to_seconds())?
        .into_iter()
        .filter(|row| row.sport() == sport)
        .filter_map(|row| row.ended_at.map(|ended_at| (row, ended_at.to_millis())))
        .map(|(row, ended)| {
            let started = row.started_at.to_millis();
            let calories =
                workout_calories(store, device_id, user.as_ref(), sport, started, ended)?;
            let beats = beats_between(store, device_id, started, ended)?;
            Ok(WorkoutTrendEntry {
                workout: Workout { row, calories },
                effort: heart::effort(&beats),
            })
        })
        .collect()
}

/// What each session of the span had earned, session by session.
pub fn earned_curves(
    store: &Store,
    device_id: DeviceId,
    from: UnixMillis,
    to: UnixMillis,
    now: UnixMillis,
) -> Result<Vec<Vec<Reading>>, Error> {
    let Some(user) = store.watch_user(device_id)? else {
        return Ok(Vec::new());
    };
    store
        .workouts_between(device_id, from.to_seconds(), to.to_seconds())?
        .into_iter()
        .map(|row| {
            let Some(wearer) = Wearer::of(&user, row.started_at) else {
                return Ok(Vec::new());
            };
            let ended = session_end(&row, now);
            let started = row.started_at.to_millis();
            let beats = beats_between(store, device_id, started, ended)?;
            let ceiling = session_ceiling(store, device_id, row.sport(), started, ended)?;
            Ok(energy::accrued(wearer, ceiling, &beats))
        })
        .collect()
}

pub fn calorie_reading(at: UnixMillis, stored: i64) -> Reading {
    Reading {
        at,
        total: Hundredths::<Kilocalories>::new(stored).value(),
    }
}

/// Energy is the one series the watch does not measure: its counter is a
/// resting estimate plus what the pedometer saw, so the sessions it never
/// stepped through are added back here.
///
/// The counter's restart is only visible against the reading before it, and a
/// window rarely opens on one. The day behind the window is read in to make the
/// restart show, then dropped again, so which day a session belongs to never
/// rests on the window the caller happened to ask for.
pub fn expenditure(
    store: &Store,
    device_id: DeviceId,
    counter: &[Reading],
    now: UnixMillis,
) -> Result<Vec<Reading>, Error> {
    let (Some(first), Some(last)) = (counter.first(), counter.last()) else {
        return Ok(Vec::new());
    };
    let behind: Vec<Reading> = store
        .samples_between(
            device_id,
            SampleKind::Calories,
            first.at - DAY,
            first.at - Duration::from_millis(1),
        )?
        .into_iter()
        .map(|point| calorie_reading(point.at, point.value))
        .collect();
    let opened_at = behind.first().map_or(first.at, |earliest| earliest.at);
    let curves = earned_curves(store, device_id, opened_at, last.at, now)?;
    let joined: Vec<Reading> = behind.iter().chain(counter).copied().collect();
    let mut lifted = energy::with_workouts(&joined, &curves, opened_at);
    Ok(lifted.split_off(behind.len()))
}

pub fn calorie_series(
    store: &Store,
    device_id: DeviceId,
    found: &[SeriesPoint],
    now: UnixMillis,
) -> Result<Vec<(Reading, Source)>, Error> {
    let counter: Vec<Reading> = found
        .iter()
        .map(|point| calorie_reading(point.at, point.value))
        .collect();
    let lifted = expenditure(store, device_id, &counter, now)?;
    assert_eq!(
        lifted.len(),
        found.len(),
        "one lifted reading per stored one"
    );
    Ok(lifted
        .into_iter()
        .zip(found)
        .map(|(reading, point)| (reading, point.source))
        .collect())
}

pub fn calories_windowed_max(
    store: &Store,
    device_id: DeviceId,
    edges: &Edges,
    now: UnixMillis,
) -> Result<Vec<Option<Kilocalories>>, Error> {
    let Some((first, last)) = edges.span() else {
        return Ok(Vec::new());
    };
    let counter: Vec<Reading> = store
        .samples_between(
            device_id,
            SampleKind::Calories,
            first,
            last - Duration::from_millis(1),
        )?
        .into_iter()
        .map(|point| calorie_reading(point.at, point.value))
        .collect();
    let lifted = expenditure(store, device_id, &counter, now)?;
    let mut found: Vec<Option<f64>> = vec![None; edges.windows()];
    for reading in lifted {
        if let Some(window) = edges.window_of(reading.at) {
            found[window] =
                Some(found[window].map_or(reading.total.0, |seen| seen.max(reading.total.0)));
        }
    }
    Ok(found.into_iter().map(|max| max.map(Kilocalories)).collect())
}

pub fn latest_calories(
    store: &Store,
    device_id: DeviceId,
    now: UnixMillis,
) -> Result<Option<Reading>, Error> {
    let Some(latest) = store.latest(device_id, SampleKind::Calories)? else {
        return Ok(None);
    };
    let counter = [calorie_reading(latest.at, latest.value)];
    let lifted = expenditure(store, device_id, &counter, now)?;
    Ok(lifted.first().copied())
}

pub fn ascents(
    store: &Store,
    device_id: DeviceId,
    from: UnixMillis,
    to: UnixMillis,
) -> Result<Vec<(UnixMillis, Metres)>, Error> {
    let minutes = store.activity_minutes(device_id, from.to_seconds(), to.to_seconds())?;
    Ok(minutes
        .iter()
        .filter(|minute| minute.at.to_millis() >= from && minute.at.to_millis() <= to)
        .filter_map(|minute| Some((minute.at.to_millis(), minute.ascent?.value())))
        .collect())
}

// A workout still running has no end, and its minutes are as much its own as a
// finished one's.
fn recorded_spans(
    store: &Store,
    device_id: DeviceId,
    from: UnixTime,
    to: UnixTime,
) -> Result<Vec<Range<UnixTime>>, Error> {
    Ok(store
        .workouts_between(device_id, from, to)?
        .iter()
        .map(|row| row.started_at..row.ended_at.unwrap_or(to))
        .collect())
}

pub fn detected_activities(
    store: &Store,
    device_id: DeviceId,
    from: UnixMillis,
    to: UnixMillis,
) -> Result<Vec<Session>, Error> {
    let (from, to) = (from.to_seconds(), to.to_seconds());
    let minutes = store.activity_minutes(device_id, from, to)?;
    let recorded = recorded_spans(store, device_id, from, to)?;
    Ok(activity::detect(&minutes, &recorded))
}

pub fn night(
    store: &Store,
    device_id: DeviceId,
    from: UnixMillis,
    to: UnixMillis,
) -> Result<Night, Error> {
    let minutes = store.activity_minutes(device_id, from.to_seconds(), to.to_seconds())?;
    Ok(sleep::night(&minutes))
}

pub fn asleep_spans(
    store: &Store,
    device_id: DeviceId,
    from: UnixMillis,
    to: UnixMillis,
) -> Result<Vec<Range<UnixMillis>>, Error> {
    let windows = store.sleep_minutes(device_id, from.to_seconds(), to.to_seconds())?;
    Ok(sleep::asleep_spans(&windows))
}

pub fn night_trend(
    store: &Store,
    device_id: DeviceId,
    windows: &[Range<UnixMillis>],
) -> Result<Vec<NightSummary>, Error> {
    let mut out = Vec::new();
    for window in windows {
        let night = night(store, device_id, window.start, window.end)?;
        let Some(span) = night.asleep else {
            continue;
        };
        let asleep: Vec<&sleep::Band> = night
            .stages
            .iter()
            .filter(|band| band.level != SleepLevel::Awake)
            .collect();
        let beats: Vec<Beat> = beats_between(store, device_id, span.start, span.end)?
            .into_iter()
            .filter(|beat| {
                asleep
                    .iter()
                    .any(|band| beat.at >= band.from && beat.at < band.to)
            })
            .collect();
        out.push(NightSummary {
            asleep_for: Duration::from_millis(
                asleep
                    .iter()
                    .map(|band| (band.to - band.from).as_millis())
                    .sum(),
            ),
            score: night.score,
            heart: heart::asleep(&beats),
            asleep: span,
        });
    }
    Ok(out)
}

/// Everything a waking rate at rest is read against, read once for a span.
struct Waking {
    busy: Vec<Range<UnixMillis>>,
    beats: Vec<Beat>,
}

impl Waking {
    fn read(
        store: &Store,
        device_id: DeviceId,
        from: UnixMillis,
        to: UnixMillis,
    ) -> Result<Waking, Error> {
        let (from_secs, to_secs) = (from.to_seconds(), to.to_seconds());
        let recorded = recorded_spans(store, device_id, from_secs, to_secs)?;
        let minutes = store.activity_minutes(device_id, from_secs, to_secs)?;
        let detected = activity::detect(&minutes, &recorded)
            .into_iter()
            .map(|session| session.started_at..session.ended_at);
        let busy = recorded
            .iter()
            .cloned()
            .chain(detected)
            .map(|span| span.start.to_millis()..span.end.to_millis() + RECOVERY)
            .chain(asleep_spans(store, device_id, from, to)?)
            .collect();
        let beats = store
            .samples_from(
                device_id,
                SampleKind::HeartRate,
                Source::Stored,
                from,
                to - Duration::from_millis(1),
            )?
            .into_iter()
            .map(|point| Beat {
                at: point.at,
                rate: Bpm(point.value as u16),
            })
            .collect();
        Ok(Waking { busy, beats })
    }

    fn at_rest(&self, from: UnixMillis, to: UnixMillis) -> Option<AtRest> {
        let within: Vec<Beat> = self
            .beats
            .iter()
            .filter(|beat| beat.at >= from && beat.at < to)
            .copied()
            .collect();
        heart::at_rest(&within, &self.busy)
    }
}

pub fn awake_heart(
    store: &Store,
    device_id: DeviceId,
    edges: &Edges,
) -> Result<Vec<Option<AtRest>>, Error> {
    let Some((first, last)) = edges.span() else {
        return Ok(Vec::new());
    };
    let waking = Waking::read(store, device_id, first, last)?;
    Ok(edges
        .as_slice()
        .windows(2)
        .map(|edge| waking.at_rest(edge[0], edge[1]))
        .collect())
}

pub fn zone_time(
    store: &Store,
    device_id: DeviceId,
    edges: &Edges,
    floors: &[Bpm],
) -> Result<Vec<Vec<Duration>>, Error> {
    let Some((first, last)) = edges.span() else {
        return Ok(Vec::new());
    };
    let mut out = vec![vec![Duration::ZERO; floors.len()]; edges.windows()];
    let sessions = store.workouts_between(device_id, first.to_seconds(), last.to_seconds())?;
    for row in sessions {
        let Some(ended_at) = row.ended_at else {
            continue;
        };
        let started = row.started_at.to_millis();
        // A session is counted whole on the day it began, like every other
        // figure about it.
        let Some(window) = edges.window_of(started) else {
            continue;
        };
        let beats = beats_between(store, device_id, started, ended_at.to_millis())?;
        for (total, spent) in out[window].iter_mut().zip(heart::zone_time(&beats, floors)) {
            *total = *total + Duration::from_millis(spent);
        }
    }
    Ok(out)
}

fn ecg_summary(store: &Store, row: EcgSummaryRow) -> Result<EcgSummary, Error> {
    let leads = row.kind.leads().len();
    let per_lead = row.sample_bytes / 2 / leads as i64;
    Ok(EcgSummary {
        id: row.id,
        measured_at: row.measured_at,
        seconds: per_lead as f64 / row.sampling_hz.as_f64(),
        leads: leads as u32,
        verdict: WatchVerdict::of(&store.ecg_measures(row.id)?),
    })
}

pub fn ecg_summaries(store: &Store, device_id: DeviceId) -> Result<EcgSummaries, Error> {
    let listed = store.ecgs(device_id)?;
    Ok(EcgSummaries {
        summaries: listed
            .rows
            .into_iter()
            .map(|row| ecg_summary(store, row))
            .collect::<Result<_, _>>()?,
        unreadable: listed.unreadable,
    })
}

pub fn ecg(store: &Store, id: EcgId) -> Result<Option<EcgDetail>, Error> {
    let Some(stored) = store.ecg(id)? else {
        return Ok(None);
    };
    let reading = rhythm::read_ecg(&stored.recording.analysed().samples, stored.recording.rate);
    Ok(Some(EcgDetail {
        verdict: WatchVerdict::of(&store.ecg_measures(id)?),
        stored,
        reading,
    }))
}

fn afib_reading(
    store: &Store,
    device_id: DeviceId,
    row: &AfibReadingRow,
    raw: &[Millis],
    asleep: &[Range<UnixMillis>],
) -> Result<AfibReading, Error> {
    let watch = store.nearest_sample(
        device_id,
        SampleKind::HeartRate,
        row.measured_at,
        Duration::from_millis(WATCH_RATE_WITHIN_MS),
    )?;
    let watch = watch.filter(|bpm| *bpm > 0).map(|bpm| Bpm(bpm as u16));
    Ok(AfibReading {
        id: row.id,
        measured_at: row.measured_at,
        pulse: rhythm::read_stored_pulse(raw, watch),
        asleep: asleep.iter().any(|span| span.contains(&row.measured_at)),
    })
}

fn is_recorded(row: &AfibReadingRow) -> bool {
    row.duration > Duration::ZERO && row.samples.iter().any(|b| *b != 0)
}

pub fn afib_alert(store: &Store, device_id: DeviceId, id: i64) -> Result<Option<AfibAlert>, Error> {
    let Some(anchor) = store
        .afib_episodes(device_id)?
        .into_iter()
        .find(|row| row.id == id)
    else {
        return Ok(None);
    };
    let anchored_at = anchor.measured_at;
    let from = anchored_at - Duration::from_secs(PpgAfibRule::WINDOW_SECS);
    let rows = store.afib_readings_up_to(device_id, id, from)?;
    let marks: Vec<(UnixTime, bool)> = rows
        .iter()
        .map(|row| {
            (
                row.measured_at.to_seconds(),
                row.attrib == Some(STORED_MEASURE_ATTRIB_CONFIRMED),
            )
        })
        .collect();
    let counted = &rows[PpgAfibRule::counted(&marks, rows.len() - 1)];
    let asleep = asleep_spans(store, device_id, from, anchored_at)?;
    let (parsed, unreadable): (Vec<_>, Vec<_>) = counted
        .iter()
        .filter(|row| is_recorded(row))
        .map(|row| row.intervals().map(|raw| (row, raw)))
        .partition(Result::is_ok);
    let readings = parsed
        .into_iter()
        .map(Result::unwrap)
        .map(|(row, raw)| afib_reading(store, device_id, row, &raw, &asleep))
        .collect::<Result<Vec<_>, Error>>()?;
    let ecgs = store.ecgs(device_id)?;
    let ecg_after = ecgs
        .rows
        .into_iter()
        .filter(|row| (anchored_at..=anchored_at + ECG_AFTER_ALERT).contains(&row.measured_at))
        .min_by_key(|row| row.measured_at)
        .map(|row| ecg_summary(store, row))
        .transpose()?;
    Ok(Some(AfibAlert {
        measured_at: anchored_at,
        counted: counted.len() as u32,
        empty: counted.iter().filter(|row| !is_recorded(row)).count() as u32,
        readings,
        ecg_after,
        unreadable_readings: unreadable.into_iter().map(Result::unwrap_err).collect(),
        unreadable_ecgs: ecgs.unreadable,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use wpp::activity::Minute;
    use wpp::client::{Record, UserProfile};
    use wpp::rhythm::{Pattern, PulseCheck, PulseVerdict};

    const DAY_ONE: i64 = 1_786_662_000_000;
    const DAY_MS: i64 = 24 * 60 * 60 * 1000;
    const ROLLOVER_MS: i64 = 5 * 60 * 1000;
    const LONG_AFTER: UnixMillis = UnixMillis(DAY_ONE + 30 * DAY_MS);

    fn span(from_ms: i64, to_ms: i64) -> Range<UnixMillis> {
        UnixMillis(from_ms)..UnixMillis(to_ms)
    }

    fn counter(at_ms: i64, kcal: i64) -> Record {
        Record::Sample {
            measured_at: UnixMillis(at_ms),
            kind: SampleKind::Calories,
            value: kcal * 100,
            quality: None,
            source: Source::Stored,
            window_secs: None,
            context: None,
        }
    }

    fn beat(at_ms: i64) -> Record {
        Record::Sample {
            measured_at: UnixMillis(at_ms),
            kind: SampleKind::HeartRate,
            value: 130,
            quality: Some(4),
            source: Source::Live,
            window_secs: None,
            context: None,
        }
    }

    /// A counter reading a quarter of an hour into the day, which is the shape
    /// that broke this: the restart lands past the edge the caller asks from,
    /// so the window opens on a reading with no drop in front of it.
    fn two_days_and_a_session() -> (Store, DeviceId) {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let mut records = vec![Record::User(UserProfile {
            id: 1,
            weight: 73_000,
            height: 175,
            gender: 0,
            birth: 738_892_800,
            first_name: "d".to_string(),
        })];
        for (day, peak) in [(DAY_ONE, 1800), (DAY_ONE + DAY_MS, 1700)] {
            for hour in 0..24 {
                let at = day + 15 * 60 * 1000 + hour * 3_600_000;
                records.push(counter(at, 5 + peak * hour / 24));
            }
        }
        let opened = DAY_ONE + 12 * 3_600_000;
        records.push(Record::WorkoutStarted {
            started_at: UnixMillis(opened).to_seconds(),
            subcategory: 16,
        });
        records.push(Record::WorkoutEnded {
            started_at: UnixMillis(opened).to_seconds(),
            ended_at: UnixMillis(opened + 45 * 60 * 1000).to_seconds(),
            paused_secs: 0,
        });
        records.extend((0..46).map(|minute| beat(opened + minute * 60 * 1000)));
        store.store(device, &records).unwrap();
        (store, device)
    }

    fn day_total(store: &Store, device: DeviceId, day: i64) -> f64 {
        let from = day + ROLLOVER_MS;
        let readings: Vec<Reading> = store
            .samples_between(
                device,
                SampleKind::Calories,
                UnixMillis(from),
                UnixMillis(from + DAY_MS - 1),
            )
            .unwrap()
            .into_iter()
            .map(|point| calorie_reading(point.at, point.value))
            .collect();
        expenditure(store, device, &readings, LONG_AFTER)
            .unwrap()
            .iter()
            .map(|reading| reading.total.0)
            .fold(0.0, f64::max)
    }

    #[test]
    fn the_day_a_session_was_on_is_the_only_day_it_is_added_to() {
        let (store, device) = two_days_and_a_session();
        let lifted = day_total(&store, device, DAY_ONE);
        let after = day_total(&store, device, DAY_ONE + DAY_MS);
        assert!(lifted > 1805.0, "the session is the day's own: {lifted}");
        assert!(after < 1706.0, "and is not the next day's: {after}");
    }

    fn rate(at_ms: i64, bpm: i64, source: Source) -> Record {
        Record::Sample {
            measured_at: UnixMillis(at_ms),
            kind: SampleKind::HeartRate,
            value: bpm,
            quality: None,
            source,
            window_secs: None,
            context: None,
        }
    }

    fn staged(at_ms: i64, minutes: i64, level: i64) -> Record {
        Record::Activity(Minute {
            duration: Duration::from_secs(minutes * 60),
            sleep_level: Some(level),
            ..Minute::opened(UnixMillis(at_ms).to_seconds())
        })
    }

    #[test]
    fn a_session_trend_leaves_out_the_stretch_the_sensor_lost_the_wrist() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let opened = DAY_ONE + 12 * 3_600_000;
        let mut records = vec![
            Record::WorkoutStarted {
                started_at: UnixMillis(opened).to_seconds(),
                subcategory: 16,
            },
            Record::WorkoutEnded {
                started_at: UnixMillis(opened).to_seconds(),
                ended_at: UnixMillis(opened + 30 * 60 * 1000).to_seconds(),
                paused_secs: 0,
            },
            Record::WorkoutStarted {
                started_at: UnixMillis(opened + 3_600_000).to_seconds(),
                subcategory: 16,
            },
        ];
        records.extend((0..1800).map(|second| {
            let bpm = if (600..900).contains(&second) {
                42
            } else {
                125
            };
            rate(opened + second * 1000, bpm, Source::Live)
        }));
        records.extend((0..60).map(|step| rate(opened + step * 30_000 + 500, 125, Source::Stored)));
        store.store(device, &records).unwrap();

        let trend = workout_trend(
            &store,
            device,
            SportId(16),
            UnixMillis(DAY_ONE),
            UnixMillis(DAY_ONE + DAY_MS),
        )
        .unwrap();
        assert_eq!(
            trend.len(),
            1,
            "the session still open is left out"
        );
        let heart = trend[0].effort.unwrap();
        assert_eq!(heart.median, Bpm(125));
        assert_eq!(heart.p95, Bpm(125));
        assert!((299_000..=301_000).contains(&heart.dropout_ms), "{heart:?}");
    }

    #[test]
    fn a_night_trend_reads_only_the_windows_the_watch_staged() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let bed = DAY_ONE + 23 * 3_600_000;
        let mut records = vec![
            staged(bed, 30, 1),
            staged(bed + 30 * 60_000, 150, 0),
            staged(bed + 180 * 60_000, 30, 2),
        ];
        records.extend((0..21).map(|step| {
            let at = bed + step * 10 * 60_000;
            let awake = (30..180).contains(&(step * 10));
            rate(at, if awake { 90 } else { 50 + step }, Source::Stored)
        }));
        store.store(device, &records).unwrap();

        let windows = vec![
            span(bed - 6 * 3_600_000, bed + 12 * 3_600_000),
            span(bed + 18 * 3_600_000, bed + 36 * 3_600_000),
        ];
        let nights = night_trend(&store, device, &windows).unwrap();
        assert_eq!(nights.len(), 1);
        assert_eq!(nights[0].asleep_for, Duration::from_millis(60 * 60_000));
        let heart = nights[0].heart.unwrap();
        assert!(heart.median < Bpm(90), "awake readings stay out: {heart:?}");
        assert_eq!(heart.resting, Bpm(50));
    }

    /// A night reading where the pulse sensor counted most beats twice, as the
    /// watch kept it: one byte per interval at 100 Hz.
    const SPLIT_NIGHT_READING: [u8; 57] = [
        38, 59, 98, 112, 36, 65, 37, 66, 37, 71, 37, 65, 37, 55, 39, 50, 37, 66, 36, 63, 36, 55,
        34, 48, 37, 73, 37, 75, 36, 60, 35, 48, 37, 55, 37, 63, 37, 65, 36, 55, 35, 57, 36, 66, 38,
        58, 36, 46, 77, 79, 37, 48, 83, 34, 48, 86, 38,
    ];

    fn reading_row(at_ms: i64) -> AfibReadingRow {
        AfibReadingRow {
            id: 1,
            measured_at: UnixMillis(at_ms),
            duration: Duration::from_secs(30),
            sampling_hz: 100,
            sample_bytes: 1,
            samples: SPLIT_NIGHT_READING.to_vec(),
            attrib: Some(STORED_MEASURE_ATTRIB_CONFIRMED),
        }
    }

    #[test]
    fn a_reading_is_read_against_the_rate_the_watch_logged_for_it() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let at = DAY_ONE + 2 * 3_600_000;
        store
            .store(device, &[rate(at + 45_000, 61, Source::Stored)])
            .unwrap();
        let asleep = [span(at - 3_600_000, at + 3_600_000)];

        let row = reading_row(at);
        let reading =
            afib_reading(&store, device, &row, &row.intervals().unwrap(), &asleep).unwrap();
        assert!(reading.asleep);
        assert!(
            reading.pulse.intervals.len() <= SPLIT_NIGHT_READING.len() - 20,
            "split beats are taken back together"
        );
        match reading.pulse.check {
            PulseCheck::Read(PulseVerdict::Assessed(rhythm)) => {
                assert_ne!(rhythm.pattern, Pattern::Irregular);
            }
            other => panic!("{other:?}"),
        }
        assert!((55..=70).contains(&reading.pulse.heart_rate.unwrap().0));
    }

    #[test]
    fn a_reading_with_no_rate_logged_for_it_is_left_unchecked() {
        let store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let row = reading_row(DAY_ONE);
        let reading = afib_reading(&store, device, &row, &row.intervals().unwrap(), &[]).unwrap();
        assert_eq!(reading.pulse.check, PulseCheck::NoWatchRate);
        assert_eq!(reading.pulse.intervals.len(), SPLIT_NIGHT_READING.len());
        assert!(!reading.asleep);
    }

    fn minute_with_ascent(at_ms: i64, centimetres: Option<i64>) -> Record {
        Record::Activity(Minute {
            duration: Duration::from_secs(60),
            ascent: centimetres.map(Hundredths::new),
            ..Minute::opened(UnixMillis(at_ms).to_seconds())
        })
    }

    #[test]
    fn a_minute_with_no_ascent_reported_is_no_point_on_the_climb() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        store
            .store(
                device,
                &[
                    minute_with_ascent(DAY_ONE, Some(250)),
                    minute_with_ascent(DAY_ONE + 60_000, None),
                ],
            )
            .unwrap();
        let climbed = ascents(&store, device, UnixMillis(DAY_ONE), UnixMillis(DAY_ONE + DAY_MS)).unwrap();
        assert_eq!(climbed, vec![(UnixMillis(DAY_ONE), Metres(2.5))]);
    }

    fn finished(opened: i64, minutes: i64, sport: i16) -> Vec<Record> {
        vec![
            Record::WorkoutStarted {
                started_at: UnixMillis(opened).to_seconds(),
                subcategory: sport,
            },
            Record::WorkoutEnded {
                started_at: UnixMillis(opened).to_seconds(),
                ended_at: UnixMillis(opened + minutes * 60_000).to_seconds(),
                paused_secs: 0,
            },
        ]
    }

    #[test]
    fn zone_time_counts_a_session_whole_on_the_day_it_began() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let opened = DAY_ONE + DAY_MS - 5 * 60_000;
        let mut records = finished(opened, 10, 16);
        records.extend((0..=10).map(|minute| rate(opened + minute * 60_000, 130, Source::Stored)));
        store.store(device, &records).unwrap();
        let edges = Edges::new(vec![
            UnixMillis(DAY_ONE),
            UnixMillis(DAY_ONE + DAY_MS),
            UnixMillis(DAY_ONE + 2 * DAY_MS),
        ])
        .unwrap();

        let spent = zone_time(&store, device, &edges, &[Bpm(100), Bpm(150)]).unwrap();
        assert_eq!(spent[0], vec![Duration::from_secs(600), Duration::from_secs(0)]);
        assert_eq!(spent[1], vec![Duration::from_secs(0), Duration::from_secs(0)]);
    }

    #[test]
    fn the_waking_rate_leaves_out_a_session_and_its_recovery() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let opened = DAY_ONE + 12 * 3_600_000;
        let mut records = finished(opened, 30, 16);
        records.extend((0..30).map(|minute| rate(opened + minute * 60_000, 140, Source::Stored)));
        records.push(rate(opened + 35 * 60_000, 120, Source::Stored));
        records.extend((0..5).map(|step| rate(DAY_ONE + step * 3_600_000, 62, Source::Stored)));
        store.store(device, &records).unwrap();
        let edges = Edges::new(vec![UnixMillis(DAY_ONE), UnixMillis(DAY_ONE + DAY_MS)]).unwrap();

        let rest = awake_heart(&store, device, &edges).unwrap();
        assert_eq!(rest, vec![Some(AtRest { median: Bpm(62), readings: 5 })]);
    }

    fn straight_ride(store: &mut Store, device: DeviceId, opened: i64, metres_per_second: f64) {
        let fixes: Vec<track::Fix> = (0..120)
            .map(|second| track::Fix {
                at: UnixMillis(opened + second * 1000),
                lat_e7: ((52.0 + second as f64 * metres_per_second / 111_320.0) * 1e7) as i32,
                lon_e7: 49_000_000,
                altitude_cm: None,
                accuracy_cm: Some(500),
                speed_mm_s: None,
                bearing_cdeg: None,
            })
            .collect();
        store.record_fixes(device, &fixes).unwrap();
    }

    #[test]
    fn a_slow_ride_is_held_to_the_speed_it_actually_went() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let cycling = SportId(6);
        straight_ride(&mut store, device, DAY_ONE, 3.0);
        let from = UnixMillis(DAY_ONE);
        let to = UnixMillis(DAY_ONE + 120_000);
        let held = session_ceiling(&store, device, cycling, from, to).unwrap();
        assert!(held.0 < sport::ceiling(cycling).0, "{held:?}");

        let nothing_recorded = UnixMillis(DAY_ONE + DAY_MS);
        assert_eq!(
            session_ceiling(&store, device, cycling, nothing_recorded, nothing_recorded + Duration::from_secs(60))
                .unwrap(),
            sport::ceiling(cycling)
        );
    }

    #[test]
    fn a_session_is_only_costed_for_a_wearer_the_watch_has_described() {
        let mut store = Store::open_in_memory().unwrap();
        let device = store.device("a4:7e:fa:44:d6:10").unwrap();
        let opened = DAY_ONE;
        store
            .store(
                device,
                &(0..=30)
                    .map(|minute| rate(opened + minute * 60_000, 140, Source::Stored))
                    .collect::<Vec<_>>(),
            )
            .unwrap();
        let (from, to) = (UnixMillis(opened), UnixMillis(opened + 30 * 60_000));
        let weights = SportId(16);
        assert_eq!(workout_calories(&store, device, None, weights, from, to).unwrap(), None);

        let wearer = UserProfile {
            id: 1,
            weight: 75_000,
            height: 178,
            gender: 0,
            birth: 0,
            first_name: String::new(),
        };
        let burned = workout_calories(&store, device, Some(&wearer), weights, from, to)
            .unwrap()
            .expect("a known wearer");
        assert!(burned.0 > 0.0);

        let sex_unknown = UserProfile { gender: 9, ..wearer };
        assert_eq!(
            workout_calories(&store, device, Some(&sex_unknown), weights, from, to).unwrap(),
            None
        );
    }

    #[test]
    fn each_window_reads_the_highest_the_counter_reached_in_it() {
        let (store, device) = two_days_and_a_session();
        let edges = Edges::new(vec![
            UnixMillis(DAY_ONE + ROLLOVER_MS),
            UnixMillis(DAY_ONE + ROLLOVER_MS + DAY_MS),
            UnixMillis(DAY_ONE + ROLLOVER_MS + 2 * DAY_MS),
            UnixMillis(DAY_ONE + ROLLOVER_MS + 3 * DAY_MS),
        ])
        .unwrap();
        let peaks = calories_windowed_max(&store, device, &edges, LONG_AFTER).unwrap();
        assert_eq!(peaks.len(), 3);
        assert!(peaks[0].unwrap().0 > 1805.0, "{peaks:?}");
        assert!(peaks[1].unwrap().0 < 1706.0, "{peaks:?}");
        assert_eq!(peaks[2], None);
    }
}
