//! Canonical TOR coin metadata — name, symbol, decimals and the pinned logo.
//!
//! One place only: `GET /api/chain/info`, `GET /api/pricing`, `GET /api/coin`
//! and the `torchain coin` CLI all render this module, so every wallet, page and
//! the AI describe the coin identically. `core/coin.py` mirrors these constants
//! for the Python layer (a test asserts the two stay in sync).
//!
//! The logo is pinned to IPFS; both URIs are published because NFT/token tooling
//! wants `ipfs://<cid>` while browsers and `<img src>` want the HTTPS gateway.
//! Override at runtime with `TOR_LOGO_URL` / `TOR_LOGO_IPFS` (private gateway,
//! re-pinned file) — nothing else in the node has to change.

use serde_json::{json, Value};

use crate::util::{NATIVE_COIN_ID, TOR_DECIMALS};

pub const COIN_NAME: &str = "OneNess TOR";
pub const COIN_SYMBOL: &str = "TOR";
pub const COIN_DESCRIPTION: &str = "TOR is the native coin of the OneNess TOR chain — a Tor-first, \
privacy-preserving ledger with a permanent block archive. The sending wallet chooses public \
or anonymous for every transaction, and an anonymous send is recorded as Tor-Anonymous-onion. \
Genesis mints 50,000,000 TOR to the founder wallet; fees are paid in TOR.";

/// Pinned logo, content addressed (IPFS CIDv1, sha2-256).
pub const LOGO_CID: &str =
    "bafybeihur4eo7oifxqahw7ze3o6ujvy7ltfmzmfkiq46j33jzwurl2w3ze";
/// Same file over the Pinata gateway (token included so browsers can load it).
pub const LOGO_URL: &str = "https://cyan-deep-magpie-676.mypinata.cloud/ipfs/\
bafybeihur4eo7oifxqahw7ze3o6ujvy7ltfmzmfkiq46j33jzwurl2w3ze\
?pinataGatewayToken=fvfdMA6s8XyRJ3C2Lp6_ScBrMLuuvUoo5tKWCGpXCkPdYVsm6mW_TWXlXE9PYi7A";
/// `ipfs://` form of the same CID.
pub const LOGO_IPFS: &str =
    "ipfs://bafybeihur4eo7oifxqahw7ze3o6ujvy7ltfmzmfkiq46j33jzwurl2w3ze";

/// `ipfs://…` form of the logo (env `TOR_LOGO_IPFS` wins).
pub fn logo_ipfs() -> String {
    env_or("TOR_LOGO_IPFS", LOGO_IPFS)
}

/// HTTPS form of the logo (env `TOR_LOGO_URL` wins).
pub fn logo_url() -> String {
    env_or("TOR_LOGO_URL", LOGO_URL)
}

fn env_or(key: &str, fallback: &str) -> String {
    match std::env::var(key) {
        Ok(value) if !value.trim().is_empty() => value.trim().to_string(),
        _ => fallback.to_string(),
    }
}

/// Compact block embedded in `/api/chain/info` and `/api/pricing`.
pub fn as_json() -> Value {
    json!({
        "name": COIN_NAME,
        "symbol": COIN_SYMBOL,
        "decimals": TOR_DECIMALS,
        "native_coin_id": NATIVE_COIN_ID,
        "description": COIN_DESCRIPTION,
        "logo_url": logo_url(),
        "logo_ipfs": logo_ipfs(),
        "image": logo_url(),
    })
}

/// Full document served by `GET /api/coin` (`torchain coin`): the keys wallets
/// and marketplaces read (`name`, `symbol`, `description`, `image`) plus the
/// chain-specific extras, so it can be pinned as the coin's metadata JSON as-is.
pub fn as_metadata() -> Value {
    json!({
        "standard": "oneness-coin-metadata/1",
        "name": COIN_NAME,
        "symbol": COIN_SYMBOL,
        "decimals": TOR_DECIMALS,
        "description": COIN_DESCRIPTION,
        "image": logo_url(),
        "logo_url": logo_url(),
        "logo_ipfs": logo_ipfs(),
        "native_coin_id": NATIVE_COIN_ID,
        "properties": {
            "chain": "OneNess TOR",
            "chain_id": "072316",
            "consensus": "Proof-of-Authority (single authorized proposer)",
            "genesis_supply_tor": 50_000_000,
            "privacy": "per transaction: the sending wallet chooses public or anonymous",
            "logo_cid": LOGO_CID,
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn logo_uris_point_at_the_same_cid() {
        assert!(LOGO_URL.contains(LOGO_CID));
        assert!(LOGO_IPFS.ends_with(LOGO_CID));
        assert!(logo_url().contains("pinataGatewayToken="));
    }

    #[test]
    fn metadata_has_the_wallet_keys() {
        let doc = as_metadata();
        for key in ["name", "symbol", "decimals", "description", "image"] {
            assert!(!doc[key].is_null(), "missing {key}");
        }
        assert_eq!(doc["decimals"], TOR_DECIMALS);
        assert_eq!(doc["image"], doc["logo_url"]);
    }
}
