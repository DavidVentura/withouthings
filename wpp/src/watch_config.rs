use crate::client::{Category, Feature, FeatureId, FeatureSchedule};
use crate::units::Duration;

pub const CATEGORIES: [Category; 9] = [
    Category(5),
    Category(9),
    Category(8),
    Category(6),
    Category(12),
    Category(11),
    Category(10),
    Category::ACTIVITY,
    Category::BODY,
];

pub const KNOWN_SCREENS: &[(u8, &str)] = &[
    (1, "Steps"),
    (2, "Distance"),
    (3, "Calories"),
    (4, "Heart rate"),
    (6, "Date"),
    (9, "Electrocardiogram (ECG)"),
    (10, "Oxygen saturation (SpO2)"),
    (11, "Workouts"),
    (12, "Elevation"),
    (16, "Clock"),
    (17, "Settings"),
    (18, "Breathe"),
    (20, "Body temperature"),
    (21, "Cycle tracking"),
    (22, "Sleep"),
];

pub fn screen_name(id: u8) -> String {
    KNOWN_SCREENS
        .iter()
        .find(|(known, _)| *known == id)
        .map(|(_, name)| name.to_string())
        .unwrap_or_else(|| format!("Screen {id}"))
}

/// `FEATURE_ID_RESPIRATORY_SCAN_MONITORING`. It holds the optical sensor on
/// through the night, and the reference app never sends it permanent: it is
/// the one id in the whole capture that arrives with a window.
pub const RESPIRATORY_SCAN_MONITORING: FeatureId = FeatureId(9);

pub const SCAN_WINDOW: Duration = Duration::from_secs(24 * 60 * 60);

/// The message carries the whole enabled set, so an id left out is switched
/// off. This is what the reference app sends, minus 9, which is armed a window
/// at a time instead — see [`RESPIRATORY_SCAN_MONITORING`]. 88 and 113 have no
/// constant in the APK but the reference app sends them, so they are carried.
pub const DEFAULT_FEATURES: &[u16] = &[3, 5, 10, 11, 14, 17, 19, 20, 27, 53, 71, 88, 113];

pub fn default_features() -> Vec<Feature> {
    DEFAULT_FEATURES
        .iter()
        .map(|id| Feature {
            id: FeatureId(*id),
            schedule: FeatureSchedule::Permanent,
        })
        .collect()
}

pub const HEALTH_FEATURES: &[(u16, &str, &str)] = &[
    (17, "Signs of AFib", "Monitor for irregular heartbeat"),
    (
        14,
        "AFib (medical grade)",
        "Medically certified AFib detection",
    ),
    (5, "SpO2 during sleep", "Measure blood oxygen while asleep"),
    (3, "SpO2", "Blood oxygen measurement"),
    (10, "Respiratory scan", "Breathing rate measurement"),
    (
        11,
        "Respiratory scan (smart)",
        "Choose when to measure automatically",
    ),
    (71, "Body temperature", "Skin and core temperature"),
    (27, "Electrocardiogram", "On-demand ECG recording"),
    (53, "ECG intervals", "QT, QTc, PQ and QRS from the ECG"),
    (
        22,
        "Resting heart rate alerts",
        "Alert on unusually high or low resting rate",
    ),
    (
        26,
        "Automatic resting HR alerts",
        "Set the alert thresholds automatically",
    ),
    (70, "Cycle tracking", "Menstrual cycle logging"),
    (20, "Activity tracking", "Steps, distance and calories"),
    (19, "Notifications", "Phone notifications on the watch"),
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_screen_never_heard_of_is_named_by_its_id() {
        assert_eq!(screen_name(4), "Heart rate");
        assert_eq!(screen_name(200), "Screen 200");
    }

    #[test]
    fn the_scan_that_is_armed_a_night_at_a_time_is_not_a_default() {
        assert!(default_features()
            .iter()
            .all(|feature| feature.id != RESPIRATORY_SCAN_MONITORING
                && feature.schedule == FeatureSchedule::Permanent));
    }
}
