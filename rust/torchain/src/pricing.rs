//! Mirrors `core/pricing.py`. Rust never invents a fee — the Python core is the
//! source of truth and this table must stay identical to it (locked rows first).

use crate::util::{ChainError, ChainResult, MICRO_PER_TOR};

pub struct PriceRow {
    pub code: &'static str,
    pub label: &'static str,
    pub tor: f64,
    pub locked: bool,
}

pub const PRICE_TABLE: &[PriceRow] = &[
    PriceRow { code: "contract_create", label: "Create a smart contract", tor: 5.0, locked: true },
    PriceRow { code: "mint", label: "Mint tokens via the integrated minter", tor: 3.0, locked: true },
    PriceRow { code: "transfer", label: "Coin/token transfer", tor: 0.1, locked: true },
    PriceRow { code: "meme_token_create", label: "Meme coin create + metadata", tor: 5.0, locked: false },
    PriceRow { code: "meme_pool_create", label: "Launch liquidity pool (platform share)", tor: 10.0, locked: false },
    PriceRow { code: "meme_volume_session", label: "Volume & market bot — 15-minute session", tor: 10.0, locked: false },
    PriceRow { code: "meme_landing_page", label: "AI token landing page (generated + hosted)", tor: 10.0, locked: false },
    PriceRow { code: "meme_trending_day", label: "Trending promotion — per day", tor: 5.0, locked: false },
    PriceRow { code: "meme_banner_7d", label: "Banner advertisement — 7 days", tor: 30.0, locked: false },
    PriceRow { code: "nft_mint", label: "AI-NFT mint (1-of-1)", tor: 3.0, locked: false },
];

pub fn row(code: &str) -> ChainResult<&'static PriceRow> {
    PRICE_TABLE
        .iter()
        .find(|item| item.code == code)
        .ok_or_else(|| ChainError::validation(format!("unknown price code {code:?}")))
}

/// Price in microTOR for one unit of `code`.
pub fn price_micro(code: &str) -> ChainResult<u64> {
    let found = row(code)?;
    Ok((found.tor * MICRO_PER_TOR as f64).round() as u64)
}

/// Price in microTOR for `quantity` units (rounded once).
pub fn total_micro(code: &str, quantity: f64) -> ChainResult<u64> {
    let found = row(code)?;
    Ok((found.tor * quantity * MICRO_PER_TOR as f64).round() as u64)
}

pub fn as_json() -> Vec<serde_json::Value> {
    PRICE_TABLE
        .iter()
        .map(|item| {
            serde_json::json!({
                "code": item.code,
                "label": item.label,
                "tor": item.tor,
                "micro_tor": (item.tor * MICRO_PER_TOR as f64).round() as u64,
                "locked": item.locked,
            })
        })
        .collect()
}
