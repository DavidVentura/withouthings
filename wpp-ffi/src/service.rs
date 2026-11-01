use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use wpp::activity;
use wpp::ancs::{self, NotificationCenter, NotificationId};
use wpp::capture::{FrameReassembler, StreamItem};
use wpp::client::{
    Action, Client, Credentials, Event, Feature, FeatureId, FeatureSchedule, SampleKind,
};
use wpp::image::{GlyphRequest, IconRequest, Mono};
use wpp::signal::STORED_MEASURE_ATTRIB_CONFIRMED;
use wpp::sport::{self, SportId};
use wpp::track;
use wpp::units::{Bpm, Duration, Millivolts, UnixMillis, UnixTime};
use wpp::watch_config::{
    default_features, screen_name, CATEGORIES, HEALTH_FEATURES, KNOWN_SCREENS,
    RESPIRATORY_SCAN_MONITORING, SCAN_WINDOW,
};
use wpp::Frame;
use wpp_store::{report, DeviceId, EcgId, SetBoundary, Store, Trim, WorkoutId};

use crate::records::*;
use crate::route::RouteTrack;

#[uniffi::export]
pub fn ancs_uuids() -> AncsUuids {
    AncsUuids {
        service: ancs::SERVICE_UUID.into(),
        notification_source: ancs::NOTIFICATION_SOURCE_UUID.into(),
        control_point: ancs::CONTROL_POINT_UUID.into(),
        data_source: ancs::DATA_SOURCE_UUID.into(),
    }
}

struct Inner {
    client: Client,
    reassembler: FrameReassembler,
}

/// How far back a live distance is held still. Long enough that the smoother
/// has stopped moving those fixes, short enough that a fix costs a fixed
/// window rather than the session so far.
const SETTLE: Duration = Duration::from_secs(60);

#[derive(uniffi::Object)]
pub struct WatchService {
    inner: Mutex<Inner>,
    store: Mutex<Store>,
    transport: Box<dyn Transport>,
    ancs: Box<dyn AncsLink>,
    rasterizer: Box<dyn Rasterizer>,
    device_id: DeviceId,
    notifications: Mutex<NotificationCenter>,
    live_distance: Mutex<Option<LiveDistance>>,
    unreadable_rows: Mutex<UnreadableRows>,
}

// The lists are re-read on every screen, so a bad row is told once rather than
// on every read of it.
#[derive(Default)]
struct UnreadableRows {
    seen: HashSet<String>,
    pending: Vec<String>,
}

impl UnreadableRows {
    fn note(&mut self, problem: &impl std::fmt::Display) {
        let line = problem.to_string();
        if self.seen.insert(line.clone()) {
            self.pending.push(line);
        }
    }
}

/// What the watch has already been told, so a fix costs a window rather than
/// the whole session.
struct LiveDistance {
    workout_started_at: UnixTime,
    settled_metres: f64,
    settled_through: UnixMillis,
}

#[uniffi::export]
impl WatchService {
    #[uniffi::constructor]
    pub fn new(
        db_path: String,
        mac: String,
        secret: String,
        transport: Box<dyn Transport>,
        ancs: Box<dyn AncsLink>,
        rasterizer: Box<dyn Rasterizer>,
    ) -> Result<Self, WatchError> {
        let mut store = Store::open(&db_path)?;
        let device_id = store.device(&mac)?;
        let watermarks = store.watermarks(device_id, &CATEGORIES)?;
        let features = store.features_or_seed(device_id, &default_features())?;
        let client = Client::new(Credentials::new(&mac, secret), watermarks, features);
        Ok(WatchService {
            inner: Mutex::new(Inner {
                client,
                reassembler: FrameReassembler::new(),
            }),
            store: Mutex::new(store),
            transport,
            ancs,
            rasterizer,
            device_id,
            notifications: Mutex::new(NotificationCenter::new()),
            live_distance: Mutex::new(None),
            unreadable_rows: Mutex::default(),
        })
    }

