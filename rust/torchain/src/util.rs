//! Shared primitives: errors, time, hashing, base32 addresses, exact TOR math.
//!
//! Everything here mirrors `core/validator.py` (locked address regex, 8-decimal
//! native coin) so the Rust ledger and the Python API agree byte-for-byte.

use sha3::{Digest, Sha3_256};
use std::time::{SystemTime, UNIX_EPOCH};

pub const MICRO_PER_TOR: u64 = 100_000_000;
pub const NATIVE_COIN_ID: &str = "TOR111111111111111111111111111111onion";
pub const TOR_DECIMALS: u32 = 8;
pub const ADDRESS_PREFIX: &str = "TOR1";
pub const ADDRESS_SUFFIX: &str = "onion";

#[derive(Debug, Clone)]
pub struct ChainError {
    pub code: String,
    pub message: String,
}

impl ChainError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self { code: code.to_string(), message: message.into() }
    }
    pub fn validation(m: impl Into<String>) -> Self {
        Self::new("VALIDATION_ERROR", m)
    }
    pub fn insufficient(m: impl Into<String>) -> Self {
        Self::new("INSUFFICIENT_FUNDS", m)
    }
    pub fn not_found(m: impl Into<String>) -> Self {
        Self::new("NOT_FOUND", m)
    }
    pub fn unauthorized(m: impl Into<String>) -> Self {
        Self::new("UNAUTHORIZED", m)
    }
    pub fn conflict(m: impl Into<String>) -> Self {
        Self::new("CONFLICT", m)
    }
    pub fn io(e: impl std::fmt::Display) -> Self {
        Self::new("IO_ERROR", e.to_string())
    }
    pub fn json(e: impl std::fmt::Display) -> Self {
        Self::new("BAD_JSON", e.to_string())
    }
}

impl std::fmt::Display for ChainError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{}] {}", self.code, self.message)
    }
}

impl std::error::Error for ChainError {}

pub type ChainResult<T> = Result<T, ChainError>;

pub fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

pub fn sha3_hex(bytes: &[u8]) -> String {
    hex_lower(&sha3_bytes(bytes))
}

pub fn sha3_bytes(bytes: &[u8]) -> [u8; 32] {
    let mut hasher = Sha3_256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    let mut out = [0u8; 32];
    out.copy_from_slice(&digest);
    out
}

pub fn hex_lower(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{:02x}", byte));
    }
    out
}

/// Lowercase RFC 4648 base32 without padding (the address body alphabet).
pub fn base32_lower(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 32] = b"abcdefghijklmnopqrstuvwxyz234567";
    let mut out = String::new();
    let mut buffer: u32 = 0;
    let mut bits: u32 = 0;
    for byte in bytes {
        buffer = (buffer << 8) | (*byte as u32);
        bits += 8;
        while bits >= 5 {
            bits -= 5;
            let index = ((buffer >> bits) & 0x1f) as usize;
            out.push(ALPHABET[index] as char);
        }
    }
    if bits > 0 {
        let index = ((buffer << (5 - bits)) & 0x1f) as usize;
        out.push(ALPHABET[index] as char);
    }
    out
}

/// `TOR1` + base32(sha3-256(pubkey)[..20]) + `onion` — identical to Python.
pub fn address_from_pubkey(pubkey: &[u8]) -> String {
    format!("{}{}{}", ADDRESS_PREFIX, base32_lower(&sha3_bytes(pubkey)[..20]), ADDRESS_SUFFIX)
}

/// Deterministic account address for a liquidity pool (holds the reserves).
pub fn pool_address(symbol: &str) -> String {
    let digest = sha3_bytes(format!("oneness-tor-pool:{symbol}").as_bytes());
    format!("{}{}{}", ADDRESS_PREFIX, base32_lower(&digest[..20]), ADDRESS_SUFFIX)
}

pub fn is_address(value: &str) -> bool {
    let text = value.trim();
    if !text.starts_with(ADDRESS_PREFIX) || !text.ends_with(ADDRESS_SUFFIX) {
        return false;
    }
    let body = &text[ADDRESS_PREFIX.len()..text.len() - ADDRESS_SUFFIX.len()];
    if body.len() < 30 || body.len() > 48 {
        return false;
    }
    body.bytes().all(|b| (b'a'..=b'z').contains(&b) || (b'2'..=b'7').contains(&b))
}

/// Parse a decimal TOR string ("1.5") into microTOR exactly.
pub fn parse_tor(text: &str) -> ChainResult<u64> {
    let raw = text.trim();
    if raw.is_empty() {
        return Err(ChainError::validation("amount is required"));
    }
    if raw.starts_with('-') {
        return Err(ChainError::validation("amount must be positive"));
    }
    let mut parts = raw.splitn(2, '.');
    let whole = parts.next().unwrap_or("0");
    let frac = parts.next().unwrap_or("");
    if !whole.chars().all(|c| c.is_ascii_digit()) || !frac.chars().all(|c| c.is_ascii_digit()) {
        return Err(ChainError::validation(format!("cannot parse TOR amount {raw:?}")));
    }
    if frac.len() > TOR_DECIMALS as usize {
        return Err(ChainError::validation(format!(
            "at most {TOR_DECIMALS} decimals are allowed"
        )));
    }
    let whole_value: u128 = if whole.is_empty() { 0 } else { whole.parse::<u128>().map_err(|_| ChainError::validation("amount too large"))? };
    let mut frac_value: u128 = if frac.is_empty() { 0 } else { frac.parse::<u128>().map_err(|_| ChainError::validation("bad decimals"))? };
    for _ in frac.len()..TOR_DECIMALS as usize {
        frac_value *= 10;
    }
    let total = whole_value
        .checked_mul(MICRO_PER_TOR as u128)
        .and_then(|v| v.checked_add(frac_value))
        .ok_or_else(|| ChainError::validation("amount too large"))?;
    if total == 0 {
        return Err(ChainError::validation("amount must be greater than zero"));
    }
    if total > u64::MAX as u128 {
        return Err(ChainError::validation("amount too large"));
    }
    Ok(total as u64)
}

/// microTOR -> display string with trailing zeros trimmed.
pub fn format_tor(micro: u64) -> String {
    let whole = micro / MICRO_PER_TOR;
    let frac = micro % MICRO_PER_TOR;
    if frac == 0 {
        return whole.to_string();
    }
    let text = format!("{:0width$}", frac, width = TOR_DECIMALS as usize);
    let trimmed = text.trim_end_matches('0');
    format!("{whole}.{trimmed}")
}

pub fn tor_value(micro: u64) -> f64 {
    (micro as f64) / (MICRO_PER_TOR as f64)
}
