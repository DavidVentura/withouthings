#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    Truncated { needed: usize, available: usize },
    TrailingBytes { remaining: usize },
    InvalidUtf8,
}

impl core::fmt::Display for ParseError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            ParseError::Truncated { needed, available } => {
                write!(f, "truncated: needed {needed} bytes, {available} available")
            }
            ParseError::TrailingBytes { remaining } => {
                write!(f, "{remaining} trailing bytes after the object")
            }
            ParseError::InvalidUtf8 => write!(f, "string field is not valid UTF-8"),
        }
    }
}

impl std::error::Error for ParseError {}

pub struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    pub fn new(buf: &'a [u8]) -> Self {
        Reader { buf, pos: 0 }
    }

    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    pub fn rest(&self) -> &'a [u8] {
        &self.buf[self.pos..]
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], ParseError> {
        if self.remaining() < n {
            return Err(ParseError::Truncated {
                needed: n,
                available: self.remaining(),
            });
        }
        let out = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(out)
    }

    pub fn u8(&mut self) -> Result<u8, ParseError> {
        Ok(self.take(1)?[0])
    }

    pub fn i8(&mut self) -> Result<i8, ParseError> {
        Ok(self.u8()? as i8)
    }

    pub fn u16(&mut self) -> Result<u16, ParseError> {
        Ok(u16::from_be_bytes(self.take(2)?.try_into().unwrap()))
    }

    pub fn i16(&mut self) -> Result<i16, ParseError> {
        Ok(self.u16()? as i16)
    }

    pub fn u32(&mut self) -> Result<u32, ParseError> {
        Ok(u32::from_be_bytes(self.take(4)?.try_into().unwrap()))
    }

    pub fn i32(&mut self) -> Result<i32, ParseError> {
        Ok(self.u32()? as i32)
    }

    pub fn u64(&mut self) -> Result<u64, ParseError> {
        Ok(u64::from_be_bytes(self.take(8)?.try_into().unwrap()))
    }

    pub fn i64(&mut self) -> Result<i64, ParseError> {
        Ok(self.u64()? as i64)
    }

    pub fn bytes(&mut self) -> Result<Vec<u8>, ParseError> {
        let len = self.u8()? as usize;
        Ok(self.take(len)?.to_vec())
    }

    pub fn string(&mut self) -> Result<String, ParseError> {
        String::from_utf8(self.bytes()?).map_err(|_| ParseError::InvalidUtf8)
    }

    pub fn array_u8(&mut self) -> Result<Vec<u8>, ParseError> {
        self.bytes()
    }

    pub fn array_i16(&mut self) -> Result<Vec<i16>, ParseError> {
        let n = self.u8()? as usize;
        (0..n).map(|_| self.i16()).collect()
    }

    pub fn array_u16(&mut self) -> Result<Vec<u16>, ParseError> {
        let n = self.u8()? as usize;
        (0..n).map(|_| self.u16()).collect()
    }

    pub fn array_i32(&mut self) -> Result<Vec<i32>, ParseError> {
        let n = self.u8()? as usize;
        (0..n).map(|_| self.i32()).collect()
    }

    pub fn array_u32(&mut self) -> Result<Vec<u32>, ParseError> {
        let n = self.u8()? as usize;
        (0..n).map(|_| self.u32()).collect()
    }
}

#[derive(Default)]
pub struct Writer {
    buf: Vec<u8>,
}

impl Writer {
    pub fn new() -> Self {
        Writer { buf: Vec::new() }
    }

    pub fn finish(self) -> Vec<u8> {
        self.buf
    }

    pub fn len(&self) -> usize {
        self.buf.len()
    }

    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }

    pub fn pad_to(&mut self, end: usize) {
        if end > self.buf.len() {
            self.buf.resize(end, 0);
        }
    }

    pub fn raw(&mut self, bytes: &[u8]) {
        self.buf.extend_from_slice(bytes);
    }

    pub fn u8(&mut self, v: u8) {
        self.buf.push(v);
    }

    pub fn i8(&mut self, v: i8) {
        self.buf.push(v as u8);
    }

    pub fn u16(&mut self, v: u16) {
        self.buf.extend_from_slice(&v.to_be_bytes());
    }

    pub fn i16(&mut self, v: i16) {
        self.u16(v as u16);
    }

    pub fn u32(&mut self, v: u32) {
        self.buf.extend_from_slice(&v.to_be_bytes());
    }

    pub fn i32(&mut self, v: i32) {
        self.u32(v as u32);
    }

    pub fn u64(&mut self, v: u64) {
        self.buf.extend_from_slice(&v.to_be_bytes());
    }

    pub fn i64(&mut self, v: i64) {
        self.u64(v as u64);
    }

    /// A length above 255 wraps in the single-byte count while the full bytes
    /// are still written after it, corrupting the frame with no error
    /// surfaced.
    pub fn bytes(&mut self, v: &[u8]) {
        self.u8(v.len() as u8);
        self.raw(v);
    }

    pub fn string(&mut self, v: &str) {
        self.bytes(v.as_bytes());
    }

    pub fn array_u8(&mut self, v: &[u8]) {
        self.bytes(v);
    }

    pub fn array_i16(&mut self, v: &[i16]) {
        self.u8(v.len() as u8);
        v.iter().for_each(|&x| self.i16(x));
    }

    pub fn array_u16(&mut self, v: &[u16]) {
        self.u8(v.len() as u8);
        v.iter().for_each(|&x| self.u16(x));
    }

    pub fn array_i32(&mut self, v: &[i32]) {
        self.u8(v.len() as u8);
        v.iter().for_each(|&x| self.i32(x));
    }

    pub fn array_u32(&mut self, v: &[u32]) {
        self.u8(v.len() as u8);
        v.iter().for_each(|&x| self.u32(x));
    }
}

pub trait WppObjectCodec: Sized {
    const TYPE_ID: u16;
    const TYPE_NAME: &'static str;
    const CLASS_NAME: &'static str;

    const FIXED_DATA_SIZE: Option<usize>;

    fn data_size(&self) -> usize;

    fn parse(r: &mut Reader<'_>) -> Result<Self, ParseError>;
    fn write(&self, w: &mut Writer);
}

pub fn parse_object<T: WppObjectCodec>(data: &[u8]) -> Result<T, ParseError> {
    let mut r = Reader::new(data);
    let object = T::parse(&mut r)?;
    let rest = r.rest();
    if !rest.is_empty() {
        let padded = T::FIXED_DATA_SIZE == Some(data.len()) && rest.iter().all(|&b| b == 0);
        if !padded {
            return Err(ParseError::TrailingBytes {
                remaining: rest.len(),
            });
        }
    }
    Ok(object)
}