    pub fn on_connected(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.reassembler.reset();
            inner.client.handle(Event::Connected { now: now() })
        };
        self.dispatch(actions)
    }

    pub fn poll_battery(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.client.poll_battery(now())
        };
        self.dispatch(actions)
    }

    pub fn tick(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            inner.client.handle(Event::Tick {
                now: now(),
            })
        };
        self.dispatch(actions)
    }

    pub fn unhandled_objects(&self) -> Vec<String> {
        let unhandled = self.inner.lock().unwrap().client.take_unhandled();
        let unreadable = std::mem::take(&mut self.unreadable_rows.lock().unwrap().pending);
        unhandled
            .into_iter()
            .map(|(command, type_id, name)| {
                let command_name = wpp::commands::Command(command)
                    .name()
                    .unwrap_or("unknown command");
                format!("{command_name} ({command}) carried {name} ({type_id}), unread")
            })
            .chain(unreadable)
            .collect()
    }

    pub fn on_bytes(&self, bytes: Vec<u8>, received_at_ms: i64) -> Result<(), WatchError> {
        let mut pending = Vec::new();
        let mut frames = Vec::new();
        let mut undecoded = Vec::new();
        {
            let mut inner = self.inner.lock().unwrap();
            let items = inner.reassembler.push(&bytes);
            for item in items {
                match item {
                    StreamItem::Frame { frame, .. } => {
                        pending.extend(inner.client.handle(Event::Frame {
                            frame: frame.clone(),
                            received_at: UnixMillis(received_at_ms),
                        }));
                        frames.push(frame);
                    }
                    StreamItem::Desync { bytes, .. } => undecoded.push(bytes),
                }
            }
        }
        // Outside any transaction, and the client has already consumed these
        // frames, so a failure here must not cost it the replies they earned.
        let kept = {
            let store = self.store.lock().unwrap();
            undecoded.iter().try_for_each(|bytes| {
                let command = Frame::declared_command(bytes).unwrap_or(0);
                store.store_undecoded(
                    self.device_id,
                    UnixMillis(received_at_ms),
                    command as i64,
                    bytes,
                    Frame::splice_offset(bytes).map(|at| at as i64),
                )
            })
        };
        let drawn = frames.iter().try_for_each(|frame| self.draw(frame));
        self.dispatch(pending)?;
        kept?;
        drawn
    }

    pub fn on_disconnected(&self) {
        {
            let mut inner = self.inner.lock().unwrap();
            inner.reassembler.reset();
            inner.client.handle(Event::Disconnected);
        }
        self.transport.changed();
    }

    pub fn sync_now(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.sync_now(now());
        self.dispatch(actions)
    }

    pub fn factory_reset(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.factory_reset(now());
        self.dispatch(actions)
    }

    pub fn request_refresh(&self) -> Result<(), WatchError> {
        let actions = {
            let mut inner = self.inner.lock().unwrap();
            let now = now();
            let mut actions = inner.client.force_refresh(now);
            actions.extend(inner.client.walk_now(now));
            actions
        };
        self.dispatch(actions)
    }

    pub fn progress(&self) -> Progress {
        let inner = self.inner.lock().unwrap();
        Progress::of(inner.client.phase())
    }

    pub fn snapshot(&self) -> Result<Snapshot, WatchError> {
        let now = now();
        let store = self.store.lock().unwrap();
        let (phase, pending, progress, measuring) = {
            let inner = self.inner.lock().unwrap();
            let transfer = inner.client.transfer_progress();
            (
                Progress::of(inner.client.phase()),
                inner.client.pending_deletes() as u32,
                SyncProgress {
                    history_fraction: inner.client.walk_span().and_then(|(from, at)| {
                        let span = now.to_seconds().0 - from.0;
                        if span <= 0 || from.0 <= 0 {
                            None
                        } else {
                            Some(((at.0 - from.0) as f64 / span as f64).clamp(0.0, 1.0))
                        }
                    }),
                    transfer_received: transfer.map(|(got, _)| got as u32),
                    transfer_total: transfer.map(|(_, total)| total as u32),
                    records_stored: inner.client.records_emitted(),
                    streams_done: inner.client.stream_position().0,
                    streams_total: inner.client.stream_position().1,
                },
                inner.client.measuring().is_some(),
            )
        };
        let progress_phase = phase;
        let wearer = store.watch_user(self.device_id)?;
        Ok(Snapshot {
            progress: progress_phase,
            user: wearer.as_ref().map(|user| UserProfile {
                birth_secs: user.birth as i64,
                weight_grams: user.weight,
                height_cm: user.height,
                first_name: user.first_name.clone(),
            }),
            device: store
                .identity(self.device_id)?
                .map(|identity| DeviceIdentity {
                    name: identity.name,
                    firmware: identity.firmware,
                    bootloader: identity.bootloader,
                    hardware: identity.hardware,
                    rescue: identity.rescue,
                }),
            battery: report::battery(&store, self.device_id, now)?.map(|level| Battery {
                percent: level.percent,
                at_ms: level.at.0,
                charging: level.charging,
            }),
            latest_hr: store
                .latest(self.device_id, SampleKind::HeartRate)?
                .map(|point| HrPoint {
                    at_ms: point.at.0,
                    bpm: point.value as u16,
                    origin: Origin::Stored,
                }),
            latest_temperature: store
                .latest(self.device_id, SampleKind::CoreTemperature)?
                .map(|point| Temperature {
                    celsius: Metric::Temperature.reading(point.value),
                    at_ms: point.at.0,
                }),
            steps: store
                .latest(self.device_id, SampleKind::Steps)?
                .map(|point| Steps {
                    count: point.value as u32,
                    day_start_ms: point.at.0,
                }),
            active_workout: report::active_workout(&store, self.device_id, now)?
                .map(workout_summary),
            pending_deletes: pending,
            sync: progress,
            measuring,
        })
    }

    pub fn hr_series(
        &self,
        from_ms: i64,
        to_ms: i64,
        max_points: u32,
    ) -> Result<Vec<HrPoint>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .series(
                self.device_id,
                SampleKind::HeartRate,
                UnixMillis(from_ms),
                UnixMillis(to_ms),
                max_points,
            )?
            .into_iter()
            .map(|point| HrPoint {
                at_ms: point.at.0,
                bpm: point.value as u16,
                origin: origin_of(point.source),
            })
            .collect())
    }

    pub fn series(
        &self,
        metric: Metric,
        from_ms: i64,
        to_ms: i64,
        max_points: u32,
    ) -> Result<Vec<Point>, WatchError> {
        let store = self.store.lock().unwrap();
        let found = store.series(
            self.device_id,
            metric.kind(),
            UnixMillis(from_ms),
            UnixMillis(to_ms),
            max_points,
        )?;
        if metric != Metric::Calories {
            return Ok(found
                .iter()
                .map(|point| Point {
                    at_ms: point.at.0,
                    value: metric.reading(point.value),
                    origin: origin_of(point.source),
                })
                .collect());
        }
        Ok(report::calorie_series(&store, self.device_id, &found, now())?
            .into_iter()
            .map(|(reading, source)| Point {
                at_ms: reading.at.0,
                value: reading.total.0,
                origin: origin_of(source),
            })
            .collect())
    }

    pub fn windowed_max(
        &self,
        metric: Metric,
        edges_ms: Vec<i64>,
    ) -> Result<Vec<Option<f64>>, WatchError> {
        let edges = edges_of(edges_ms)?;
        let store = self.store.lock().unwrap();
        if metric != Metric::Calories {
            return Ok(store
                .windowed_max(self.device_id, metric.kind(), &edges)?
                .into_iter()
                .map(|found| found.map(|value| metric.reading(value)))
                .collect());
        }
        Ok(report::calories_windowed_max(&store, self.device_id, &edges, now())?
            .into_iter()
            .map(|found| found.map(|total| total.0))
            .collect())
    }

    pub fn latest_value(&self, metric: Metric) -> Result<Option<Point>, WatchError> {
        let store = self.store.lock().unwrap();
        if metric == Metric::Calories {
            let latest = report::latest_calories(&store, self.device_id, now())?;
            return Ok(latest.map(|reading| Point {
                at_ms: reading.at.0,
                value: reading.total.0,
                origin: Origin::Stored,
            }));
        }
        Ok(store.latest(self.device_id, metric.kind())?.map(|latest| Point {
            at_ms: latest.at.0,
            value: metric.reading(latest.value),
            origin: Origin::Stored,
        }))
    }

    pub fn extent(&self, metric: Metric) -> Result<Option<Extent>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .extent(self.device_id, metric.kind())?
            .map(|(from, to)| Extent {
                from_ms: from.0,
                to_ms: to.0,
            }))
    }

    pub fn workouts(&self, limit: u32) -> Result<Vec<WorkoutSummary>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(report::workouts(&store, self.device_id, limit, now())?
            .into_iter()
            .map(workout_summary)
            .collect())
    }

    /// Every session the span touches, one still running included.
    pub fn workouts_between(
        &self,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<Vec<WorkoutSummary>, WatchError> {
        let store = self.store.lock().unwrap();
        let found = report::workouts_between(
            &store,
            self.device_id,
            UnixMillis(from_ms),
            UnixMillis(to_ms),
            now(),
        )?;
        Ok(found.into_iter().map(workout_summary).collect())
    }

    /// Hours of each window the watch measured a heart rate in, which is the
    /// hours it was on a wrist.
    pub fn worn_hours(&self, edges_ms: Vec<i64>) -> Result<Vec<u32>, WatchError> {
        let edges = edges_of(edges_ms)?;
        let store = self.store.lock().unwrap();
        Ok(store.windowed_hours(self.device_id, SampleKind::HeartRate, &edges)?)
    }

    pub fn delete_workout(&self, id: i64) -> Result<(), WatchError> {
        let store = self.store.lock().unwrap();
        store.delete_workout(self.device_id, WorkoutId(id))?;
        drop(store);
        self.transport.changed();
        Ok(())
    }

    /// Answers with the session as it now stands: the figures over it are read
    /// back out of the samples the shorter span covers, not carried over.
    pub fn trim_workout(&self, id: i64, ended_at_ms: i64) -> Result<WorkoutSummary, WatchError> {
        let store = self.store.lock().unwrap();
        let ended_at = UnixMillis(ended_at_ms).to_seconds();
        if store.trim_workout(self.device_id, WorkoutId(id), ended_at)? == Trim::OutsideSession {
            return Err(WatchError::Refused {
                reason: format!("{ended_at_ms} is not inside session {id}"),
            });
        }
        let now = now();
        let Some(trimmed) = report::workout(&store, self.device_id, WorkoutId(id), now)? else {
            return Err(WatchError::Refused {
                reason: format!("no session {id}"),
            });
        };
        drop(store);
        self.transport.changed();
        Ok(workout_summary(trimmed))
    }

    pub fn activity_totals(&self, from_ms: i64, to_ms: i64) -> Result<ActivityTotals, WatchError> {
        let store = self.store.lock().unwrap();
        let minutes = store.activity_minutes(
            self.device_id,
            UnixMillis(from_ms).to_seconds(),
            UnixMillis(to_ms).to_seconds(),
        )?;
        let summed = activity::totals(&minutes);
        Ok(ActivityTotals {
            steps: summed.steps,
            distance_metres: summed.distance.0,
            ascent_metres: summed.ascent.0,
            calories: summed.calories.0,
        })
    }

    /// Metres climbed in each window the watch reports one for. It gives a
    /// climb per minute and never a descent, so there is no height above
    /// anything to be had from it — only how steeply the ground rose.
    pub fn ascent_series(&self, from_ms: i64, to_ms: i64) -> Result<Vec<Point>, WatchError> {
        let store = self.store.lock().unwrap();
        let climbed = report::ascents(&store, self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))?;
        drop(store);

        Ok(climbed
            .into_iter()
            .map(|(at, ascent)| Point {
                at_ms: at.0,
                value: ascent.0,
                origin: Origin::Stored,
            })
            .collect())
    }

    pub fn detected_activities(
        &self,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<Vec<DetectedActivity>, WatchError> {
        let store = self.store.lock().unwrap();
        let found = report::detected_activities(
            &store,
            self.device_id,
            UnixMillis(from_ms),
            UnixMillis(to_ms),
        )?;
        Ok(found
            .into_iter()
            .map(|session| {
                let kind = SportId(session.subcategory as u32);
                DetectedActivity {
                    started_at_ms: session.started_at.to_millis().0,
                    ended_at_ms: session.ended_at.to_millis().0,
                    subcategory: session.subcategory as i32,
                    activity: sport::name(kind),
                    on_foot: sport::on_foot(kind),
                    travel: travel(kind),
                    steps: session.steps,
                    distance_metres: session.distance.0,
                    calories: session.calories.0,
                }
            })
            .collect())
    }

    pub fn screens(&self) -> Vec<WatchScreen> {
        let current = self.inner.lock().unwrap().client.screens();
        let Some(current) = current else {
            return Vec::new();
        };
        let mut screens: Vec<WatchScreen> = current
            .iter()
            .map(|id| WatchScreen {
                id: *id,
                name: screen_name(*id),
                enabled: true,
            })
            .collect();
        for (id, name) in KNOWN_SCREENS {
            if !current.contains(id) {
                screens.push(WatchScreen {
                    id: *id,
                    name: name.to_string(),
                    enabled: false,
                });
            }
        }
        screens
    }

    pub fn request_screens(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.request_screens(now());
        self.dispatch(actions)
    }

    pub fn set_screens(&self, ids: Vec<u8>) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.set_screens(&ids, now());
        self.dispatch(actions)
    }

    pub fn set_user(
        &self,
        birth_secs: i64,
        weight_grams: u32,
        height_cm: u32,
    ) -> Result<(), WatchError> {
        let held = self
            .store
            .lock()
            .unwrap()
            .watch_user(self.device_id)?
            .ok_or_else(|| WatchError::Protocol {
                reason: "the watch has not reported a profile to edit yet".to_string(),
            })?;
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .set_user(&wpp::client::UserProfile {
                birth: birth_secs as i32,
                weight: weight_grams,
                height: height_cm,
                ..held
            }, now());
        self.dispatch(actions)
    }

    pub fn start_workout(&self, activity: u32) -> Result<(), WatchError> {
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .start_workout(activity as i16, now());
        self.dispatch(actions)
    }

    /// The route so far as the watch's own screens want it: distance in the
    /// centimetres they scale to kilometres, speed already converted to the
    /// kilometres an hour they print unaltered. Nothing is sent until there is
    /// a distance, which is what the screens gate on.
    pub fn push_route_to_watch(&self) -> Result<(), WatchError> {
        let Some(active) = self.store.lock().unwrap().active_workout(self.device_id)? else {
            return Ok(());
        };
        let at = now();
        let started = active.started_at.to_millis();
        let mut held = self.live_distance.lock().unwrap();
        let carried = held
            .take()
            .filter(|live| live.workout_started_at == active.started_at);
        let from = carried
            .as_ref()
            .map_or(started, |live| live.settled_through);
        let settled_metres = carried.as_ref().map_or(0.0, |live| live.settled_metres);

        let Some(track) = self.track(from.0, at.0, active.subcategory as i32)? else {
            return Ok(());
        };
        let split = track.track.distance_split(at - SETTLE);
        *held = Some(LiveDistance {
            workout_started_at: active.started_at,
            settled_metres: settled_metres + split.settled.0,
            settled_through: split.settled_through.unwrap_or(from),
        });
        drop(held);

        let distance_cm =
            ((settled_metres + split.settled.0 + split.tail.0) * 100.0).round() as i32;
        if distance_cm == 0 {
            return Ok(());
        }
        let speed_kph = track
            .track
            .speed_series()
            .last()
            .map(|(_, speed)| (speed.0 * 3.6).round() as i32);
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .workout_live_data(wpp::client::LiveRoute {
                pace: None,
                distance: Some(distance_cm),
                speed: speed_kph,
            });
        self.dispatch(actions)
    }

    pub fn refresh_workout_status(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.workout_status(now());
        self.dispatch(actions)
    }

    pub fn set_gps_status(&self, present: bool) -> Result<(), WatchError> {
        let status = if present {
            wpp::client::GpsStatus::Present
        } else {
            wpp::client::GpsStatus::Absent
        };
        let actions = self.inner.lock().unwrap().client.gps_status(status);
        self.dispatch(actions)
    }

    /// A field left as None is left out of the frame. The watch acknowledges
    /// nothing here, so a success is only that the frame went out.
    pub fn push_live_route(
        &self,
        speed: Option<i32>,
        distance: Option<i32>,
        pace: Option<i32>,
    ) -> Result<(), WatchError> {
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .workout_live_data(wpp::client::LiveRoute {
                pace,
                distance,
                speed,
            });
        self.dispatch(actions)
    }

    pub fn stop_workout(&self) -> Result<(), WatchError> {
        let Some(active) = self.store.lock().unwrap().active_workout(self.device_id)? else {
            return Ok(());
        };
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .stop_workout(active.started_at, now());
        self.dispatch(actions)
    }

    pub fn wear_position(&self) -> WearPosition {
        let Some(reported) = self.inner.lock().unwrap().client.wear_position() else {
            return WearPosition::NotSet;
        };
        WearPosition::of(reported).unwrap_or_else(|| {
            self.note_unreadable(&[format!("wear position {reported} is not one this app knows")]);
            WearPosition::NotSet
        })
    }

    pub fn set_wear_position(&self, position: WearPosition) -> Result<(), WatchError> {
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .set_wear_position(position.wire(), now());
        self.dispatch(actions)
    }

    pub fn set_zone(
        &self,
        gmt_offset: i32,
        next_change: Option<DstChange>,
    ) -> Result<(), WatchError> {
        let zone = wpp::client::Zone {
            gmt_offset,
            next_change: next_change.map(|c| wpp::client::DstChange {
                at: UnixMillis(c.at_ms).to_seconds(),
                gmt_offset: c.gmt_offset_seconds,
            }),
        };
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .set_zone(zone, now());
        self.dispatch(actions)
    }

    pub fn phone_clock_changed(&self) -> Result<(), WatchError> {
        let actions = self
            .inner
            .lock()
            .unwrap()
            .client
            .phone_clock_changed(now());
        self.dispatch(actions)
    }

    pub fn request_device_config(&self) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.request_device_config(now());
        self.dispatch(actions)
    }

    /// Debug: starts a read of `len` bytes of external SPI flash at `addr`. The
    /// bytes arrive over the link asynchronously, so the caller polls
    /// [`WatchService::spi_flash_progress`] and takes them with
    /// [`WatchService::spi_flash_take`]. The watch reads in 16-byte units, so
    /// `len` should be a multiple of 16.
    pub fn spi_flash_read(&self, addr: u32, len: u32) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.spi_flash_read(addr, len);
        self.dispatch(actions)
    }

    pub fn spi_flash_progress(&self) -> Option<SpiFlashProgress> {
        self.inner
            .lock()
            .unwrap()
            .client
            .spi_flash_progress()
            .map(SpiFlashProgress::from)
    }

    pub fn spi_flash_take(&self) -> Option<Vec<u8>> {
        self.inner.lock().unwrap().client.spi_flash_take()
    }

    pub fn spi_flash_reset(&self) {
        self.inner.lock().unwrap().client.spi_flash_reset();
    }

    pub fn activities(&self) -> Vec<Activity> {
        let current = self
            .inner
            .lock()
            .unwrap()
            .client
            .activities()
            .unwrap_or_default();
        let mut activities: Vec<Activity> = current
            .iter()
            .map(|id| Activity {
                id: *id,
                name: sport::name(SportId(*id)),
                enabled: true,
            })
            .collect();
        for known in sport::ACTIVITIES {
            if !current.contains(&known.id.0) {
                activities.push(Activity {
                    id: known.id.0,
                    name: known.name.to_string(),
                    enabled: false,
                });
            }
        }
        activities
    }

    /// What the watch's menu holds, or None until it has said. [`Self::activities`]
    /// lists every sport either way, so it cannot tell an empty menu from an
    /// unread one, and saving an unread one would overwrite the watch's.
    pub fn activity_menu(&self) -> Option<Vec<u32>> {
        self.inner.lock().unwrap().client.activities()
    }

    pub fn set_activities(&self, ids: Vec<u32>) -> Result<(), WatchError> {
        let formats = self.inner.lock().unwrap().client.image_formats().to_vec();
        if formats.is_empty() {
            return Err(WatchError::Protocol {
                reason: "the watch has not said what size glyphs it wants yet".to_string(),
            });
        }
        let screens: Vec<wpp::client::WorkoutScreen> = ids
            .iter()
            .map(|id| {
                let face = sport::face(SportId(*id));
                let glyphs = formats
                    .iter()
                    .map(|format| {
                        let drawn =
                            self.rasterizer
                                .activity_glyph(*id, format.width, format.height);
                        Ok((format.kind, drawn.pack()?))
                    })
                    .collect::<Result<_, WatchError>>()?;
                Ok(wpp::client::WorkoutScreen {
                    id: *id,
                    name: sport::name(SportId(*id)),
                    face_mode: face.mode,
                    flag: face.flag,
                    glyphs,
                })
            })
            .collect::<Result<_, WatchError>>()?;
        let actions = self.inner.lock().unwrap().client.set_activities(&screens, now());
        self.dispatch(actions)
    }

    pub fn health_features(&self) -> Vec<HealthFeature> {
        let enabled = self.enabled_features();
        HEALTH_FEATURES
            .iter()
            .map(|(id, name, description)| HealthFeature {
                id: *id,
                name: name.to_string(),
                description: description.to_string(),
                enabled: enabled.iter().any(|f| f.id == FeatureId(*id)),
            })
            .collect()
    }

    pub fn set_health_feature(&self, id: u16, enabled: bool) -> Result<(), WatchError> {
        let id = FeatureId(id);
        let mut features = self.enabled_features();
        features.retain(|f| f.id != id);
        if enabled {
            features.push(Feature {
                id,
                schedule: FeatureSchedule::Permanent,
            });
        }
        self.commit_features(features)
    }

    /// Reports the respiratory scan's window if one is still running. The scan
    /// is never left on: it is armed a night at a time, so a caller that wants
    /// another one calls [`WatchService::arm_respiratory_scan`] again.
    pub fn respiratory_scan(&self) -> Option<ArmedScan> {
        self.enabled_features()
            .iter()
            .find(|f| f.id == RESPIRATORY_SCAN_MONITORING)
            .and_then(|f| match f.schedule {
                FeatureSchedule::Window { end, .. } => Some(ArmedScan { ends_at: end.0 }),
                FeatureSchedule::Permanent => None,
            })
    }

    pub fn arm_respiratory_scan(&self) -> Result<(), WatchError> {
        let start = now().to_seconds();
        let mut features = self.enabled_features();
        features.retain(|f| f.id != RESPIRATORY_SCAN_MONITORING);
        features.push(Feature {
            id: RESPIRATORY_SCAN_MONITORING,
            schedule: FeatureSchedule::Window {
                start,
                end: start + SCAN_WINDOW,
            },
        });
        self.commit_features(features)
    }

    pub fn cancel_respiratory_scan(&self) -> Result<(), WatchError> {
        let mut features = self.enabled_features();
        features.retain(|f| f.id != RESPIRATORY_SCAN_MONITORING);
        self.commit_features(features)
    }

    pub fn notification_config(&self) -> Option<NotificationConfig> {
        self.inner
            .lock()
            .unwrap()
            .client
            .notifications()
            .map(|c| NotificationConfig {
                accepted: c.accepted,
                displayed: c.displayed,
            })
    }

    pub fn set_notifications(&self, enabled: bool) -> Result<(), WatchError> {
        let actions = self.inner.lock().unwrap().client.set_notifications(enabled, now());
        self.dispatch(actions)
    }

    pub fn prefer_notifications(&self, enabled: bool) {
        self.inner
            .lock()
            .unwrap()
            .client
            .prefer_notifications(enabled);
    }

    pub fn post_notification(
        &self,
        app_id: String,
        title: String,
        subtitle: String,
        message: String,
        category: NotificationCategory,
    ) -> u32 {
        let (id, announcement) = self.notifications.lock().unwrap().post(
            app_id,
            title,
            subtitle,
            message,
            category.into(),
        );
        self.ancs.announce(announcement.to_vec());
        id.0
    }

    pub fn dismiss_notification(&self, id: u32) {
        let announcement = self
            .notifications
            .lock()
            .unwrap()
            .dismiss(NotificationId(id));
        if let Some(announcement) = announcement {
            self.ancs.announce(announcement.to_vec());
        }
    }

    pub fn on_ancs_write(&self, bytes: Vec<u8>, max_payload: u32) -> Result<(), WatchError> {
        if max_payload == 0 {
            return Err(WatchError::Protocol {
                reason: "a data source fragment has to carry something".into(),
            });
        }
        let request =
            wpp::ancs::ControlPoint::parse(&bytes).map_err(|err| WatchError::Protocol {
                reason: err.to_string(),
            })?;
        let response = {
            let center = self.notifications.lock().unwrap();
            let Some(notification) = center.get(request.id) else {
                return Ok(());
            };
            request.response(notification)
        };
        for fragment in wpp::ancs::fragments(&response, max_payload as usize) {
            self.ancs.attributes(fragment);
        }
        Ok(())
    }

    pub fn mark_set(&self, at_ms: i64, edge: SetEdge) -> Result<(), WatchError> {
        let store = self.store.lock().unwrap();
        store.mark_set(
            self.device_id,
            UnixMillis(at_ms),
            match edge {
                SetEdge::Start => SetBoundary::Start,
                SetEdge::End => SetBoundary::End,
            },
        )?;
        drop(store);
        self.transport.changed();
        Ok(())
    }

    pub fn record_fixes(&self, fixes: Vec<LocationFix>) -> Result<(), WatchError> {
        if fixes.is_empty() {
            return Ok(());
        }
        let held: Vec<wpp::track::Fix> = fixes.iter().map(|fix| fix.held()).collect();
        let mut store = self.store.lock().unwrap();
        store.record_fixes(self.device_id, &held)?;
        drop(store);
        self.transport.changed();
        Ok(())
    }

    /// When the running session is one that covers ground, the moment it
    /// started. What the phone's receiver is switched on and off by, and cheap
    /// enough to ask on every change: one indexed row.
    pub fn route_recording_since(&self) -> Result<Option<i64>, WatchError> {
        let store = self.store.lock().unwrap();
        let Some(active) = store.active_workout(self.device_id)? else {
            return Ok(None);
        };
        match travel(SportId(active.subcategory as u32)) {
            Travel::Ground => Ok(Some(active.started_at.0 * 1000)),
            Travel::InPlace => Ok(None),
        }
    }

    /// None where the activity covers no ground, and where it does but nothing
    /// was recorded: an empty route and no route are the same to every caller.
    pub fn track(
        &self,
        from_ms: i64,
        to_ms: i64,
        subcategory: i32,
    ) -> Result<Option<Arc<RouteTrack>>, WatchError> {
        let Some(limits) = sport::limits(SportId(subcategory as u32)) else {
            return Ok(None);
        };
        let store = self.store.lock().unwrap();
        let fixes = store.track_points(self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))?;
        drop(store);

        let cleaned = track::clean(&fixes, limits);
        if cleaned.is_empty() {
            return Ok(None);
        }
        Ok(Some(Arc::new(RouteTrack { track: cleaned })))
    }

    pub fn night(&self, from_ms: i64, to_ms: i64) -> Result<Night, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(night_record(report::night(
            &store,
            self.device_id,
            UnixMillis(from_ms),
            UnixMillis(to_ms),
        )?))
    }

    /// One entry for each window the watch staged any sleep in; a window it
    /// did not is left out rather than reported as a night of none.
    pub fn night_trend(&self, windows: Vec<SleepSpan>) -> Result<Vec<NightTrend>, WatchError> {
        let windows: Vec<std::ops::Range<UnixMillis>> = windows
            .iter()
            .map(|window| UnixMillis(window.from_ms)..UnixMillis(window.to_ms))
            .collect();
        let store = self.store.lock().unwrap();
        Ok(report::night_trend(&store, self.device_id, &windows)?
            .into_iter()
            .map(|night| NightTrend {
                asleep_from_ms: night.asleep.start.0,
                asleep_to_ms: night.asleep.end.0,
                asleep_ms: night.asleep_for.as_millis(),
                score: night.score.map(sleep_score),
                heart: night.heart.map(|heart| NightHeart {
                    median_bpm: heart.median.0,
                    resting_bpm: heart.resting.0,
                }),
            })
            .collect())
    }

    /// When the first session of any kind was recorded: the watch keeps no
    /// workouts from before it was paired, so there is no knowing what was
    /// done before then.
    pub fn first_workout_at_ms(&self) -> Result<Option<i64>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .first_workout(self.device_id)?
            .map(|at| at.to_millis().0))
    }

    /// Finished sessions of one activity, oldest first.
    pub fn workout_trend(
        &self,
        subcategory: i32,
        from_ms: i64,
        to_ms: i64,
    ) -> Result<Vec<WorkoutTrend>, WatchError> {
        let store = self.store.lock().unwrap();
        let trend = report::workout_trend(
            &store,
            self.device_id,
            SportId(subcategory as u32),
            UnixMillis(from_ms),
            UnixMillis(to_ms),
        )?;
        Ok(trend
            .into_iter()
            .map(|entry| WorkoutTrend {
                workout: workout_summary(entry.workout),
                heart: entry.effort.map(|effort| SessionHeart {
                    median_bpm: effort.median.0,
                    p95_bpm: effort.p95.0,
                    measured_ms: effort.measured_ms,
                    dropout_ms: effort.dropout_ms,
                }),
            })
            .collect())
    }

    /// A few minutes awake in the middle of a night leave the body on its
    /// sleeping baseline, so a stretch survives them as one span.
    pub fn sleep_spans(&self, from_ms: i64, to_ms: i64) -> Result<Vec<SleepSpan>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(report::asleep_spans(&store, self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))?
            .into_iter()
            .map(|span| SleepSpan {
                from_ms: span.start.0,
                to_ms: span.end.0,
            })
            .collect())
    }

    /// The median waking rate of each window, readings taken asleep, in a
    /// session or while recovering from one left out. None for a window with
    /// nothing left.
    /// Milliseconds in each heart rate zone over the sessions that started in
    /// each window, every kind of session together. Only sessions are
    /// counted: outside one the watch reads a rate every ten minutes, too
    /// seldom to say how long any of it lasted.
    pub fn zone_time(
        &self,
        edges_ms: Vec<i64>,
        floors_bpm: Vec<u16>,
    ) -> Result<Vec<Vec<i64>>, WatchError> {
        let edges = edges_of(edges_ms)?;
        let floors: Vec<Bpm> = floors_bpm.into_iter().map(Bpm).collect();
        let store = self.store.lock().unwrap();
        Ok(report::zone_time(&store, self.device_id, &edges, &floors)?
            .into_iter()
            .map(|window| window.into_iter().map(|spent| spent.as_millis()).collect())
            .collect())
    }

    pub fn add_note(&self, at_ms: i64, text: String) -> Result<i64, WatchError> {
        let text = text.trim();
        if text.is_empty() {
            return Err(WatchError::Refused {
                reason: "a note needs some text".to_string(),
            });
        }
        let store = self.store.lock().unwrap();
        Ok(store.add_note(UnixMillis(at_ms), text)?)
    }

    pub fn notes(&self) -> Result<Vec<Note>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .notes()?
            .into_iter()
            .map(|note| Note {
                id: note.id,
                at_ms: note.at.0,
                text: note.text,
            })
            .collect())
    }

    pub fn delete_note(&self, id: i64) -> Result<(), WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store.delete_note(id)?)
    }

    pub fn awake_heart(&self, edges_ms: Vec<i64>) -> Result<Vec<Option<AwakeHeart>>, WatchError> {
        let edges = edges_of(edges_ms)?;
        let store = self.store.lock().unwrap();
        Ok(report::awake_heart(&store, self.device_id, &edges)?
            .into_iter()
            .map(|window| {
                window.map(|rest| AwakeHeart {
                    median_bpm: rest.median.0,
                    readings: rest.readings,
                })
            })
            .collect())
    }

    pub fn has_staging(&self, from_ms: i64, to_ms: i64) -> Result<bool, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store.has_staging(
            self.device_id,
            UnixMillis(from_ms).to_seconds(),
            UnixMillis(to_ms).to_seconds(),
        )?)
    }

    pub fn charging(&self, from_ms: i64, to_ms: i64) -> Result<Vec<Marker>, WatchError> {
        let store = self.store.lock().unwrap();
        let mut out = Vec::new();
        for period in store.charge_periods(self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))? {
            out.push(Marker {
                at_ms: period.from.0,
                edge: SetEdge::Start,
            });
            if let Some(end) = period.to {
                out.push(Marker {
                    at_ms: end.0,
                    edge: SetEdge::End,
                });
            }
        }
        Ok(out)
    }

    pub fn markers(&self, from_ms: i64, to_ms: i64) -> Result<Vec<Marker>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .markers(self.device_id, UnixMillis(from_ms), UnixMillis(to_ms))?
            .into_iter()
            .map(|marker| Marker {
                at_ms: marker.at.0,
                edge: match marker.boundary {
                    SetBoundary::Start => SetEdge::Start,
                    SetBoundary::End => SetEdge::End,
                },
            })
            .collect())
    }

    pub fn live_ecg(&self) -> Vec<f64> {
        let inner = self.inner.lock().unwrap();
        inner
            .client
            .live_samples()
            .iter()
            .map(|c| Millivolts::from_counts(*c).0)
            .collect()
    }

    pub fn ecgs(&self) -> Result<Vec<EcgSummary>, WatchError> {
        let store = self.store.lock().unwrap();
        let listed = report::ecg_summaries(&store, self.device_id)?;
        self.note_unreadable(&listed.unreadable);
        Ok(listed.summaries.iter().map(ecg_summary).collect())
    }

    pub fn afib_episodes(&self) -> Result<Vec<AfibEpisode>, WatchError> {
        let store = self.store.lock().unwrap();
        Ok(store
            .afib_episodes(self.device_id)?
            .into_iter()
            .map(|row| AfibEpisode {
                id: row.id,
                measured_at_ms: row.measured_at.0,
                seconds: row.duration.as_secs() as u32,
                rhythm: row.verdict.and_then(ppg_rhythm),
                alerted: row.attrib.map(|a| a == STORED_MEASURE_ATTRIB_CONFIRMED),
            })
            .collect())
    }

    pub fn afib_alert(&self, id: i64) -> Result<Option<AfibAlert>, WatchError> {
        use wpp::signal::PpgAfibRule;
        let store = self.store.lock().unwrap();
        let Some(alert) = report::afib_alert(&store, self.device_id, id)? else {
            return Ok(None);
        };
        self.note_unreadable(&alert.unreadable_readings);
        self.note_unreadable(&alert.unreadable_ecgs);
        Ok(Some(AfibAlert {
            id,
            measured_at_ms: alert.measured_at.0,
            run: PpgAfibRule::RUN as u32,
            window_hours: (PpgAfibRule::WINDOW_SECS / 3600) as u32,
            counted: alert.counted,
            empty: alert.empty,
            readings: alert.readings.into_iter().map(afib_reading).collect(),
            ecg_after: alert.ecg_after.as_ref().map(ecg_summary),
        }))
    }

    pub fn ecg(&self, id: i64) -> Result<Option<EcgRecording>, WatchError> {
        let store = self.store.lock().unwrap();
        let Some(detail) = report::ecg(&store, EcgId(id))? else {
            return Ok(None);
        };
        let recording = &detail.stored.recording;
        let leads = recording
            .leads()
            .iter()
            .enumerate()
            .map(|(i, column)| EcgLead {
                name: column
                    .lead
                    .map_or_else(|| format!("CH{i}"), |lead| lead.name().to_string()),
                millivolts: column
                    .samples
                    .iter()
                    .map(|c| Millivolts::from_counts(*c).0)
                    .collect(),
            })
            .collect();
        Ok(Some(EcgRecording {
            id,
            measured_at_ms: detail.stored.measured_at.0,
            sampling_hz: u32::from(recording.rate.get()),
            leads,
            heart_rate: detail.verdict.rate.map(|bpm| u32::from(bpm.0)),
            rhythm: detail.verdict.rhythm.map(ecg_rhythm),
            analysis: ecg_analysis(&detail.reading),
        }))
    }
}

