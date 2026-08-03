use crate::frame::Channel;
use crate::objects::{GlyphId, ImageData, ImageMetadata, NotificationAppId, Null, WppObject};
use crate::{Command, Frame};

const LUMA_THRESHOLD: f64 = 115.2;
const CHUNK: usize = 64;
pub const DEFAULT_SIZE: u8 = 22;
const IMAGE_TYPE: u8 = 0;

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Mono {
    pub width: u8,
    pub height: u8,
    pub bits: Vec<u8>,
}

impl Mono {
    pub fn empty() -> Mono {
        Mono {
            width: 0,
            height: 0,
            bits: Vec::new(),
        }
    }

    fn bytes_per_column(height: u8) -> usize {
        if height == 0 {
            return 0;
        }
        (height as usize - 1) / 8 + 1
    }

    pub fn pack(pixels: &[u32], width: u8, height: u8) -> Mono {
        assert_eq!(
            pixels.len(),
            width as usize * height as usize,
            "pixel count must match the declared size"
        );
        let stride = Mono::bytes_per_column(height);
        let mut bits = vec![0u8; stride * width as usize];
        for x in 0..width as usize {
            for y in 0..height as usize {
                let pixel = pixels[y * width as usize + x];
                if luma(pixel) > LUMA_THRESHOLD {
                    bits[x * stride + y / 8] |= 1 << (y % 8);
                }
            }
        }
        Mono {
            width,
            height,
            bits,
        }
    }

    fn metadata(&self) -> ImageMetadata {
        self.metadata_of(IMAGE_TYPE)
    }

    pub fn metadata_of(&self, kind: u8) -> ImageMetadata {
        ImageMetadata {
            r#type: kind,
            width: self.width,
            height: self.height,
        }
    }

    pub fn data_objects(&self) -> Vec<WppObject> {
        self.chunks()
    }

    fn chunks(&self) -> Vec<WppObject> {
        if self.bits.is_empty() {
            return vec![WppObject::ImageData(ImageData { data: Vec::new() })];
        }
        self.bits
            .chunks(CHUNK)
            .map(|c| WppObject::ImageData(ImageData { data: c.to_vec() }))
            .collect()
    }
}

fn luma(argb: u32) -> f64 {
    let alpha = ((argb >> 24) & 0xff) as f64 / 255.0;
    let red = ((argb >> 16) & 0xff) as f64;
    let green = ((argb >> 8) & 0xff) as f64;
    let blue = (argb & 0xff) as f64;
    (0.2126 * red + 0.7152 * green + 0.0722 * blue) * alpha
}

