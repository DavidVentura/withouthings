//! Bitmaps in the form the watch takes them, and the two requests that carry
//! them.
//!
//! The watch has no font for characters outside its own, and no icons for the
//! apps a notification can come from, so it asks the phone to draw them and
//! sends back the size it wants. Both requests arrive on
//! [`Channel::SlaveRequest`] and are answered on the same channel.
//!
//! Rasterising needs a font and a canvas, which is the host's business. What
//! is here is everything either side of it: reading the request, packing a
//! rendered bitmap into the wire format, and building the reply.

use crate::frame::Channel;
use crate::objects::{GlyphId, ImageData, ImageMetadata, NotificationAppId, Null, WppObject};
use crate::{Command, Frame};

/// Luma above which a pixel is lit, out of 255. From `aca.a`, which is
/// `0.45 * 256` however it was arrived at.
const LUMA_THRESHOLD: f64 = 115.2;
/// Payload of one `ImageData` object. The app splits on this and so does the
/// app id in a notification reply.
const CHUNK: usize = 64;
/// What the app draws when the watch names no size.
pub const DEFAULT_SIZE: u8 = 22;
/// `ImageMetadata.type` for everything either side sends.
const IMAGE_TYPE: u8 = 0;

/// A 1-bit bitmap, packed as the watch reads it.
///
/// Column-major: the first `ceil(height / 8)` bytes are column 0, top pixel in
/// the low bit of the first byte. A 22x22 glyph is three bytes per column and
/// 66 in total, with the last two bits of each column unused.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Mono {
    pub width: u8,
    pub height: u8,
    pub bits: Vec<u8>,
}

impl Mono {
    /// The empty bitmap, which is what the app sends when it has nothing to
    /// draw — an unknown app, or a codepoint that is only whitespace.
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

    /// Pack ARGB8888 pixels, row-major, into the wire layout.
    ///
    /// A pixel is lit when its alpha-weighted luma clears [`LUMA_THRESHOLD`],
    /// so a glyph drawn white on a transparent canvas comes out solid and the
    /// antialiased edge falls away.
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

    /// The same, for a picture the watch asked for by a type of its own.
    ///
    /// The workout screen list declares the kinds it wants glyphs in, and the
    /// reply has to name the kind it is answering — the size alone does not
    /// say which, and two kinds can share one.
    pub fn metadata_of(&self, kind: u8) -> ImageMetadata {
        ImageMetadata {
            r#type: kind,
            width: self.width,
            height: self.height,
        }
    }

    /// The bits as `ImageData` objects, in the 64-byte pieces the app sends.
    pub fn data_objects(&self) -> Vec<WppObject> {
        self.chunks()
    }

    /// The bits as `ImageData` objects. An empty bitmap is still one object,
    /// carrying nothing, which is how the app says "no image".
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

/// Is this the watch asking us for `command`?
///
/// The channel matters as much as the opcode: these two commands only ever
/// arrive watch-initiated, and our own reply carries the same opcode back.
fn asks(frame: &Frame, command: Command) -> bool {
    frame.command.opcode() == command.0 && frame.command.channel() == Some(Channel::SlaveRequest)
}

/// A size of image the watch says it can hold, for one image type.
///
/// It declares one of these per type alongside the workout screen list.
/// Observed on a ScanWatch 2: type 0 at 20x20, type 1 at 34x34.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImageFormat {
    pub kind: u8,
    pub width: u8,
    pub height: u8,
}