/// None for edges out of order, which only a broken caller can send.
fn edges_of(edges_ms: Vec<i64>) -> Result<wpp::units::Edges, WatchError> {
    wpp::units::Edges::new(edges_ms.into_iter().map(UnixMillis).collect()).ok_or_else(|| {
        WatchError::Refused {
            reason: "window edges must be in time order".to_string(),
        }
    })
}



impl WatchService {
    fn note_unreadable(&self, rows: &[impl std::fmt::Display]) {
        let mut unreadable = self.unreadable_rows.lock().unwrap();
        for row in rows {
            unreadable.note(row);
        }
    }

    fn enabled_features(&self) -> Vec<Feature> {
        self.inner.lock().unwrap().client.features(now().to_seconds())
    }

    /// The store is the only record of the set, so it is written before the
    /// watch: a write that never reaches the watch is re-asserted on the next
    /// connection, where the reverse would lose the change entirely.
    fn commit_features(&self, features: Vec<Feature>) -> Result<(), WatchError> {
        self.store
            .lock()
            .unwrap()
            .set_features(self.device_id, &features)?;
        let actions = self.inner.lock().unwrap().client.set_features(features, now());
        self.dispatch(actions)
    }

    fn dispatch(&self, actions: Vec<Action>) -> Result<(), WatchError> {
        let mut actions: std::collections::VecDeque<Action> = actions.into();
        let mut changed = false;
        let mut failure: Option<WatchError> = None;
        let mut unkept = false;
        while let Some(action) = actions.pop_front() {
            match action {
                Action::Send(frame) | Action::Delete(frame) => {
                    if let Err(error) = self.write(&frame) {
                        failure.get_or_insert(error);
                    }
                }
                Action::Finished => changed = true,
                Action::Reconnect => self.transport.reconnect(),
                // A token acknowledges every record before it, so nothing later
                // may be acknowledged past one that was not kept.
                Action::Store { .. } if unkept => {}
                Action::Store { token, records } => {
                    let stored = self.store.lock().unwrap().store(self.device_id, &records);
                    let unstarted = match stored {
                        Ok(unstarted) => unstarted,
                        Err(error) => {
                            failure.get_or_insert(error.into());
                            unkept = true;
                            let refetch = self.inner.lock().unwrap().client.handle(Event::StoreFailed);
                            actions.extend(refetch);
                            continue;
                        }
                    };
                    self.note_unreadable(&unstarted);
                    changed = true;
                    let released = self
                        .inner
                        .lock()
                        .unwrap()
                        .client
                        .handle(Event::Stored { token });
                    actions.extend(released);
                }
            }
        }

        let kept = self.inner.lock().unwrap().client.take_kept_watermarks();
        if !kept.is_empty() {
            self.store
                .lock()
                .unwrap()
                .set_watermarks(self.device_id, &kept)?;
        }

        if changed {
            self.transport.changed();
        }
        failure.map_or(Ok(()), Err)
    }