fn asks(frame: &Frame, command: Command) -> bool {
    frame.command.opcode() == command.0 && frame.command.channel() == Some(Channel::SlaveRequest)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImageFormat {
    pub kind: u8,
    pub width: u8,
    pub height: u8,
}

impl ImageFormat {
    pub fn declared(frame: &Frame) -> Vec<ImageFormat> {
        if frame.command.opcode() != Command::CMD_WORKOUT_SCREEN_LIST_GET.0 {
            return Vec::new();
        }
        frame
            .objects
            .iter()
            .filter_map(|o| match o {
                WppObject::ImageMetadata(m) => Some(ImageFormat {
                    kind: m.r#type,
                    width: m.width,
                    height: m.height,
                }),
                _ => None,
            })
            .collect()
    }
}

/// The watch asks for notification icons at 34x34 tagged type 0, which is the
/// type 1 size; answering at that size reboots it. Where the request and the
/// declaration disagree, the declaration wins.
fn fit(kind: u8, width: u8, height: u8, formats: &[ImageFormat]) -> (u8, u8) {
    let Some(format) = formats.iter().find(|f| f.kind == kind) else {
        return (width.min(UNDECLARED_MAX), height.min(UNDECLARED_MAX));
    };
    (width.min(format.width), height.min(format.height))
}

const UNDECLARED_MAX: u8 = 20;

fn requested(objects: &[WppObject]) -> (u8, u8, u8) {
    objects
        .iter()
        .find_map(|o| match o {
            WppObject::ImageMetadata(m) => Some((m.r#type, m.width, m.height)),
            _ => None,
        })
        .unwrap_or((IMAGE_TYPE, DEFAULT_SIZE, DEFAULT_SIZE))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GlyphRequest {
    pub glyphs: Vec<Glyph>,
    kind: u8,
    width: u8,
    height: u8,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Glyph {
    pub codepoint: u32,
    raw: u32,
}

impl GlyphRequest {
    pub fn parse(frame: &Frame) -> Option<GlyphRequest> {
        if !asks(frame, Command::CMD_GLYPH_GET) {
            return None;
        }
        let (kind, width, height) = requested(&frame.objects);
        let glyphs = frame
            .objects
            .iter()
            .filter_map(|o| match o {
                WppObject::GlyphId(g) => Some(Glyph {
                    codepoint: g.unicode.swap_bytes(),
                    raw: g.unicode,
                }),
                _ => None,
            })
            .collect();
        Some(GlyphRequest {
            glyphs,
            kind,
            width,
            height,
        })
    }

    pub fn size(&self, _formats: &[ImageFormat]) -> (u8, u8) {
        (self.width, self.height)
    }

    pub fn reply(&self, bitmaps: &[Mono]) -> Frame {
        assert_eq!(
            bitmaps.len(),
            self.glyphs.len(),
            "every glyph asked for needs an answer"
        );
        let mut objects = Vec::new();
        for (glyph, bitmap) in self.glyphs.iter().zip(bitmaps) {
            objects.push(WppObject::GlyphId(GlyphId { unicode: glyph.raw }));
            objects.push(WppObject::ImageMetadata(bitmap.metadata()));
            objects.extend(bitmap.chunks());
        }
        objects.push(WppObject::Null(Null {}));
        Frame::new(
            Command::CMD_GLYPH_GET.with_channel(Channel::SlaveRequest),
            objects,
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IconRequest {
    pub app_id: String,
    kind: u8,
    width: u8,
    height: u8,
}

impl IconRequest {
    pub fn parse(frame: &Frame) -> Option<IconRequest> {
        if !asks(frame, Command::CMD_NOTIFICATION_GET) {
            return None;
        }
        let app_id = frame.objects.iter().find_map(|o| match o {
            WppObject::NotificationAppId(a) => Some(a.id.clone()),
            _ => None,
        })?;
        let (kind, width, height) = requested(&frame.objects);
        Some(IconRequest {
            app_id,
            kind,
            width,
            height,
        })
    }

    pub fn size(&self, formats: &[ImageFormat]) -> (u8, u8) {
        fit(self.kind, self.width, self.height, formats)
    }

    pub fn reply(&self, icon: &Mono) -> Frame {
        let mut objects: Vec<WppObject> = self
            .app_id
            .as_bytes()
            .chunks(CHUNK)
            .map(|c| {
                WppObject::NotificationAppId(NotificationAppId {
                    id: String::from_utf8_lossy(c).into_owned(),
                })
            })
            .collect();
        objects.push(WppObject::ImageMetadata(icon.metadata()));
        objects.extend(icon.chunks());
        objects.push(WppObject::Null(Null {}));
        Frame::new(
            Command::CMD_NOTIFICATION_GET.with_channel(Channel::SlaveRequest),
            objects,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const WHITE: u32 = 0xffff_ffff;
    const CLEAR: u32 = 0x0000_0000;

    #[test]
    fn packing_is_column_major_with_the_top_pixel_in_the_low_bit() {
        let mut pixels = vec![CLEAR; 2 * 9];
        pixels[0] = WHITE;
        pixels[8 * 2] = WHITE;
        pixels[1] = WHITE;
        let mono = Mono::pack(&pixels, 2, 9);
        assert_eq!(mono.bits, vec![0b0000_0001, 0b0000_0001, 0b0000_0001, 0]);
        assert_eq!(mono.bits.len(), 2 * 2);
    }

    #[test]
    fn a_22px_glyph_is_three_bytes_per_column() {
        let mono = Mono::pack(&vec![CLEAR; 22 * 22], 22, 22);
        assert_eq!(mono.bits.len(), 66);
    }

    #[test]
    fn transparency_dims_a_pixel_below_the_threshold() {
        assert_eq!(Mono::pack(&[0xff80_8080], 1, 1).bits, vec![1]);
        assert_eq!(Mono::pack(&[0x60ff_ffff], 1, 1).bits, vec![0]);
        assert_eq!(Mono::pack(&[0x80ff_ffff], 1, 1).bits, vec![1]);
    }

    #[test]
    fn an_empty_bitmap_still_answers_with_one_empty_object() {
        assert_eq!(
            Mono::empty().chunks(),
            vec![WppObject::ImageData(ImageData { data: Vec::new() })]
        );
    }

    #[test]
    fn bits_are_split_into_64_byte_objects() {
        let mono = Mono::pack(&vec![WHITE; 100 * 8], 100, 8);
        assert_eq!(mono.bits.len(), 100);
        let chunks = mono.chunks();
        assert_eq!(chunks.len(), 2);
        assert_eq!(
            chunks[0],
            WppObject::ImageData(ImageData {
                data: vec![0xff; 64]
            })
        );
        assert_eq!(
            chunks[1],
            WppObject::ImageData(ImageData {
                data: vec![0xff; 36]
            })
        );
    }

    #[test]
    fn a_glyph_id_is_swapped_to_read_and_echoed_untouched() {
        let frame = Frame::new(
            Command::CMD_GLYPH_GET.with_channel(Channel::SlaveRequest),
            vec![WppObject::GlyphId(GlyphId {
                unicode: 0x4b26_0100,
            })],
        );
        let request = GlyphRequest::parse(&frame).unwrap();
        assert_eq!(request.glyphs[0].codepoint, 0x0001_264b);
        assert_eq!(request.width, DEFAULT_SIZE);

        let reply = request.reply(&[Mono::empty()]);
        assert_eq!(
            reply.objects[0],
            WppObject::GlyphId(GlyphId {
                unicode: 0x4b26_0100
            })
        );
        assert_eq!(reply.command.channel(), Some(Channel::SlaveRequest));
        assert_eq!(reply.objects.last(), Some(&WppObject::Null(Null {})));
    }

    #[test]
    fn the_requested_size_overrides_the_default() {
        let frame = Frame::new(
            Command::CMD_GLYPH_GET.with_channel(Channel::SlaveRequest),
            vec![
                WppObject::GlyphId(GlyphId {
                    unicode: 0x4100_0000,
                }),
                WppObject::ImageMetadata(ImageMetadata {
                    r#type: 0,
                    width: 16,
                    height: 24,
                }),
            ],
        );
        let request = GlyphRequest::parse(&frame).unwrap();
        assert_eq!((request.width, request.height), (16, 24));
    }

    #[test]
    fn only_a_watch_initiated_frame_is_a_request() {
        let objects = vec![WppObject::GlyphId(GlyphId {
            unicode: 0x4100_0000,
        })];
        assert!(
            GlyphRequest::parse(&Frame::new(Command::CMD_GLYPH_GET, objects.clone())).is_none()
        );
        assert!(GlyphRequest::parse(&Frame::new(
            Command::CMD_GLYPH_GET.with_channel(Channel::SlaveRequest),
            objects
        ))
        .is_some());
    }

    #[test]
    fn a_request_is_held_to_the_size_its_type_was_declared_at() {
        let formats = vec![
            ImageFormat {
                kind: 0,
                width: 20,
                height: 20,
            },
            ImageFormat {
                kind: 1,
                width: 34,
                height: 34,
            },
        ];
        assert_eq!(fit(0, 34, 34, &formats), (20, 20));
        assert_eq!(fit(1, 34, 34, &formats), (34, 34));
        assert_eq!(fit(0, 8, 8, &formats), (8, 8));
        assert_eq!(fit(7, 34, 34, &formats), (20, 20));
        assert_eq!(fit(0, 34, 34, &[]), (20, 20));
    }

    #[test]
    fn only_the_workout_screen_reply_declares_formats() {
        let objects = vec![
            WppObject::ImageMetadata(ImageMetadata {
                r#type: 0,
                width: 20,
                height: 20,
            }),
            WppObject::ImageMetadata(ImageMetadata {
                r#type: 1,
                width: 34,
                height: 34,
            }),
        ];
        let declaration = Frame::new(Command::CMD_WORKOUT_SCREEN_LIST_GET, objects.clone());
        assert_eq!(
            ImageFormat::declared(&declaration),
            vec![
                ImageFormat {
                    kind: 0,
                    width: 20,
                    height: 20
                },
                ImageFormat {
                    kind: 1,
                    width: 34,
                    height: 34
                },
            ]
        );

        let request = Frame::new(
            Command::CMD_NOTIFICATION_GET.with_channel(Channel::SlaveRequest),
            objects,
        );
        assert!(ImageFormat::declared(&request).is_empty());
    }

    #[test]
    fn an_app_id_longer_than_a_chunk_is_split() {
        let app_id = "a".repeat(70);
        let request = IconRequest {
            app_id: app_id.clone(),
            kind: 0,
            width: 22,
            height: 22,
        };
        let reply = request.reply(&Mono::empty());
        let ids: Vec<String> = reply
            .objects
            .iter()
            .filter_map(|o| match o {
                WppObject::NotificationAppId(a) => Some(a.id.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(ids.len(), 2);
        assert_eq!(ids.concat(), app_id);
    }

    #[test]
    fn a_reply_survives_a_round_trip_through_the_frame_codec() {
        let request = IconRequest {
            app_id: "dev.davidv.withoutings".into(),
            kind: 0,
            width: 3,
            height: 3,
        };
        let icon = Mono::pack(&[WHITE; 9], 3, 3);
        for frame in request.reply(&icon).to_wire() {
            assert_eq!(Frame::parse(&frame.to_bytes()).unwrap(), frame);
        }
    }

    #[test]
    fn a_full_size_icon_is_split_into_frames_the_watch_survives() {
        let request = IconRequest {
            app_id: "dev.davidv.withoutings".into(),
            kind: 1,
            width: 34,
            height: 34,
        };
        let frames = request
            .reply(&Mono::pack(&[WHITE; 34 * 34], 34, 34))
            .to_wire();
        assert!(
            frames.len() > 1,
            "170 bytes of icon cannot ride in one frame"
        );
        for frame in &frames {
            assert!(
                frame.to_bytes().len() <= crate::frame::MAX_FRAME_BYTES,
                "every frame must be sendable",
            );
        }
    }
}