impl ImageFormat {
    /// Read the declaration the watch sends with its workout screen list.
    ///
    /// `ImageMetadata` also rides in the requests it makes for pictures, so
    /// this only means anything on that reply — hence taking the whole frame
    /// rather than loose objects.
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

/// The size to draw at: what the watch asked for, held to what it says that
/// image type can hold.
///
/// The two disagree. It asks for notification icons at 34x34 tagged type 0,
/// which is the type 1 size and nearly three times the bytes type 0 is worth,
/// and answering literally reboots it. Where the request and the declaration
/// disagree the declaration wins.
fn fit(kind: u8, width: u8, height: u8, formats: &[ImageFormat]) -> (u8, u8) {
    let Some(format) = formats.iter().find(|f| f.kind == kind) else {
        return (width.min(UNDECLARED_MAX), height.min(UNDECLARED_MAX));
    };
    (width.min(format.width), height.min(format.height))
}

/// What to hold an image to before the watch has declared anything.
///
/// The smallest size it has been seen to declare. Answering at the size asked
/// for reboots it, and answering empty is worse than answering small: the
/// watch caches one answer per app and never asks again.
const UNDECLARED_MAX: u8 = 20;

/// The type and size the watch asked for, defaulting to [`DEFAULT_SIZE`] if it
/// named none.
fn requested(objects: &[WppObject]) -> (u8, u8, u8) {
    objects
        .iter()
        .find_map(|o| match o {
            WppObject::ImageMetadata(m) => Some((m.r#type, m.width, m.height)),
            _ => None,
        })
        .unwrap_or((IMAGE_TYPE, DEFAULT_SIZE, DEFAULT_SIZE))
}

/// `CMD_GLYPH_GET`: draw these characters at this size.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GlyphRequest {
    /// One entry per glyph, in the order the watch asked.
    pub glyphs: Vec<Glyph>,
    kind: u8,
    width: u8,
    height: u8,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Glyph {
    /// The character to draw.
    pub codepoint: u32,
    /// The field exactly as it arrived, to be echoed in the reply.
    ///
    /// `GlyphId.unicode` is byte-swapped with respect to the big-endian frame
    /// around it — the app writes it big-endian and reads it back
    /// little-endian — so the number on the wire is not the codepoint and
    /// cannot be re-derived from it once swapped.
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

    /// The size to draw each glyph at.
    ///
    /// Unlike an icon this is the size asked for, not the size declared. The
    /// watch asks for glyphs at 32x34 tagged type 0 and renders whatever comes
    /// back at the top left of that cell, so a bitmap held to the declared
    /// 20x20 arrives at a quarter of the area and looks like a mistake.
    ///
    /// 32x34 is 160 bytes, which is within a few bytes of the 170 that reboots
    /// the watch on the icon path. Whether the two share a budget is untested:
    /// the reboot was only ever measured on `CMD_NOTIFICATION_GET`, and this
    /// is a different command. If a full-size glyph turns out to reboot it
    /// too, the answer is [`fit`] here as well and glyphs stay small.
    pub fn size(&self, _formats: &[ImageFormat]) -> (u8, u8) {
        (self.width, self.height)
    }

    /// One bitmap per glyph, in the order they were asked for.
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
        // The watch says so itself — "WPP_CMD_GLYPH_GET must be sent in
        // multiple packets" — and its accumulator is what joins them again.
        Frame::new(
            Command::CMD_GLYPH_GET.with_channel(Channel::SlaveRequest),
            objects,
        )
    }
}

/// `CMD_NOTIFICATION_GET`: the icon for the app a notification came from.
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

    /// The size to draw at, given what the watch has declared.
    pub fn size(&self, formats: &[ImageFormat]) -> (u8, u8) {
        fit(self.kind, self.width, self.height, formats)
    }

    pub fn reply(&self, icon: &Mono) -> Frame {
        // The id goes back split across as many objects as it takes; a long
        // package name does not fit one.
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
        // A full-size icon does not fit a frame, and one frame too many is
        // what reboots the watch rather than anything about the picture.
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
        // 2 wide, 9 tall: two bytes per column, so the ninth row starts a
        // second byte and the layout cannot be mistaken for row-major.
        let mut pixels = vec![CLEAR; 2 * 9];
        pixels[0] = WHITE; // x=0, y=0
        pixels[8 * 2] = WHITE; // x=0, y=8
        pixels[1] = WHITE; // x=1, y=0
        let mono = Mono::pack(&pixels, 2, 9);
        assert_eq!(mono.bits, vec![0b0000_0001, 0b0000_0001, 0b0000_0001, 0]);
        assert_eq!(mono.bits.len(), 2 * 2);
    }

    #[test]
    fn a_22px_glyph_is_three_bytes_per_column() {
        let mono = Mono::pack(&vec![CLEAR; 22 * 22], 22, 22);
        assert_eq!(mono.bits.len(), 66);
    }

    /// The threshold is on alpha-weighted luma, so white can fall below it on
    /// transparency alone while a dimmer opaque colour stays lit. Antialiased
    /// edges are exactly the first case.
    #[test]
    fn transparency_dims_a_pixel_below_the_threshold() {
        assert_eq!(Mono::pack(&[0xff80_8080], 1, 1).bits, vec![1]);
        assert_eq!(Mono::pack(&[0x60ff_ffff], 1, 1).bits, vec![0]);
        // Half-transparent white is still lit; the cutoff sits below halfway.
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

    /// The codepoint is byte-swapped inside the frame, and the reply repeats
    /// the field as it arrived rather than re-encoding what we decoded.
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

    /// Our own reply carries the same opcode, so matching on the opcode alone
    /// would make the client answer itself if a frame ever came back round.
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

    /// The watch asks for icons at 34x34 tagged type 0, and 34x34 is the
    /// type 1 size. Answering at the size asked for reboots it, so the
    /// declaration wins over the request.
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
        // A request smaller than the limit is left alone.
        assert_eq!(fit(0, 8, 8, &formats), (8, 8));
        // An undeclared type gets the conservative limit, not the request.
        assert_eq!(fit(7, 34, 34, &formats), (20, 20));
        assert_eq!(fit(0, 34, 34, &[]), (20, 20));
    }

    /// `ImageMetadata` rides in the requests too, so reading it out of any
    /// frame would let a request overwrite the declaration with its own size.
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

    /// The size the watch declares for its larger icon, which is what made a
    /// single-frame reply reboot it.
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