    fn write(&self, message: &Frame) -> Result<(), WatchError> {
        let wire = message.to_wire().map_err(|error| WatchError::Protocol {
            reason: error.to_string(),
        })?;
        self.transport.write(wire.iter().map(Frame::to_bytes).collect());
        Ok(())
    }

    fn draw(&self, frame: &Frame) -> Result<(), WatchError> {
        let glyph = GlyphRequest::parse(frame);
        let icon = IconRequest::parse(frame);
        if glyph.is_none() && icon.is_none() {
            return Ok(());
        }
        let formats = self.inner.lock().unwrap().client.image_formats().to_vec();

        if let Some(request) = glyph {
            let (width, height) = request.size();
            let bitmaps: Vec<Mono> = request
                .glyphs
                .iter()
                .map(|g| self.rasterizer.glyph(g.codepoint, width, height).pack())
                .collect::<Result<_, _>>()?;
            return self.write(&request.reply(&bitmaps));
        }
        if let Some(request) = icon {
            let (width, height) = request.size(&formats);
            let drawn = self
                .rasterizer
                .icon(request.app_id.clone(), width, height)
                .pack()?;
            return self.write(&request.reply(&drawn));
        }
        Ok(())
    }
}

fn now() -> UnixMillis {
    let since_epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("the clock reads before 1970");
    UnixMillis(since_epoch.as_millis() as i64)
}
