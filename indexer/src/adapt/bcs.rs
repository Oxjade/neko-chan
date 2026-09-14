//! Minimal, exact BCS reader. Only what Move structs/events use in practice:
//! fixed-width LE integers (u8…u256), bool, uleb128 vector lengths, option
//! (0/1 prefix), and 32-byte addresses. All financial amounts are decoded as
//! integers — never floats.

use anyhow::{bail, Result};

const HEX: &[u8; 16] = b"0123456789abcdef";

pub struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    pub fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }

    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        if self.pos + n > self.buf.len() {
            bail!("bcs eof at byte {} (need {n})", self.pos);
        }
        let out = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(out)
    }

    pub fn read_u8(&mut self) -> Result<u8> {
        Ok(self.take(1)?[0])
    }

    pub fn read_bool(&mut self) -> Result<bool> {
        match self.read_u8()? {
            0 => Ok(false),
            1 => Ok(true),
            b => bail!("invalid bool byte {b} at {}", self.pos - 1),
        }
    }

    pub fn read_uleb(&mut self) -> Result<u64> {
        let mut value = 0u64;
        let mut shift = 0u32;
        for _ in 0..10 {
            let byte = self.read_u8()?;
            value |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return Ok(value);
            }
            shift += 7;
        }
        bail!("uleb128 too long at {}", self.pos)
    }

    pub fn read_u16(&mut self) -> Result<u16> {
        let b = self.take(2)?;
        Ok(u16::from_le_bytes([b[0], b[1]]))
    }

    pub fn read_u32(&mut self) -> Result<u32> {
        let b = self.take(4)?;
        Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
    }

    pub fn read_u64(&mut self) -> Result<u64> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }

    pub fn read_u128(&mut self) -> Result<u128> {
        let b = self.take(16)?;
        let mut arr = [0u8; 16];
        arr.copy_from_slice(b);
        Ok(u128::from_le_bytes(arr))
    }

    pub fn read_u256(&mut self) -> Result<[u8; 32]> {
        let b = self.take(32)?;
        let mut arr = [0u8; 32];
        arr.copy_from_slice(b);
        Ok(arr)
    }

    pub fn read_address(&mut self) -> Result<String> {
        let b = self.take(32)?;
        let mut s = String::with_capacity(66);
        s.push_str("0x");
        for byte in b {
            s.push(HEX[(byte >> 4) as usize] as char);
            s.push(HEX[(byte & 0x0f) as usize] as char);
        }
        Ok(s)
    }

    /// BCS vector: uleb128 length then items (caller reads items).
    pub fn read_vec_len(&mut self) -> Result<usize> {
        let len = self.read_uleb()?;
        let len: usize = len.try_into().map_err(|_| anyhow::anyhow!("vector length overflow"))?;
        if len > self.remaining() {
            bail!("vector length {len} exceeds remaining {} bytes", self.remaining());
        }
        Ok(len)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_primitives() {
        let buf = vec![1u8, 0xB4, 0xF3, 0, 1, 2, 3, 0x78];
        let mut r = Reader::new(&buf);
        r.read_bool().unwrap();
        r.read_u16().unwrap();
        assert_eq!(r.read_u32().unwrap(), 0x03020100);
        assert_eq!(r.read_u8().unwrap(), 0x78);
    }

    #[test]
    fn uleb() {
        let mut r = Reader::new(&[0xE5, 0x8E, 0x26]);
        assert_eq!(r.read_uleb().unwrap(), 624485);
    }

    #[test]
    fn address() {
        let mut r = Reader::new(&[0u8; 32]);
        assert_eq!(
            r.read_address().unwrap(),
            "0x0000000000000000000000000000000000000000000000000000000000000000"
        );
    }

    #[test]
    fn eof() {
        let mut r = Reader::new(&[1, 2, 3]);
        assert!(r.read_u64().is_err());
    }
}