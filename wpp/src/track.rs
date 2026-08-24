//! Routes recorded by the phone during a workout, and everything read off
//! them. The watch has no receiver, so nothing here comes off the wire.

use crate::units::{Metres, MetresPerSecond, UnixMillis};

/// Seven decimal places of a degree, the resolution the rows are stored at.
pub const DEGREE_E7: f64 = 1e7;

/// IUGG mean radius. Which radius is used moves a kilometre by well under a
/// metre, far inside what the receiver itself is uncertain about.
const EARTH_RADIUS_M: f64 = 6_371_008.8;

const TILE_PX: f64 = 256.0;

/// Below this a body is standing still as far as a moving average cares; a
/// stationary receiver still wanders by a metre or two between fixes.
const MOVING_M_S: f64 = 0.5;

/// How far a simplified line may sit from the one it replaces. Under a pixel
/// the difference cannot be drawn, so the points are worth dropping.
const SIMPLIFY_PX: f64 = 1.0;

/// How hard a body changes its own speed, in metres per second per second, and
/// so how far the filter lets a fix pull it off the course it was holding. A
/// bike leaving a junction manages about this; set it lower and corners get
/// rounded off, higher and the line follows every reflection.
const ACCELERATION_M_S2: f64 = 1.0;

/// What a fix that reports no uncertainty is treated as having. A receiver
/// that will not say is not to be trusted much.
const UNKNOWN_ACCURACY_M: f64 = 30.0;

/// How wrong the first fix's velocity of zero may be, as a variance. Wide
/// enough that a session already under way is not dragged back by it.
const INITIAL_SPEED_VARIANCE: f64 = 100.0;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Fix {
    pub at: UnixMillis,
    pub lat_e7: i32,
    pub lon_e7: i32,
    pub altitude_cm: Option<i32>,
    pub accuracy_cm: Option<i32>,
    pub speed_mm_s: Option<i32>,
    pub bearing_cdeg: Option<i32>,
}

impl Fix {
    pub fn position(&self) -> Position {
        Position {
            lat: self.lat_e7 as f64 / DEGREE_E7,
            lon: self.lon_e7 as f64 / DEGREE_E7,
        }
    }

    pub fn accuracy(&self) -> Option<Metres> {
        self.accuracy_cm.map(|cm| Metres(cm as f64 / 100.0))
    }

