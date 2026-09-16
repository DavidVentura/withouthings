use std::fmt;

/// The watch's settings database, as it sits in the external SPI flash: a run
/// of `[ie u16][len u16][value]` entries per 4 KiB region, ended by the `END`
/// marker and a 16-bit sum of everything before it.
const REGION_LEN: usize = 0x1000;
const REGION_COUNT: usize = 8;
const IE_END: u16 = 0xffff;
const IE_MAC: u16 = 0x01;
const IE_AUTH_DATA: u16 = 0x79;
const SECRET_LEN: usize = 32;

#[derive(Debug)]
pub enum DumpError {
    NoValidRegion,
    Missing(&'static str),
    SecretNotAscii,
}

impl fmt::Display for DumpError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DumpError::NoValidRegion => write!(f, "no dblib region in the dump has a valid checksum"),
            DumpError::Missing(what) => write!(f, "the dump's dblib holds no {what}"),
            DumpError::SecretNotAscii => write!(f, "the association secret is not 32 ASCII bytes"),
        }
    }
}

/// What a caller needs out of a flash dump to answer a probe challenge.
pub struct Association {
    pub mac: String,
    pub secret: String,
}

impl fmt::Debug for Association {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "Association {{ mac: {}, secret: {} bytes, fnv1a {:08x} }}",
            self.mac,
            self.secret.len(),
            fnv1a(self.secret.as_bytes())
        )
    }
}

pub fn association(dump: &[u8]) -> Result<Association, DumpError> {
    let entries = regions(dump)?;
    let mac = entries
        .iter()
        .find(|(ie, _)| *ie == IE_MAC)
        .ok_or(DumpError::Missing("MAC"))?;
    let auth = entries
        .iter()
        .find(|(ie, _)| *ie == IE_AUTH_DATA)
        .ok_or(DumpError::Missing("association secret"))?;
    if auth.1.len() < SECRET_LEN {
        return Err(DumpError::SecretNotAscii);
    }
    let secret = &auth.1[..SECRET_LEN];
    if !secret.iter().all(|b| b.is_ascii_graphic()) {
        return Err(DumpError::SecretNotAscii);
    }
    Ok(Association {
        mac: String::from_utf8_lossy(trim_nul(&mac.1)).to_ascii_lowercase(),
        secret: String::from_utf8_lossy(secret).into_owned(),
    })
}

fn trim_nul(value: &[u8]) -> &[u8] {
    match value.iter().position(|b| *b == 0) {
        Some(end) => &value[..end],
        None => value,
    }
}

/// dblib is spread over several regions and each is rewritten whole, so a
/// region that does not check out is a half-written copy rather than an error.
fn regions(dump: &[u8]) -> Result<Vec<(u16, Vec<u8>)>, DumpError> {
    let mut entries = Vec::new();
    for region in 0..REGION_COUNT {
        let start = region * REGION_LEN;
        let Some(bytes) = dump.get(start..start + REGION_LEN) else {
            break;
        };
        if let Some(parsed) = parse_region(bytes) {
            entries.extend(parsed);
        }
    }
    if entries.is_empty() {
        return Err(DumpError::NoValidRegion);
    }
    Ok(entries)
}

fn parse_region(bytes: &[u8]) -> Option<Vec<(u16, Vec<u8>)>> {
    let mut entries = Vec::new();
    let mut offset = 0usize;
    while offset + 4 <= bytes.len() {
        let ie = u16::from_le_bytes([bytes[offset], bytes[offset + 1]]);
        let len = u16::from_le_bytes([bytes[offset + 2], bytes[offset + 3]]) as usize;
        if ie == IE_END {
            // The end marker is `[0xffff][update count u32][sum u16]`, and the
            // sum covers every byte up to it, the count included.
            let sum: u32 = bytes.get(..offset + 6)?.iter().map(|b| *b as u32).sum();
            let stored = u16::from_le_bytes([*bytes.get(offset + 6)?, *bytes.get(offset + 7)?]);
            if (sum % 0x10000) as u16 != stored {
                return None;
            }
            return Some(entries);
        }
        let value = bytes.get(offset + 4..offset + 4 + len)?;
        entries.push((ie, value.to_vec()));
        offset += 4 + len;
    }
    None
}

fn fnv1a(data: &[u8]) -> u32 {
    let mut hash: u32 = 0x811c9dc5;
    for byte in data {
        hash ^= *byte as u32;
        hash = hash.wrapping_mul(0x01000193);
    }
    hash
}

#[cfg(test)]
mod tests {
    use super::*;

    fn region(entries: &[(u16, &[u8])]) -> Vec<u8> {
        let mut bytes = Vec::new();
        for (ie, value) in entries {
            bytes.extend_from_slice(&ie.to_le_bytes());
            bytes.extend_from_slice(&(value.len() as u16).to_le_bytes());
            bytes.extend_from_slice(value);
        }
        bytes.extend_from_slice(&IE_END.to_le_bytes());
        bytes.extend_from_slice(&7u32.to_le_bytes());
        let sum: u32 = bytes.iter().map(|b| *b as u32).sum();
        bytes.extend_from_slice(&((sum % 0x10000) as u16).to_le_bytes());
        bytes.resize(REGION_LEN, 0xff);
        bytes
    }

    #[test]
    fn the_mac_and_the_secret_come_out_of_a_region() {
        let secret = b"0123456789abcdef0123456789abcdef";
        let mut dump = region(&[]);
        dump.extend(region(&[
            (IE_MAC, b"a4:7e:fa:44:d6:10\0"),
            (IE_AUTH_DATA, &[secret.as_slice(), &[0]].concat()),
        ]));
        let association = association(&dump).unwrap();
        assert_eq!(association.mac, "a4:7e:fa:44:d6:10");
        assert_eq!(association.secret.as_bytes(), secret);
    }

    #[test]
    fn a_half_written_region_is_skipped_rather_than_parsed() {
        let mut broken = region(&[(IE_MAC, b"a4:7e:fa:44:d6:10\0")]);
        broken[4] = 0x41;
        assert!(matches!(association(&broken), Err(DumpError::NoValidRegion)));
    }
}
