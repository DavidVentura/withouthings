use crate::energy;
use crate::track::Limits;
use crate::units::{Met, Metres, MetresPerSecond};

/// The subcategory a session is recorded under, which is also the id the
/// watch's activity menu lists it by.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct SportId(pub u32);

/// Whether the wearer's own feet carry them through an activity. The pedometer
/// keeps counting either way — a wrist swings on a bike too — so this is what
/// says whether the steps and the metres climbed under a session describe it.
///
/// Pedalled sits apart from Otherwise because speed prices a ride, where it
/// says nothing about a boat or a board.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Gait {
    OnFoot,
    Pedalled,
    Otherwise,
}

use Gait::OnFoot;
use Gait::Otherwise;
use Gait::Pedalled;

/// Whether an activity covers ground, which is a different question from
/// whether feet carry it: a bike covers ground without a step, a treadmill
/// takes thousands without moving. Only the first is worth a receiver.
///
/// The ceiling is the speed above which a jump between two fixes is a lost
/// signal rather than a body: 12 m/s is nonsense on a walk and an ordinary
/// descent on a bike.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Terrain {
    Ground(MetresPerSecond),
    InPlace,
}

use Terrain::Ground;
use Terrain::InPlace;

/// How the watch lays out an activity's screen when its menu entry is written.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Face {
    pub mode: u8,
    pub flag: u16,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Sport {
    pub id: SportId,
    pub name: &'static str,
    pub gait: Gait,
    pub travel: Terrain,
    pub ceiling: Met,
    pub face: Face,
}

impl Sport {
    const fn new(
        id: u32,
        name: &'static str,
        gait: Gait,
        travel: Terrain,
        ceiling: Met,
        face: Face,
    ) -> Sport {
        Sport {
            id: SportId(id),
            name,
            gait,
            travel,
            ceiling,
            face,
        }
    }
}

/// A rate the watch reports under an activity this table has never heard of is
/// still a rate a body reached, so it is held to the highest ceiling any
/// activity has rather than to none.
pub const UNKNOWN_CEILING: Met = Met(19.0);

const UNKNOWN_FACE: Face = Face { mode: 1, flag: 1 };