    pub fn speed(&self) -> Option<MetresPerSecond> {
        self.speed_mm_s
            .map(|mm| MetresPerSecond(mm as f64 / 1000.0))
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Position {
    pub lat: f64,
    pub lon: f64,
}

impl Position {
    pub fn distance_to(self, other: Position) -> Metres {
        let lat1 = self.lat.to_radians();
        let lat2 = other.lat.to_radians();
        let dlat = (other.lat - self.lat).to_radians();
        let dlon = (other.lon - self.lon).to_radians();
        let a = (dlat / 2.0).sin().powi(2) + lat1.cos() * lat2.cos() * (dlon / 2.0).sin().powi(2);
        Metres(2.0 * EARTH_RADIUS_M * a.sqrt().asin())
    }
}

/// What a fix has to clear to be believed, and how long a silence breaks the
/// line. A ceiling per activity is the point: 12 m/s is a lost fix on a walk
/// and an ordinary descent on a bike.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Limits {
    pub accuracy_ceiling: Metres,
    pub speed_ceiling: MetresPerSecond,
    pub gap_secs: i64,
}

impl Limits {
    pub const DEFAULT_ACCURACY_M: f64 = 50.0;
    pub const DEFAULT_GAP_SECS: i64 = 30;
}

/// A run of fixes with no break in it. A route with no gaps is one segment,
/// which is why there is no separate shape for that case.
#[derive(Debug, Clone, PartialEq)]
pub struct Segment {
    pub fixes: Vec<Fix>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Track {
    pub segments: Vec<Segment>,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Bounds {
    pub north: f64,
    pub south: f64,
    pub east: f64,
    pub west: f64,
}

impl Bounds {
    pub fn centre(self) -> Position {
        Position {
            lat: (self.north + self.south) / 2.0,
            lon: (self.east + self.west) / 2.0,
        }
    }
}

/// Fixes must arrive in time order, which is how every caller reads them.
pub fn clean(fixes: &[Fix], limits: Limits) -> Track {
    let believable = fixes.iter().filter(|fix| match fix.accuracy() {
        Some(accuracy) => accuracy.0 <= limits.accuracy_ceiling.0,
        None => true,
    });

    let mut segments: Vec<Segment> = Vec::new();
    let mut current: Vec<Fix> = Vec::new();
    for fix in believable {
        let Some(previous) = current.last() else {
            current.push(*fix);
            continue;
        };
        let elapsed_ms = fix.at.0 - previous.at.0;
        if elapsed_ms <= 0 {
            continue;
        }
        if elapsed_ms > limits.gap_secs * 1000 {
            segments.push(Segment {
                fixes: std::mem::take(&mut current),
            });
            current.push(*fix);
            continue;
        }
        let metres = previous.position().distance_to(fix.position());
        if metres.0 / (elapsed_ms as f64 / 1000.0) > limits.speed_ceiling.0 {
            continue;
        }
        current.push(*fix);
    }
    if !current.is_empty() {
        segments.push(Segment { fixes: current });
    }
    Track {
        segments: segments
            .into_iter()
            .map(|segment| Segment {
                fixes: smooth(&segment.fixes),
            })
            .collect(),
    }
}

/// Where a body must have been, given where it was going and how sure the
/// receiver was of each fix. A constant-velocity Kalman filter run forwards and
/// then smoothed backwards over the whole run, which is what a recording allows
/// and a live position does not: every fix is judged against the ones after it
/// as well as before.
///
/// The two axes are filtered separately. Nothing in the model couples north to
/// east, so the four-state filter factors exactly into two of two states, and
/// every matrix in it is small enough to write out.
fn smooth(fixes: &[Fix]) -> Vec<Fix> {
    if fixes.len() < 3 {
        return fixes.to_vec();
    }
    let origin = fixes[0].position();
    let metres_per_degree = EARTH_RADIUS_M * std::f64::consts::PI / 180.0;
    let east_per_degree = metres_per_degree * origin.lat.to_radians().cos();

    let seconds: Vec<f64> = fixes
        .iter()
        .map(|fix| (fix.at.0 - fixes[0].at.0) as f64 / 1000.0)
        .collect();
    let variances: Vec<f64> = fixes
        .iter()
        .map(|fix| {
            let metres = fix.accuracy().map_or(UNKNOWN_ACCURACY_M, |a| a.0);
            metres * metres
        })
        .collect();

    let north: Vec<f64> = fixes
        .iter()
        .map(|fix| (fix.position().lat - origin.lat) * metres_per_degree)
        .collect();
    let east: Vec<f64> = fixes
        .iter()
        .map(|fix| (fix.position().lon - origin.lon) * east_per_degree)
        .collect();

    let north = smooth_axis(&seconds, &north, &variances);
    let east = smooth_axis(&seconds, &east, &variances);

    fixes
        .iter()
        .enumerate()
        .map(|(index, fix)| Fix {
            lat_e7: ((origin.lat + north[index] / metres_per_degree) * DEGREE_E7).round() as i32,
            lon_e7: ((origin.lon + east[index] / east_per_degree) * DEGREE_E7).round() as i32,
            ..*fix
        })
        .collect()
}

/// A symmetric two by two, which is all a position-and-velocity covariance is.
#[derive(Debug, Clone, Copy)]
struct Spread {
    pp: f64,
    pv: f64,
    vv: f64,
}

#[derive(Debug, Clone, Copy)]
struct State {
    position: f64,
    velocity: f64,
}

fn smooth_axis(seconds: &[f64], measured: &[f64], variances: &[f64]) -> Vec<f64> {
    let count = measured.len();
    let mut filtered = Vec::with_capacity(count);
    let mut predicted = Vec::with_capacity(count);

    let mut state = State {
        position: measured[0],
        velocity: 0.0,
    };
    let mut spread = Spread {
        pp: variances[0],
        pv: 0.0,
        vv: INITIAL_SPEED_VARIANCE,
    };
    filtered.push((state, spread));
    predicted.push((state, spread));

    for index in 1..count {
        let dt = seconds[index] - seconds[index - 1];
        let (ahead, reach) = predict(state, spread, dt);
        predicted.push((ahead, reach));

        let (settled, tightened) = correct(ahead, reach, measured[index], variances[index]);
        state = settled;
        spread = tightened;
        filtered.push((state, spread));
    }

    // Rauch-Tung-Striebel: walk back through what the filter believed at the
    // time, correcting each by what the rest of the run went on to show.
    let mut smoothed = vec![0.0; count];
    let mut behind = filtered[count - 1].0;
    smoothed[count - 1] = behind.position;
    for index in (0..count - 1).rev() {
        let (state, spread) = filtered[index];
        let (ahead, reach) = predicted[index + 1];
        let dt = seconds[index + 1] - seconds[index];

        // gain = P F' inv(P_ahead), with F' the transpose of the step forward.
        let Some(inverse) = invert(reach) else {
            smoothed[index] = state.position;
            behind = state;
            continue;
        };
        let fp = Spread {
            pp: spread.pp + dt * spread.pv,
            pv: spread.pv,
            vv: spread.vv,
        };
        let vp = spread.pv + dt * spread.vv;
        let gain = [
            fp.pp * inverse.pp + fp.pv * inverse.pv,
            fp.pp * inverse.pv + fp.pv * inverse.vv,
            vp * inverse.pp + spread.vv * inverse.pv,
            vp * inverse.pv + spread.vv * inverse.vv,
        ];

        let off_position = behind.position - ahead.position;
        let off_velocity = behind.velocity - ahead.velocity;
        behind = State {
            position: state.position + gain[0] * off_position + gain[1] * off_velocity,
            velocity: state.velocity + gain[2] * off_position + gain[3] * off_velocity,
        };
        smoothed[index] = behind.position;
    }
    smoothed
}

fn predict(state: State, spread: Spread, dt: f64) -> (State, Spread) {
    let noise = ACCELERATION_M_S2 * ACCELERATION_M_S2;
    (
        State {
            position: state.position + state.velocity * dt,
            velocity: state.velocity,
        },
        Spread {
            pp: spread.pp + 2.0 * dt * spread.pv + dt * dt * spread.vv + noise * dt.powi(4) / 4.0,
            pv: spread.pv + dt * spread.vv + noise * dt.powi(3) / 2.0,
            vv: spread.vv + noise * dt * dt,
        },
    )
}

fn correct(state: State, spread: Spread, measured: f64, variance: f64) -> (State, Spread) {
    let total = spread.pp + variance;
    if total <= 0.0 {
        return (state, spread);
    }
    let gain_position = spread.pp / total;
    let gain_velocity = spread.pv / total;
    let off = measured - state.position;
    (
        State {
            position: state.position + gain_position * off,
            velocity: state.velocity + gain_velocity * off,
        },
        Spread {
            pp: spread.pp - gain_position * spread.pp,
            pv: spread.pv - gain_position * spread.pv,
            vv: spread.vv - gain_velocity * spread.pv,
        },
    )
}

fn invert(spread: Spread) -> Option<Spread> {
    let determinant = spread.pp * spread.vv - spread.pv * spread.pv;
    if determinant.abs() < f64::EPSILON {
        return None;
    }
    Some(Spread {
        pp: spread.vv / determinant,
        pv: -spread.pv / determinant,
        vv: spread.pp / determinant,
    })
}

impl Segment {
    /// Displacement from the fix before, for a receiver that reported no speed
    /// of its own. The first fix of a run has nothing to measure against.
    fn derived_speed(&self, index: usize) -> Option<MetresPerSecond> {
        let previous = self.fixes.get(index.checked_sub(1)?)?;
        let fix = self.fixes.get(index)?;
        let elapsed = (fix.at.0 - previous.at.0) as f64 / 1000.0;
        if elapsed <= 0.0 {
            return None;
        }
        Some(MetresPerSecond(
            previous.position().distance_to(fix.position()).0 / elapsed,
        ))
    }

    pub fn distance(&self) -> Metres {
        Metres(
            self.fixes
                .windows(2)
                .map(|pair| pair[0].position().distance_to(pair[1].position()).0)
                .sum(),
        )
    }
}

impl Track {
    pub fn is_empty(&self) -> bool {
        self.segments.iter().all(|segment| segment.fixes.is_empty())
    }

    pub fn fixes(&self) -> impl Iterator<Item = &Fix> {
        self.segments
            .iter()
            .flat_map(|segment| segment.fixes.iter())
    }

    pub fn distance(&self) -> Metres {
        Metres(
            self.segments
                .iter()
                .map(|segment| segment.distance().0)
                .sum(),
        )
    }

    pub fn bounds(&self) -> Option<Bounds> {
        let mut found: Option<Bounds> = None;
        for fix in self.fixes() {
            let at = fix.position();
            found = Some(match found {
                None => Bounds {
                    north: at.lat,
                    south: at.lat,
                    east: at.lon,
                    west: at.lon,
                },
                Some(bounds) => Bounds {
                    north: bounds.north.max(at.lat),
                    south: bounds.south.min(at.lat),
                    east: bounds.east.max(at.lon),
                    west: bounds.west.min(at.lon),
                },
            });
        }
        found
    }

    /// Seconds spent actually going somewhere. A session's average speed over
    /// its whole length says more about how long it was paused at lights than
    /// about how it was ridden.
    pub fn moving_secs(&self) -> i64 {
        let millis: i64 = self
            .segments
            .iter()
            .flat_map(|segment| segment.fixes.windows(2))
            .filter(|pair| {
                let elapsed = (pair[1].at.0 - pair[0].at.0) as f64 / 1000.0;
                let metres = pair[0].position().distance_to(pair[1].position()).0;
                metres / elapsed >= MOVING_M_S
            })
            .map(|pair| pair[1].at.0 - pair[0].at.0)
            .sum();
        millis / 1000
    }

    pub fn average_speed(&self) -> Option<MetresPerSecond> {
        let seconds = self.moving_secs();
        if seconds == 0 {
            return None;
        }
        Some(MetresPerSecond(self.distance().0 / seconds as f64))
    }

    /// Doppler speed where the receiver reported it for every fix, and
    /// displacement over time where it did not. Mixing the two within one
    /// series would put a step in the line wherever the source changed.
    pub fn speed_series(&self) -> Vec<(UnixMillis, MetresPerSecond)> {
        run_of_three(self.raw_speed_series())
    }

    fn raw_speed_series(&self) -> Vec<(UnixMillis, MetresPerSecond)> {
        if self.fixes().all(|fix| fix.speed().is_some()) {
            return self
                .fixes()
                .map(|fix| (fix.at, fix.speed().expect("checked just above")))
                .collect();
        }
        self.segments
            .iter()
            .flat_map(|segment| segment.fixes.windows(2))
            .map(|pair| {
                let elapsed = (pair[1].at.0 - pair[0].at.0) as f64 / 1000.0;
                let metres = pair[0].position().distance_to(pair[1].position()).0;
                (pair[1].at, MetresPerSecond(metres / elapsed))
            })
            .collect()
    }

    /// The band the colour ramp runs over. Taken off the middle of the spread
    /// so one reflected fix reading 40 km/h does not flatten the whole route
    /// into the slow end of it.
    pub fn speed_band(&self) -> Option<(MetresPerSecond, MetresPerSecond)> {
        let mut speeds: Vec<f64> = self.speed_series().iter().map(|(_, s)| s.0).collect();
        if speeds.is_empty() {
            return None;
        }
        speeds.sort_by(|a, b| a.partial_cmp(b).expect("no NaN in a measured speed"));
        let at = |part: f64| speeds[((speeds.len() - 1) as f64 * part).round() as usize];
        Some((MetresPerSecond(at(0.05)), MetresPerSecond(at(0.95))))
    }
}

/// A reflected signal reads as a spike no body produced — forty kilometres an
/// hour in the middle of a city commute. The median of three throws one away
/// without smearing it across its neighbours the way an average would.
fn run_of_three(series: Vec<(UnixMillis, MetresPerSecond)>) -> Vec<(UnixMillis, MetresPerSecond)> {
    if series.len() < 3 {
        return series;
    }
    let mut smoothed = Vec::with_capacity(series.len());
    smoothed.push(series[0]);
    for window in series.windows(3) {
        let mut three = [window[0].1 .0, window[1].1 .0, window[2].1 .0];
        three.sort_by(|a, b| a.partial_cmp(b).expect("no NaN in a measured speed"));
        smoothed.push((window[1].0, MetresPerSecond(three[1])));
    }
    smoothed.push(series[series.len() - 1]);
    smoothed
}

/// Where the map is looking. Zoom is continuous so a pinch is not quantised to
/// the tile pyramid; the tiles drawn come from its whole part, scaled up by
/// the rest.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct View {
    pub centre: Position,
    pub zoom: f64,
    pub width_px: f64,
    pub height_px: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Tile {
    pub z: u32,
    pub x: u32,
    pub y: u32,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TilePlacement {
    pub tile: Tile,
    pub left_px: f64,
    pub top_px: f64,
    pub size_px: f64,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PathPoint {
    pub at: UnixMillis,
    pub x_px: f64,
    pub y_px: f64,
    pub speed: Option<MetresPerSecond>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub tiles: Vec<TilePlacement>,
    pub path: Vec<Vec<PathPoint>>,
    pub metres_per_pixel: f64,
}

pub const MIN_ZOOM: f64 = 1.0;
pub const MAX_ZOOM: f64 = 19.0;

/// World pixel coordinates at a given zoom: Web Mercator, the projection every
/// raster tile scheme is cut to.
fn project(at: Position, zoom: f64) -> (f64, f64) {
    let scale = TILE_PX * 2f64.powf(zoom);
    let x = (at.lon + 180.0) / 360.0 * scale;
    let lat = at.lat.to_radians().clamp(-1.4844222, 1.4844222);
    let y = (1.0 - (lat.tan() + 1.0 / lat.cos()).ln() / std::f64::consts::PI) / 2.0 * scale;
    (x, y)
}

fn unproject_lat(y: f64, zoom: f64) -> f64 {
    let scale = TILE_PX * 2f64.powf(zoom);
    let n = std::f64::consts::PI * (1.0 - 2.0 * y / scale);
    n.sinh().atan().to_degrees()
}

fn unproject_lon(x: f64, zoom: f64) -> f64 {
    let scale = TILE_PX * 2f64.powf(zoom);
    (x / scale * 360.0 - 180.0).clamp(-180.0, 180.0)
}

/// The closest zoom that still holds the whole route inside the box, with room
/// left over so the line does not run along the edge.
pub fn fit(track: &Track, width_px: f64, height_px: f64, padding_px: f64) -> View {
    let Some(bounds) = track.bounds() else {
        return View {
            centre: Position { lat: 0.0, lon: 0.0 },
            zoom: MIN_ZOOM,
            width_px,
            height_px,
        };
    };
    let usable_x = (width_px - 2.0 * padding_px).max(1.0);
    let usable_y = (height_px - 2.0 * padding_px).max(1.0);

    // Span at zoom 0, where the world is one tile across, then scaled up until
    // it fills the box: zoom is the log of that factor.
    let (west_x, north_y) = project(
        Position {
            lat: bounds.north,
            lon: bounds.west,
        },
        0.0,
    );
    let (east_x, south_y) = project(
        Position {
            lat: bounds.south,
            lon: bounds.east,
        },
        0.0,
    );
    let span_x = (east_x - west_x).max(f64::MIN_POSITIVE);
    let span_y = (south_y - north_y).max(f64::MIN_POSITIVE);
    let zoom = (usable_x / span_x)
        .min(usable_y / span_y)
        .log2()
        .clamp(MIN_ZOOM, MAX_ZOOM);

    View {
        centre: bounds.centre(),
        zoom,
        width_px,
        height_px,
    }
}

/// A drag moves the map under the finger, which is a move in pixels before it
/// is a move in degrees. Doing that conversion here is what keeps Mercator out
/// of the gesture handler.
pub fn pan(view: View, dx_px: f64, dy_px: f64) -> View {
    let (x, y) = project(view.centre, view.zoom);
    let scale = TILE_PX * 2f64.powf(view.zoom);
    let moved_y = (y - dy_px).clamp(0.0, scale);
    View {
        centre: Position {
            lat: unproject_lat(moved_y, view.zoom),
            lon: unproject_lon(x - dx_px, view.zoom),
        },
        ..view
    }
}

/// A pinch holds the ground under the fingers still, so the centre moves
/// unless they are already on it.
pub fn zoom_about(view: View, factor: f64, at_x_px: f64, at_y_px: f64) -> View {
    let zoom = (view.zoom + factor.log2()).clamp(MIN_ZOOM, MAX_ZOOM);
    let held_dx = at_x_px - view.width_px / 2.0;
    let held_dy = at_y_px - view.height_px / 2.0;
    let growth = 2f64.powf(zoom - view.zoom);

    let (x, y) = project(view.centre, zoom);
    let scale = TILE_PX * 2f64.powf(zoom);
    let moved_y = (y + held_dy * (growth - 1.0)).clamp(0.0, scale);
    View {
        centre: Position {
            lat: unproject_lat(moved_y, zoom),
            lon: unproject_lon(x + held_dx * (growth - 1.0), zoom),
        },
        zoom,
        ..view
    }
}

pub fn frame(track: &Track, view: View) -> Frame {
    let zoom = view.zoom.clamp(MIN_ZOOM, MAX_ZOOM);
    let (centre_x, centre_y) = project(view.centre, zoom);
    let left = centre_x - view.width_px / 2.0;
    let top = centre_y - view.height_px / 2.0;

    let path = track
        .segments
        .iter()
        .map(|segment| {
            let points: Vec<PathPoint> = segment
                .fixes
                .iter()
                .enumerate()
                .map(|(index, fix)| {
                    let (x, y) = project(fix.position(), zoom);
                    PathPoint {
                        at: fix.at,
                        x_px: x - left,
                        y_px: y - top,
                        speed: fix.speed().or_else(|| segment.derived_speed(index)),
                    }
                })
                .collect();
            simplify(&points, SIMPLIFY_PX)
        })
        .filter(|points| !points.is_empty())
        .collect();

    Frame {
        tiles: tiles_for(view, zoom, left, top),
        path,
        metres_per_pixel: metres_per_pixel(view.centre.lat, zoom),
    }
}

fn tiles_for(view: View, zoom: f64, left: f64, top: f64) -> Vec<TilePlacement> {
    let z = zoom.floor();
    // Tiles are cut for whole zooms; the fractional part is drawn as scale.
    let size_px = TILE_PX * 2f64.powf(zoom - z);
    let across = 1u32 << (z as u32);

    let first_x = (left / size_px).floor() as i64;
    let last_x = ((left + view.width_px) / size_px).floor() as i64;
    let first_y = ((top / size_px).floor() as i64).max(0);
    let last_y = (((top + view.height_px) / size_px).floor() as i64).min(across as i64 - 1);

    let mut placements = Vec::new();
    for y in first_y..=last_y {
        for x in first_x..=last_x {
            placements.push(TilePlacement {
                tile: Tile {
                    z: z as u32,
                    // Longitude wraps, so a view straddling the antimeridian
                    // asks for tiles either side of the seam by their own index.
                    x: x.rem_euclid(across as i64) as u32,
                    y: y as u32,
                },
                left_px: x as f64 * size_px - left,
                top_px: y as f64 * size_px - top,
                size_px,
            });
        }
    }
    placements
}

fn metres_per_pixel(lat: f64, zoom: f64) -> f64 {
    let equator = 2.0 * std::f64::consts::PI * EARTH_RADIUS_M;
    equator * lat.to_radians().cos() / (TILE_PX * 2f64.powf(zoom))
}

/// Ramer-Douglas-Peucker, iterative so a long route cannot overflow the stack.
fn simplify(points: &[PathPoint], tolerance_px: f64) -> Vec<PathPoint> {
    if points.len() < 3 {
        return points.to_vec();
    }
    let mut keep = vec![false; points.len()];
    keep[0] = true;
    keep[points.len() - 1] = true;

    let mut spans = vec![(0usize, points.len() - 1)];
    while let Some((first, last)) = spans.pop() {
        if last <= first + 1 {
            continue;
        }
        let mut worst = 0.0;
        let mut at = first;
        for (index, point) in points.iter().enumerate().take(last).skip(first + 1) {
            let away = perpendicular_px(*point, points[first], points[last]);
            if away > worst {
                worst = away;
                at = index;
            }
        }
        if worst <= tolerance_px {
            continue;
        }
        keep[at] = true;
        spans.push((first, at));
        spans.push((at, last));
    }

    points
        .iter()
        .zip(keep)
        .filter(|(_, kept)| *kept)
        .map(|(point, _)| *point)
        .collect()
}

fn perpendicular_px(point: PathPoint, from: PathPoint, to: PathPoint) -> f64 {
    let dx = to.x_px - from.x_px;
    let dy = to.y_px - from.y_px;
    let length = (dx * dx + dy * dy).sqrt();
    if length == 0.0 {
        let ax = point.x_px - from.x_px;
        let ay = point.y_px - from.y_px;
        return (ax * ax + ay * ay).sqrt();
    }
    ((point.x_px - from.x_px) * dy - (point.y_px - from.y_px) * dx).abs() / length
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fix(at_secs: i64, lat: f64, lon: f64) -> Fix {
        Fix {
            at: UnixMillis(at_secs * 1000),
            lat_e7: (lat * DEGREE_E7).round() as i32,
            lon_e7: (lon * DEGREE_E7).round() as i32,
            altitude_cm: None,
            accuracy_cm: Some(500),
            speed_mm_s: None,
            bearing_cdeg: None,
        }
    }

    fn limits() -> Limits {
        Limits {
            accuracy_ceiling: Metres(Limits::DEFAULT_ACCURACY_M),
            speed_ceiling: MetresPerSecond(20.0),
            gap_secs: Limits::DEFAULT_GAP_SECS,
        }
    }

    #[test]
    fn a_degree_of_latitude_measures_what_the_radius_says_it_should() {
        let along_meridian = Position {
            lat: 52.0,
            lon: 5.0,
        }
        .distance_to(Position {
            lat: 53.0,
            lon: 5.0,
        })
        .0;
        let expected = EARTH_RADIUS_M * std::f64::consts::PI / 180.0;
        assert!(
            (along_meridian - expected).abs() < 0.5,
            "{along_meridian} m against {expected} m"
        );
    }

    #[test]
    fn a_short_leg_agrees_with_the_flat_approximation_it_is_short_enough_for() {
        let from = Position {
            lat: 52.3791,
            lon: 4.9003,
        };
        let to = Position {
            lat: 52.3467,
            lon: 4.9179,
        };
        let metre_per_degree = EARTH_RADIUS_M * std::f64::consts::PI / 180.0;
        let north = (to.lat - from.lat) * metre_per_degree;
        let east = (to.lon - from.lon) * metre_per_degree * from.lat.to_radians().cos();
        let flat = (north * north + east * east).sqrt();

        assert!(
            (from.distance_to(to).0 - flat).abs() < 5.0,
            "{:?} against {flat} m",
            from.distance_to(to)
        );
    }

    #[test]
    fn a_fix_too_uncertain_to_believe_is_dropped() {
        let mut wild = fix(2, 52.3800, 4.9010);
        wild.accuracy_cm = Some(20_000);
        let track = clean(
            &[fix(1, 52.3791, 4.9003), wild, fix(3, 52.3793, 4.9005)],
            limits(),
        );

        assert_eq!(track.segments.len(), 1);
        assert_eq!(track.segments[0].fixes.len(), 2);
    }

    #[test]
    fn a_jump_no_body_could_make_is_dropped_without_breaking_the_line() {
        let track = clean(
            &[
                fix(1, 52.3791, 4.9003),
                fix(2, 52.4791, 4.9003),
                fix(3, 52.3792, 4.9004),
            ],
            limits(),
        );

        assert_eq!(track.segments.len(), 1, "a rejected fix is not a gap");
        assert_eq!(track.segments[0].fixes.len(), 2);
    }

    #[test]
    fn a_silence_longer_than_the_gap_breaks_the_line_in_two() {
        let track = clean(
            &[
                fix(1, 52.3791, 4.9003),
                fix(2, 52.3792, 4.9004),
                fix(600, 52.3800, 4.9010),
                fix(601, 52.3801, 4.9011),
            ],
            limits(),
        );

        assert_eq!(track.segments.len(), 2);
        assert_eq!(track.segments[0].fixes.len(), 2);
        assert_eq!(track.segments[1].fixes.len(), 2);
    }

    #[test]
    fn distance_does_not_count_the_gap_it_did_not_see() {
        let before = fix(1, 52.3791, 4.9003);
        let after = fix(600, 52.3900, 4.9100);
        let track = clean(
            &[
                before,
                fix(2, 52.3792, 4.9004),
                after,
                fix(601, 52.3901, 4.9101),
            ],
            limits(),
        );

        let across = before.position().distance_to(after.position()).0;
        assert!(across > 1_000.0, "the two runs are {across} m apart");
        assert!(
            track.distance().0 < 100.0,
            "{:?} counts the stretch it never saw",
            track.distance()
        );
    }

    #[test]
    fn standing_still_earns_no_moving_time() {
        let mut fixes = Vec::new();
        for second in 0..60 {
            fixes.push(fix(second, 52.3791, 4.9003));
        }
        let track = clean(&fixes, limits());

        assert_eq!(track.moving_secs(), 0);
        assert_eq!(track.average_speed(), None);
    }

    #[test]
    fn reported_speed_is_preferred_and_only_used_when_every_fix_has_one() {
        let mut with_speed = [fix(1, 52.3791, 4.9003), fix(2, 52.3792, 4.9004)];
        with_speed[0].speed_mm_s = Some(4_000);
        with_speed[1].speed_mm_s = Some(4_500);
        let reported = clean(&with_speed, limits()).speed_series();
        assert_eq!(reported.len(), 2);
        assert_eq!(reported[0].1, MetresPerSecond(4.0));

        let mut partial = with_speed;
        partial[1].speed_mm_s = None;
        let derived = clean(&partial, limits()).speed_series();
        assert_eq!(derived.len(), 1, "derived speed needs a pair of fixes");
        assert!(derived[0].1 .0 > 0.0);
    }

    #[test]
    fn fitting_a_route_puts_it_inside_the_box_and_centres_it() {
        let track = clean(
            &[
                fix(0, 52.3700, 4.8900),
                fix(30, 52.3720, 4.8930),
                fix(60, 52.3740, 4.8960),
            ],
            limits(),
        );
        assert_eq!(track.fixes().count(), 3, "the route the rest of this reads");
        let view = fit(&track, 400.0, 300.0, 16.0);
        let drawn = frame(&track, view);

        let points: Vec<&PathPoint> = drawn.path.iter().flatten().collect();
        assert!(!points.is_empty());
        for point in &points {
            assert!(
                point.x_px >= 15.0 && point.x_px <= 385.0,
                "{} is outside the padded box",
                point.x_px
            );
            assert!(
                point.y_px >= 15.0 && point.y_px <= 285.0,
                "{} is outside the padded box",
                point.y_px
            );
        }
    }

    #[test]
    fn the_tiles_asked_for_cover_the_whole_view() {
        let track = clean(
            &[fix(0, 52.3791, 4.9003), fix(30, 52.3800, 4.9010)],
            limits(),
        );
        let view = fit(&track, 400.0, 300.0, 16.0);
        let drawn = frame(&track, view);

        assert!(!drawn.tiles.is_empty());
        let leftmost = drawn
            .tiles
            .iter()
            .map(|t| t.left_px)
            .fold(f64::MAX, f64::min);
        let topmost = drawn
            .tiles
            .iter()
            .map(|t| t.top_px)
            .fold(f64::MAX, f64::min);
        let rightmost = drawn
            .tiles
            .iter()
            .map(|t| t.left_px + t.size_px)
            .fold(f64::MIN, f64::max);
        let bottommost = drawn
            .tiles
            .iter()
            .map(|t| t.top_px + t.size_px)
            .fold(f64::MIN, f64::max);

        assert!(leftmost <= 0.0 && topmost <= 0.0);
        assert!(rightmost >= 400.0 && bottommost >= 300.0);
    }

    #[test]
    fn projection_round_trips_through_the_pixel_it_lands_on() {
        for zoom in [2.0, 11.0, 16.5] {
            let (_, y) = project(
                Position {
                    lat: 52.3791,
                    lon: 4.9003,
                },
                zoom,
            );
            assert!(
                (unproject_lat(y, zoom) - 52.3791).abs() < 1e-6,
                "zoom {zoom}"
            );
        }
    }

    #[test]
    fn a_straight_run_of_fixes_simplifies_to_its_ends() {
        let mut fixes = Vec::new();
        for second in 0..50 {
            fixes.push(fix(second, 52.3700 + second as f64 * 0.0001, 4.8900));
        }
        let track = clean(&fixes, limits());
        let view = fit(&track, 400.0, 300.0, 16.0);

        assert_eq!(frame(&track, view).path[0].len(), 2);
    }

    #[test]
    fn simplifying_keeps_the_corner_that_makes_the_shape() {
        let track = clean(
            &[
                fix(0, 52.3700, 4.8900),
                fix(20, 52.3710, 4.8900),
                fix(40, 52.3710, 4.8920),
            ],
            limits(),
        );
        let view = fit(&track, 400.0, 300.0, 16.0);

        assert_eq!(frame(&track, view).path[0].len(), 3);
    }

    #[test]
    fn a_drag_moves_the_map_by_the_ground_under_the_finger() {
        let track = clean(
            &[fix(0, 52.3791, 4.9003), fix(30, 52.3800, 4.9010)],
            limits(),
        );
        let view = fit(&track, 400.0, 300.0, 16.0);
        let before = frame(&track, view).path[0][0];

        let moved = frame(&track, pan(view, 40.0, -25.0)).path[0][0];

        assert!((moved.x_px - (before.x_px + 40.0)).abs() < 1e-6);
        assert!((moved.y_px - (before.y_px - 25.0)).abs() < 1e-6);
    }

    #[test]
    fn a_pinch_holds_the_ground_under_the_fingers_still() {
        let track = clean(
            &[fix(0, 52.3791, 4.9003), fix(30, 52.3800, 4.9010)],
            limits(),
        );
        let view = View {
            zoom: 14.0,
            ..fit(&track, 400.0, 300.0, 16.0)
        };
        let held = (120.0, 90.0);
        let before = frame(&track, view).path[0][0];

        let closer = zoom_about(view, 2.0, held.0, held.1);
        let after = frame(&track, closer).path[0][0];

        assert!((closer.zoom - 15.0).abs() < 1e-9);
        // Twice the scale about a held point: everything doubles its distance
        // from that point and nothing else moves.
        assert!((after.x_px - held.0 - 2.0 * (before.x_px - held.0)).abs() < 1e-6);
        assert!((after.y_px - held.1 - 2.0 * (before.y_px - held.1)).abs() < 1e-6);
    }

    #[test]
    fn zoom_stays_inside_the_pyramid_that_has_tiles() {
        let track = clean(
            &[fix(0, 52.3791, 4.9003), fix(30, 52.3800, 4.9010)],
            limits(),
        );
        let view = fit(&track, 400.0, 300.0, 16.0);

        assert_eq!(zoom_about(view, 1e6, 200.0, 150.0).zoom, MAX_ZOOM);
        assert_eq!(zoom_about(view, 1e-6, 200.0, 150.0).zoom, MIN_ZOOM);
    }

    #[test]
    fn the_scale_bar_measures_the_ground_the_projection_draws() {
        // What the bar claims and what a kilometre of ground actually spans on
        // screen have to be the same number, or the bar is decoration.
        let metres_per_degree = EARTH_RADIUS_M * std::f64::consts::PI / 180.0;
        for zoom in [12.0, 14.5, 16.0] {
            for lat in [0.0, 45.0, 52.3775, 64.0] {
                let west = Position { lat, lon: 4.89 };
                let east = Position {
                    lat,
                    lon: 4.89 + 1000.0 / (metres_per_degree * lat.to_radians().cos()),
                };
                let (west_x, _) = project(west, zoom);
                let (east_x, _) = project(east, zoom);

                let measured = (east_x - west_x) * metres_per_pixel(lat, zoom);
                assert!(
                    (measured - 1000.0).abs() < 1.0,
                    "at zoom {zoom} and {lat}° the bar reads {measured} m for a kilometre"
                );
            }
        }
    }

    #[test]
    fn smoothing_takes_the_wobble_out_of_a_straight_road() {
        // A straight ride with the position thrown a few metres either side of
        // the road, which is what a reflected signal between buildings looks
        // like. The distance travelled is the road, not the wandering.
        let mut fixes = Vec::new();
        for second in 0..60i64 {
            let wobble = if second % 2 == 0 { 0.00012 } else { -0.00012 };
            fixes.push(Fix {
                lon_e7: ((4.8900 + wobble) * DEGREE_E7).round() as i32,
                ..fix(second, 52.3700 + second as f64 * 0.00004, 4.8900)
            });
        }
        let straight: Vec<Fix> = (0..60)
            .map(|second| fix(second, 52.3700 + second as f64 * 0.00004, 4.8900))
            .collect();

        let wobbled = clean(&fixes, limits()).distance().0;
        let road = clean(&straight, limits()).distance().0;

        assert!(
            wobbled < road * 1.02,
            "{wobbled} m against {road} m of road: the wobble is being counted"
        );
    }

    #[test]
    fn a_corner_is_still_a_corner_after_filtering() {
        // North for half a minute, then east for half a minute, at cycling
        // speed. A filter tuned too smooth turns this into a curve.
        let mut fixes = Vec::new();
        for second in 0..30i64 {
            fixes.push(fix(second, 52.3700 + second as f64 * 0.00004, 4.8900));
        }
        let corner = Position {
            lat: 52.3700 + 30.0 * 0.00004,
            lon: 4.8900,
        };
        for second in 30..60i64 {
            fixes.push(fix(
                second,
                corner.lat,
                4.8900 + (second - 30) as f64 * 0.000065,
            ));
        }

        let track = clean(&fixes, limits());
        let nearest = track
            .fixes()
            .map(|fix| fix.position().distance_to(corner).0)
            .fold(f64::MAX, f64::min);

        assert!(
            nearest < 8.0,
            "the line passes {nearest} m from a corner it went round"
        );
    }

    /// The forward half of the filter on its own: what a live position would
    /// have, knowing only the fixes up to each moment.
    fn forward_only(seconds: &[f64], measured: &[f64], variances: &[f64]) -> Vec<f64> {
        let mut state = State {
            position: measured[0],
            velocity: 0.0,
        };
        let mut spread = Spread {
            pp: variances[0],
            pv: 0.0,
            vv: INITIAL_SPEED_VARIANCE,
        };
        let mut out = vec![state.position];
        for index in 1..measured.len() {
            let (ahead, reach) = predict(state, spread, seconds[index] - seconds[index - 1]);
            let (settled, tightened) = correct(ahead, reach, measured[index], variances[index]);
            state = settled;
            spread = tightened;
            out.push(state.position);
        }
        out
    }

    #[test]
    fn knowing_where_the_run_went_next_is_what_keeps_a_corner() {
        // Thirty seconds north, then thirty east, at five metres a second.
        // A filter that has only seen the past cannot know a turn is coming,
        // so it carries on north into it and cuts the corner off.
        let leg = 5.0;
        let mut seconds = Vec::new();
        let mut north = Vec::new();
        for second in 0..60 {
            seconds.push(second as f64);
            north.push(if second <= 30 {
                second as f64 * leg
            } else {
                30.0 * leg
            });
        }
        let variances = vec![25.0; north.len()];

        let live = forward_only(&seconds, &north, &variances);
        let recorded = smooth_axis(&seconds, &north, &variances);

        // How far past the corner each one carries on north in the seconds
        // right after it, which is where a lagging filter shows.
        let apex = 30.0 * leg;
        let worst = |line: &[f64]| {
            (31..=36)
                .map(|index| (line[index] - apex).abs())
                .fold(f64::MIN, f64::max)
        };
        let live_overshoot = worst(&live);
        let recorded_overshoot = worst(&recorded);

        assert!(
            recorded_overshoot < live_overshoot / 2.0,
            "smoothed is {recorded_overshoot} m past the corner against {live_overshoot} m live"
        );
    }

    #[test]
    fn a_fix_the_receiver_doubted_moves_the_line_less_than_one_it_did_not() {
        let straight: Vec<Fix> = (0..30)
            .map(|second| fix(second, 52.3700 + second as f64 * 0.00004, 4.8900))
            .collect();

        // The same ten metre excursion — small enough to be a body and not a
        // jump — once reported as a good fix and once as a poor one.
        let off = 0.000145;
        let mut trusted = straight.clone();
        trusted[15] = Fix {
            lon_e7: ((4.8900 + off) * DEGREE_E7).round() as i32,
            accuracy_cm: Some(300),
            ..trusted[15]
        };
        let mut doubted = straight.clone();
        doubted[15] = Fix {
            lon_e7: ((4.8900 + off) * DEGREE_E7).round() as i32,
            accuracy_cm: Some(4_000),
            ..doubted[15]
        };

        let pull =
            |fixes: &[Fix]| clean(fixes, limits()).segments[0].fixes[15].position().lon - 4.8900;

        assert!(
            pull(&trusted) > pull(&doubted) * 2.0,
            "a fix reported to 3 m moved the line {} and one reported to 40 m moved it {}",
            pull(&trusted),
            pull(&doubted)
        );
    }

    #[test]
    fn the_colour_band_ignores_a_reflected_fix_at_the_top_of_the_range() {
        let mut fixes: Vec<Fix> = (0..60)
            .map(|second| {
                let mut steady = fix(second, 52.3700 + second as f64 * 0.00004, 4.8900);
                steady.speed_mm_s = Some(4_000);
                steady
            })
            .collect();
        fixes[30].speed_mm_s = Some(40_000);

        let (slow, fast) = clean(&fixes, limits()).speed_band().expect("a band");
        assert_eq!(slow, MetresPerSecond(4.0));
        assert_eq!(
            fast,
            MetresPerSecond(4.0),
            "the outlier is outside the band"
        );
    }

    #[test]
    fn every_drawn_point_carries_the_speed_it_was_travelling_at() {
        let fixes: Vec<Fix> = (0..40)
            .map(|second| {
                let mut moving = fix(second, 52.3700 + second as f64 * 0.00004, 4.8900);
                moving.speed_mm_s = Some(4_000 + second as i32 * 10);
                moving
            })
            .collect();
        let track = clean(&fixes, limits());
        let view = fit(&track, 400.0, 300.0, 16.0);

        let drawn = frame(&track, view);
        assert!(drawn.path[0].iter().all(|point| point.speed.is_some()));
    }

    #[test]
    fn a_scale_bar_shrinks_as_the_map_zooms_in() {
        let close = metres_per_pixel(52.0, 16.0);
        let far = metres_per_pixel(52.0, 12.0);
        assert!(close < far);
        assert!((far / close - 16.0).abs() < 1e-9);
    }
}
