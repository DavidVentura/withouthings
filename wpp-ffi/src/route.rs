use wpp::track::{self, Track};
use wpp::units::UnixMillis;

use crate::records::{Origin, Point};

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct TrackSummary {
    pub distance_metres: f64,
    /// Seconds under way, which is what an average speed means anything over.
    pub moving_secs: i64,
    pub average_speed_m_s: Option<f64>,
    /// The middle of the spread of speeds, for colouring the line by it.
    pub slow_m_s: Option<f64>,
    pub fast_m_s: Option<f64>,
    pub fixes: u32,
}

/// Where the map is looking, carried by the caller between gestures so the
/// projection stays on this side of the boundary.
#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct MapView {
    pub centre_lat: f64,
    pub centre_lon: f64,
    pub zoom: f64,
    pub width_px: f64,
    pub height_px: f64,
}

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct MapTile {
    pub z: u32,
    pub x: u32,
    pub y: u32,
    pub left_px: f64,
    pub top_px: f64,
    pub size_px: f64,
}

#[derive(uniffi::Record, Debug, Clone, Copy, PartialEq)]
pub struct MapPoint {
    pub at_ms: i64,
    pub x_px: f64,
    pub y_px: f64,
    pub speed_m_s: Option<f64>,
}

/// One unbroken run of the route. A stretch with no fixes is a break in the
/// line rather than a straight leg across whatever was not recorded.
#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct MapPath {
    pub points: Vec<MapPoint>,
}

#[derive(uniffi::Record, Debug, Clone, PartialEq)]
pub struct MapFrame {
    pub tiles: Vec<MapTile>,
    pub path: Vec<MapPath>,
    pub metres_per_pixel: f64,
}

/// A route read once and asked many questions. Cleaning and projecting on
/// every call would re-read the whole session for each frame of a pinch.
#[derive(uniffi::Object)]
pub struct RouteTrack {
    pub(crate) track: Track,
}

#[uniffi::export]
impl RouteTrack {
    pub fn summary(&self) -> TrackSummary {
        let band = self.track.speed_band();
        TrackSummary {
            distance_metres: self.track.distance().0,
            moving_secs: self.track.moving_secs(),
            average_speed_m_s: self.track.average_speed().map(|speed| speed.0),
            slow_m_s: band.map(|(slow, _)| slow.0),
            fast_m_s: band.map(|(_, fast)| fast.0),
            fixes: self.track.fixes().count() as u32,
        }
    }

    pub fn speed_series(&self) -> Vec<Point> {
        self.track
            .speed_series()
            .into_iter()
            .map(|(at, speed)| Point {
                at_ms: at.0,
                value: speed.0,
                origin: Origin::Stored,
            })
            .collect()
    }

    pub fn fit(&self, width_px: f64, height_px: f64, padding_px: f64) -> MapView {
        of_view(track::fit(&self.track, width_px, height_px, padding_px))
    }

    /// Where the body was at a moment, for a cursor. Deliberately not read off
    /// `frame`: the drawn path is simplified, and a stop is exactly what
    /// simplification removes.
    pub fn cursor(&self, view: MapView, at_ms: i64) -> Option<MapPoint> {
        track::cursor(&self.track, view.held(), UnixMillis(at_ms)).map(|point| MapPoint {
            at_ms: point.at.0,
            x_px: point.x_px,
            y_px: point.y_px,
            speed_m_s: point.speed.map(|speed| speed.0),
        })
    }

    pub fn frame(&self, view: MapView) -> MapFrame {
        let drawn = track::frame(&self.track, view.held());
        MapFrame {
            tiles: drawn
                .tiles
                .into_iter()
                .map(|placed| MapTile {
                    z: placed.tile.z,
                    x: placed.tile.x,
                    y: placed.tile.y,
                    left_px: placed.left_px,
                    top_px: placed.top_px,
                    size_px: placed.size_px,
                })
                .collect(),
            path: drawn
                .path
                .into_iter()
                .map(|points| MapPath {
                    points: points
                        .into_iter()
                        .map(|point| MapPoint {
                            at_ms: point.at.0,
                            x_px: point.x_px,
                            y_px: point.y_px,
                            speed_m_s: point.speed.map(|speed| speed.0),
                        })
                        .collect(),
                })
                .collect(),
            metres_per_pixel: drawn.metres_per_pixel,
        }
    }
}

#[uniffi::export]
pub fn pan_map(view: MapView, dx_px: f64, dy_px: f64) -> MapView {
    of_view(track::pan(view.held(), dx_px, dy_px))
}

#[uniffi::export]
pub fn zoom_map(view: MapView, factor: f64, at_x_px: f64, at_y_px: f64) -> MapView {
    of_view(track::zoom_about(view.held(), factor, at_x_px, at_y_px))
}

impl MapView {
    fn held(&self) -> track::View {
        track::View {
            centre: track::Position {
                lat: self.centre_lat,
                lon: self.centre_lon,
            },
            zoom: self.zoom,
            width_px: self.width_px,
            height_px: self.height_px,
        }
    }
}

fn of_view(view: track::View) -> MapView {
    MapView {
        centre_lat: view.centre.lat,
        centre_lon: view.centre.lon,
        zoom: view.zoom,
        width_px: view.width_px,
        height_px: view.height_px,
    }
}