/// The ceilings are Withings' own, from the `activityCategory` table its app
/// ships: the intensity each activity tops out at, which an estimate drawn from
/// heart rate alone will otherwise sail past.
pub const ACTIVITIES: &[Sport] = &[
    Sport::new(
        1,
        "Walking",
        OnFoot,
        Ground(MetresPerSecond(5.0)),
        Met(8.3),
        Face { mode: 2, flag: 0 },
    ),
    Sport::new(
        2,
        "Running",
        OnFoot,
        Ground(MetresPerSecond(8.0)),
        Met(19.0),
        Face { mode: 2, flag: 0 },
    ),
    Sport::new(
        3,
        "Hiking",
        OnFoot,
        Ground(MetresPerSecond(5.0)),
        Met(6.0),
        Face { mode: 2, flag: 0 },
    ),
    Sport::new(
        4,
        "Skating",
        Otherwise,
        Ground(MetresPerSecond(20.0)),
        Met(14.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        5,
        "BMX",
        Pedalled,
        Ground(MetresPerSecond(20.0)),
        Met(10.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        6,
        "Cycling",
        Pedalled,
        Ground(MetresPerSecond(25.0)),
        Met(15.8),
        Face { mode: 3, flag: 0 },
    ),
    Sport::new(
        7,
        "Swimming",
        Otherwise,
        InPlace,
        Met(13.8),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        8,
        "Surfing",
        Otherwise,
        Ground(MetresPerSecond(15.0)),
        Met(5.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        9,
        "Kitesurfing",
        Otherwise,
        Ground(MetresPerSecond(30.0)),
        Met(11.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        10,
        "Windsurfing",
        Otherwise,
        Ground(MetresPerSecond(30.0)),
        Met(13.5),
        Face { mode: 3, flag: 1 },
    ),
    Sport::new(
        11,
        "Bodyboard",
        Otherwise,
        Ground(MetresPerSecond(15.0)),
        Met(5.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        12,
        "Tennis",
        OnFoot,
        InPlace,
        Met(8.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        13,
        "Table tennis",
        OnFoot,
        InPlace,
        Met(9.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        14,
        "Squash",
        OnFoot,
        InPlace,
        Met(12.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        15,
        "Badminton",
        OnFoot,
        InPlace,
        Met(7.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        16,
        "Weights",
        OnFoot,
        InPlace,
        Met(6.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        17,
        "Calisthenics",
        OnFoot,
        InPlace,
        Met(8.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        18,
        "Elliptical",
        OnFoot,
        InPlace,
        Met(10.0),
        Face { mode: 1, flag: 0 },
    ),
    Sport::new(
        19,
        "Pilates",
        OnFoot,
        InPlace,
        Met(5.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        20,
        "Basketball",
        OnFoot,
        InPlace,
        Met(9.3),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        21,
        "Soccer",
        OnFoot,
        InPlace,
        Met(10.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        22,
        "Football",
        OnFoot,
        InPlace,
        Met(8.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        23,
        "Rugby",
        OnFoot,
        InPlace,
        Met(8.3),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        24,
        "Volleyball",
        OnFoot,
        InPlace,
        Met(8.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        25,
        "Water polo",
        Otherwise,
        InPlace,
        Met(12.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        26,
        "Horse riding",
        Otherwise,
        Ground(MetresPerSecond(20.0)),
        Met(7.3),
        Face { mode: 3, flag: 0 },
    ),
    Sport::new(
        27,
        "Golf",
        OnFoot,
        Ground(MetresPerSecond(20.0)),
        Met(5.3),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        28,
        "Yoga",
        OnFoot,
        InPlace,
        Met(9.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        29,
        "Dancing",
        OnFoot,
        InPlace,
        Met(11.3),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        30,
        "Boxing",
        OnFoot,
        InPlace,
        Met(12.8),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        31,
        "Fencing",
        OnFoot,
        InPlace,
        Met(10.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        32,
        "Wrestling",
        OnFoot,
        InPlace,
        Met(12.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        33,
        "Martial arts",
        OnFoot,
        InPlace,
        Met(10.3),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        34,
        "Skiing",
        Otherwise,
        Ground(MetresPerSecond(40.0)),
        Met(11.3),
        Face { mode: 3, flag: 0 },
    ),
    Sport::new(
        35,
        "Snowboarding",
        Otherwise,
        Ground(MetresPerSecond(30.0)),
        Met(5.3),
        Face { mode: 3, flag: 0 },
    ),
    Sport::new(
        36,
        "Other",
        OnFoot,
        InPlace,
        Met(18.0),
        Face { mode: 3, flag: 0 },
    ),
    Sport::new(
        187,
        "Rowing",
        Otherwise,
        Ground(MetresPerSecond(8.0)),
        Met(11.0),
        Face { mode: 3, flag: 0 },
    ),
    Sport::new(
        188,
        "Zumba",
        OnFoot,
        InPlace,
        Met(11.3),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        191,
        "Baseball",
        OnFoot,
        InPlace,
        Met(9.3),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        192,
        "Handball",
        OnFoot,
        InPlace,
        Met(9.3),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        193,
        "Hockey",
        OnFoot,
        InPlace,
        Met(14.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        194,
        "Ice hockey",
        Otherwise,
        InPlace,
        Met(14.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        195,
        "Climbing",
        OnFoot,
        InPlace,
        Met(6.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        196,
        "Ice skating",
        Otherwise,
        Ground(MetresPerSecond(20.0)),
        Met(14.0),
        Face { mode: 1, flag: 1 },
    ),
    Sport::new(
        306,
        "Indoor walk",
        OnFoot,
        InPlace,
        Met(8.3),
        Face { mode: 2, flag: 1 },
    ),
    Sport::new(
        307,
        "Indoor running",
        OnFoot,
        InPlace,
        Met(19.0),
        Face { mode: 1, flag: 0 },
    ),
];

pub fn find(id: SportId) -> Option<&'static Sport> {
    ACTIVITIES.iter().find(|sport| sport.id == id)
}

pub fn name(id: SportId) -> String {
    find(id)
        .map(|sport| sport.name.to_string())
        .unwrap_or_else(|| format!("Activity {}", id.0))
}

/// An activity the table has never heard of is not credited with a gait, so
/// what the pedometer holds over it goes unshown rather than unexplained.
pub fn on_foot(id: SportId) -> bool {
    find(id).is_some_and(|sport| sport.gait == OnFoot)
}

/// None for an activity that covers no ground, and for one the table has never
/// heard of: without a speed to hold it to there is no way to tell a lost fix
/// from a fast one.
pub fn limits(id: SportId) -> Option<Limits> {
    match find(id)?.travel {
        Ground(ceiling) => Some(Limits {
            accuracy_ceiling: Metres(Limits::DEFAULT_ACCURACY_M),
            speed_ceiling: ceiling,
            gap_secs: Limits::DEFAULT_GAP_SECS,
        }),
        InPlace => None,
    }
}

pub fn ceiling(id: SportId) -> Met {
    find(id).map_or(UNKNOWN_CEILING, |sport| sport.ceiling)
}

pub fn face(id: SportId) -> Face {
    find(id).map_or(UNKNOWN_FACE, |sport| sport.face)
}

/// What the ground says the effort was worth. Heart rate alone reads a city
/// commute as hard riding — traffic, heat and the rate hanging on through the
/// lights all lift it — and the activity's own ceiling is far too loose to
/// catch that. A measured speed is the work actually done.
///
/// None for an activity with no route recorded, and for one whose speed says
/// nothing about its cost: rowing hard on the spot covers no ground.
fn measured_ceiling(sport: &Sport, speed: MetresPerSecond) -> Option<Met> {
    if sport.travel == InPlace {
        return None;
    }
    match sport.gait {
        Pedalled => Some(energy::cycling_met(speed)),
        OnFoot => Some(energy::on_foot_met(speed)),
        Otherwise => None,
    }
}

/// Never above what the activity itself can reach: a reflected fix reading
/// sixty on a bike is not a harder ride than the sport has in it.
pub fn session_ceiling(id: SportId, average_speed: Option<MetresPerSecond>) -> Met {
    let declared = ceiling(id);
    let measured = find(id)
        .zip(average_speed)
        .and_then(|(sport, speed)| measured_ceiling(sport, speed));
    match measured {
        Some(measured) if measured.0 < declared.0 => measured,
        _ => declared,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const WALKING: SportId = SportId(1);
    const BMX: SportId = SportId(5);
    const CYCLING: SportId = SportId(6);
    const SURFING: SportId = SportId(8);
    const WEIGHTS: SportId = SportId(16);
    const NEVER_HEARD_OF: SportId = SportId(9999);

    #[test]
    fn a_slow_ride_is_held_to_what_its_speed_is_worth() {
        let slow = MetresPerSecond(4.0);
        for ride in [CYCLING, BMX] {
            assert_eq!(session_ceiling(ride, Some(slow)), energy::cycling_met(slow));
        }
    }

    #[test]
    fn a_walk_is_priced_as_a_walk() {
        let slow = MetresPerSecond(1.2);
        assert_eq!(
            session_ceiling(WALKING, Some(slow)),
            energy::on_foot_met(slow)
        );
    }

    #[test]
    fn a_speed_never_lifts_a_session_above_its_declared_ceiling() {
        let reflected = MetresPerSecond(200.0);
        assert_eq!(session_ceiling(CYCLING, Some(reflected)), ceiling(CYCLING));
    }

    #[test]
    fn a_sport_whose_speed_says_nothing_keeps_its_declared_ceiling() {
        let speed = MetresPerSecond(1.0);
        assert_eq!(session_ceiling(SURFING, Some(speed)), ceiling(SURFING));
        assert_eq!(session_ceiling(WEIGHTS, Some(speed)), ceiling(WEIGHTS));
        assert_eq!(session_ceiling(CYCLING, None), ceiling(CYCLING));
    }

    #[test]
    fn an_activity_never_heard_of_gets_the_highest_ceiling_and_no_credit() {
        assert_eq!(
            session_ceiling(NEVER_HEARD_OF, Some(MetresPerSecond(1.0))),
            UNKNOWN_CEILING
        );
        assert_eq!(name(NEVER_HEARD_OF), "Activity 9999");
        assert!(!on_foot(NEVER_HEARD_OF));
        assert_eq!(limits(NEVER_HEARD_OF), None);
        assert_eq!(face(NEVER_HEARD_OF), Face { mode: 1, flag: 1 });
    }

    #[test]
    fn the_declared_ceiling_tops_out_at_the_unknown_one() {
        assert!(ACTIVITIES
            .iter()
            .all(|sport| sport.ceiling.0 <= UNKNOWN_CEILING.0));
    }
}
