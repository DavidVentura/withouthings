pub const SERVICE_UUID: &str = "10000057-5749-5448-0037-000000000000";
pub const NOTIFICATION_SOURCE_UUID: &str = "10000059-5749-5448-0037-000000000000";
pub const CONTROL_POINT_UUID: &str = "10000058-5749-5448-0037-000000000000";
pub const DATA_SOURCE_UUID: &str = "1000005a-5749-5448-0037-000000000000";

const EVENT_FLAGS: u8 = 0x02;

const GET_NOTIFICATION_ATTRIBUTES: u8 = 0x00;

const ELLIPSIS: &[u8] = b"...";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Category {
    Other = 0,
    IncomingCall = 1,
    MissedCall = 2,
    VoiceMail = 3,
    Social = 4,
    Schedule = 5,
    Email = 6,
    News = 7,
    HealthAndFitness = 8,
    BusinessAndFinance = 9,
    Location = 10,
    Entertainment = 11,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventKind {
    Added = 0,
    Removed = 2,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct NotificationId(pub u32);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notification {
    pub id: NotificationId,
    pub app_id: String,
    pub title: String,
    pub subtitle: String,
    pub message: String,
    pub category: Category,
}

impl Notification {
    fn attribute(&self, attribute: Attribute) -> &str {
        match attribute {
            Attribute::AppIdentifier => &self.app_id,
            Attribute::Title => &self.title,
            Attribute::Subtitle => &self.subtitle,
            Attribute::Message => &self.message,
            Attribute::Unknown(_) => "",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Attribute {
    AppIdentifier,
    Title,
    Subtitle,
    Message,
    /// Answered with an empty value rather than omitted: leaving it out would
    /// shift every later attribute in the reply, which the watch parses
    /// positionally.
    Unknown(u8),
}

impl Attribute {
    fn from_id(id: u8) -> Attribute {
        match id {
            0 => Attribute::AppIdentifier,
            1 => Attribute::Title,
            2 => Attribute::Subtitle,
            3 => Attribute::Message,
            other => Attribute::Unknown(other),
        }
    }

    fn id(self) -> u8 {
        match self {
            Attribute::AppIdentifier => 0,
            Attribute::Title => 1,
            Attribute::Subtitle => 2,
            Attribute::Message => 3,
            Attribute::Unknown(id) => id,
        }
    }

    fn is_bounded(self) -> bool {
        matches!(
            self,
            Attribute::Title | Attribute::Subtitle | Attribute::Message
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AttributeQuery {
    pub attribute: Attribute,
    pub max_len: Option<u16>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ControlPoint {
    pub id: NotificationId,
    pub queries: Vec<AttributeQuery>,
}

impl ControlPoint {
    pub const GET_NOTIFICATION_ATTRIBUTES: u8 = GET_NOTIFICATION_ATTRIBUTES;

    pub fn parse(bytes: &[u8]) -> Result<ControlPoint, AncsError> {
        let (&command, rest) = bytes.split_first().ok_or(AncsError::Empty)?;
        if command != GET_NOTIFICATION_ATTRIBUTES {
            return Err(AncsError::UnsupportedCommand(command));
        }
        let id = rest
            .get(..4)
            .ok_or(AncsError::Truncated)
            .map(|b| NotificationId(u32::from_be_bytes([b[0], b[1], b[2], b[3]])))?;

        let mut queries = Vec::new();
        let mut rest = &rest[4..];
        while let Some((&attribute_id, tail)) = rest.split_first() {
            let attribute = Attribute::from_id(attribute_id);
            if !attribute.is_bounded() {
                queries.push(AttributeQuery {
                    attribute,
                    max_len: None,
                });
                rest = tail;
                continue;
            }
            let len = tail.get(..2).ok_or(AncsError::Truncated)?;
            queries.push(AttributeQuery {
                attribute,
                max_len: Some(u16::from_le_bytes([len[0], len[1]])),
            });
            rest = &tail[2..];
        }
        Ok(ControlPoint { id, queries })
    }

    pub fn response(&self, notification: &Notification) -> Vec<u8> {
        let mut out = vec![GET_NOTIFICATION_ATTRIBUTES];
        out.extend_from_slice(&self.id.0.to_le_bytes());
        for query in &self.queries {
            let value = fit(
                notification.attribute(query.attribute).as_bytes(),
                query.max_len,
            );
            out.push(query.attribute.id());
            out.extend_from_slice(&(value.len() as u16).to_le_bytes());
            out.extend_from_slice(&value);
        }
        out
    }
}

fn fit(value: &[u8], max: Option<u16>) -> Vec<u8> {
    let Some(max) = max.map(usize::from) else {
        return value.to_vec();
    };
    if value.len() <= max {
        return value.to_vec();
    }
    if max < ELLIPSIS.len() {
        return value[..max].to_vec();
    }
    let mut out = value[..max - ELLIPSIS.len()].to_vec();
    out.extend_from_slice(ELLIPSIS);
    out
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AncsError {
    Empty,
    Truncated,
    UnsupportedCommand(u8),
}

impl core::fmt::Display for AncsError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            AncsError::Empty => write!(f, "empty control point write"),
            AncsError::Truncated => write!(f, "control point write ends mid-field"),
            AncsError::UnsupportedCommand(c) => write!(f, "unsupported ANCS command {c}"),
        }
    }
}

impl std::error::Error for AncsError {}

pub fn announcement(kind: EventKind, notification: &Notification) -> [u8; 8] {
    let count = match kind {
        EventKind::Added => 1,
        EventKind::Removed => 0,
    };
    let id = notification.id.0.to_be_bytes();
    [
        kind as u8,
        EVENT_FLAGS,
        notification.category as u8,
        count,
        id[0],
        id[1],
        id[2],
        id[3],
    ]
}

pub fn fragments(payload: &[u8], max_payload: usize) -> Vec<Vec<u8>> {
    assert!(max_payload > 0, "a fragment has to carry something");
    payload.chunks(max_payload).map(<[u8]>::to_vec).collect()
}

#[derive(Debug, Default)]
pub struct NotificationCenter {
    live: Vec<Notification>,
    next_id: u32,
}

impl NotificationCenter {
    pub fn new() -> NotificationCenter {
        NotificationCenter {
            live: Vec::new(),
            next_id: 1,
        }
    }

    pub fn post(
        &mut self,
        app_id: String,
        title: String,
        subtitle: String,
        message: String,
        category: Category,
    ) -> (NotificationId, [u8; 8]) {
        let id = NotificationId(self.next_id);
        self.next_id += 1;
        let notification = Notification {
            id,
            app_id,
            title,
            subtitle,
            message,
            category,
        };
        let announcement = announcement(EventKind::Added, &notification);
        self.live.push(notification);
        (id, announcement)
    }

    pub fn dismiss(&mut self, id: NotificationId) -> Option<[u8; 8]> {
        let index = self.live.iter().position(|n| n.id == id)?;
        let notification = self.live.remove(index);
        Some(announcement(EventKind::Removed, &notification))
    }

    pub fn get(&self, id: NotificationId) -> Option<&Notification> {
        self.live.iter().find(|n| n.id == id)
    }

    pub fn live(&self) -> &[Notification] {
        &self.live
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn notification() -> Notification {
        Notification {
            id: NotificationId(0x0102_0304),
            app_id: "dev.davidv.withoutings".into(),
            title: "Title".into(),
            subtitle: "Sub".into(),
            message: "Message".into(),
            category: Category::Social,
        }
    }

    #[test]
    fn an_announcement_is_eight_bytes_with_a_big_endian_id() {
        assert_eq!(
            announcement(EventKind::Added, &notification()),
            [0, 0x02, 4, 1, 0x01, 0x02, 0x03, 0x04]
        );
        assert_eq!(
            announcement(EventKind::Removed, &notification()),
            [2, 0x02, 4, 0, 0x01, 0x02, 0x03, 0x04]
        );
    }

    #[test]
    fn the_id_changes_byte_order_between_the_request_and_the_reply() {
        let write = [0x00, 0x01, 0x02, 0x03, 0x04, 0x00, 0x01, 0x10, 0x00];
        let request = ControlPoint::parse(&write).unwrap();
        assert_eq!(request.id, NotificationId(0x0102_0304));
        assert_eq!(
            request.queries,
            vec![
                AttributeQuery {
                    attribute: Attribute::AppIdentifier,
                    max_len: None
                },
                AttributeQuery {
                    attribute: Attribute::Title,
                    max_len: Some(16)
                },
            ]
        );

        let response = request.response(&notification());
        assert_eq!(&response[..5], &[0x00, 0x04, 0x03, 0x02, 0x01]);
    }

    #[test]
    fn a_response_lays_out_id_length_then_value() {
        let write = [0x00, 0, 0, 0, 1, 0x01, 0x40, 0x00];
        let request = ControlPoint::parse(&write).unwrap();
        let response = request.response(&notification());
        assert_eq!(
            &response[5..],
            &[0x01, 0x05, 0x00, b'T', b'i', b't', b'l', b'e']
        );
    }

    #[test]
    fn an_over_long_value_is_cut_and_marked() {
        assert_eq!(fit(b"abcdefgh", Some(5)), b"ab...".to_vec());
        assert_eq!(fit(b"abcdefgh", Some(8)), b"abcdefgh".to_vec());
        assert_eq!(fit(b"abcdefgh", None), b"abcdefgh".to_vec());
    }

    #[test]
    fn a_budget_below_the_marker_just_clips() {
        assert_eq!(fit(b"abcdefgh", Some(2)), b"ab".to_vec());
        assert_eq!(fit(b"abcdefgh", Some(0)), Vec::<u8>::new());
    }

    #[test]
    fn an_unknown_attribute_is_answered_empty() {
        let write = [0x00, 0, 0, 0, 1, 0x07];
        let request = ControlPoint::parse(&write).unwrap();
        assert_eq!(request.queries[0].attribute, Attribute::Unknown(7));
        let response = request.response(&notification());
        assert_eq!(&response[5..], &[0x07, 0x00, 0x00]);
    }

    #[test]
    fn only_one_command_is_accepted() {
        assert_eq!(
            ControlPoint::parse(&[0x02, 0, 0, 0, 1]),
            Err(AncsError::UnsupportedCommand(2))
        );
        assert_eq!(ControlPoint::parse(&[]), Err(AncsError::Empty));
        assert_eq!(
            ControlPoint::parse(&[0x00, 0, 0]),
            Err(AncsError::Truncated)
        );
        assert_eq!(
            ControlPoint::parse(&[0x00, 0, 0, 0, 1, 0x01, 0x10]),
            Err(AncsError::Truncated)
        );
    }

    #[test]
    fn fragments_split_on_the_payload_size() {
        assert_eq!(
            fragments(&[1, 2, 3, 4, 5], 2),
            vec![vec![1, 2], vec![3, 4], vec![5]]
        );
        assert_eq!(fragments(&[1, 2], 8), vec![vec![1, 2]]);
    }

    #[test]
    fn a_posted_notification_can_be_read_back_and_dismissed() {
        let mut center = NotificationCenter::new();
        let (id, added) = center.post(
            "app".into(),
            "t".into(),
            "s".into(),
            "m".into(),
            Category::Email,
        );
        assert_eq!(added[0], EventKind::Added as u8);
        assert_eq!(added[2], Category::Email as u8);
        assert_eq!(center.get(id).unwrap().title, "t");

        let removed = center.dismiss(id).unwrap();
        assert_eq!(removed[0], EventKind::Removed as u8);
        assert!(center.get(id).is_none());
        assert!(center.dismiss(id).is_none());
    }

    #[test]
    fn ids_are_not_reused_after_a_dismissal() {
        let mut center = NotificationCenter::new();
        let (first, _) = center.post(
            "a".into(),
            String::new(),
            String::new(),
            String::new(),
            Category::Other,
        );
        center.dismiss(first);
        let (second, _) = center.post(
            "a".into(),
            String::new(),
            String::new(),
            String::new(),
            Category::Other,
        );
        assert_ne!(first, second);
    }
}
