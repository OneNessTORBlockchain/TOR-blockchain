//! The permanent ledger: accounts, tokens, AMM pools, signed transactions, blocks.
//!
//! Every state change is a signed transaction committed in a block; blocks are
//! append-only (`blocks.jsonl`) and the live state is a checkpoint (`state.json`).
//! Records are permanent: the ledger no longer expires or seals anything. The
//! only privacy choice is made by the sending wallet, per transaction, and it
//! rides in `external_ref` (`oneness-tor/privacy/1`) — the Python API layer masks
//! the sending address on every public projection when it says `anonymous`.

use std::collections::{BTreeMap, HashMap};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::pricing;
use crate::util::{
    address_from_pubkey, format_tor, is_address, now_secs, parse_tor, pool_address, sha3_hex,
    tor_value, ChainError, ChainResult, MICRO_PER_TOR, NATIVE_COIN_ID, TOR_DECIMALS,
};
use crate::wallet::verify_signature;

pub const CHAIN_ID: &str = "072316";
pub const PROTOCOL_VERSION: u32 = 1;
pub const SWAP_FEE_BPS: u64 = 30; // 0.30%
pub const MAX_CLOCK_DRIFT_SECS: i64 = 900;

/// The base layer charges **nothing** to include a transaction. Every price in
/// `pricing.rs` (transfer included) is the *node/platform* fee schedule that the
/// Python API enforces on top of the free ledger; the chain itself only moves
/// value. Keeping this constant at 0 is what `test_transfer_fee_and_supply_
/// conservation` proves: a transfer costs exactly the amount transferred.
pub const BASE_TX_FEE_MICRO: u64 = 0;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Account {
    pub address: String,
    pub pubkey_base58: String,
    pub balance: u64,
    #[serde(default)]
    pub tokens: BTreeMap<String, u64>,
    pub nonce: u64,
    pub role: String,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Token {
    pub symbol: String,
    pub name: String,
    pub supply: u64,
    pub decimals: u8,
    pub owner: String,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Pool {
    pub token: String,
    pub tor_reserve: u64,
    pub token_reserve: u64,
    pub fee_bps: u64,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TxRecord {
    pub txid: String,
    pub kind: String,
    pub from: String,
    pub to: String,
    pub amount: u64,
    pub token: Option<String>,
    pub fee: u64,
    pub note: String,
    pub external_ref: String,
    pub signature: String,
    pub seq: u64,
    pub timestamp: i64,
    pub height: u64,
    pub block_hash: String,
    pub visibility: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Block {
    pub height: u64,
    pub prev_hash: String,
    pub timestamp: i64,
    pub proposer: String,
    pub state_root: String,
    pub txids: Vec<String>,
    pub hash: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct State {
    pub chain_id: String,
    pub protocol_version: u32,
    pub native_coin_id: String,
    pub height: u64,
    pub head_hash: String,
    pub genesis_hash: String,
    pub total_supply: u64,
    pub founder: String,
    pub fee_receiver: String,
    pub admin_token_sha256: String,
    pub created_at: i64,
    pub seq: u64,
    pub accounts: BTreeMap<String, Account>,
    pub tokens: BTreeMap<String, Token>,
    pub pools: BTreeMap<String, Pool>,
    pub records: Vec<TxRecord>,
}

/// What a client signs. `from` is the signer's TOR address.
#[derive(Debug, Clone, Deserialize)]
pub struct SignedTx {
    pub kind: String,
    pub from: String,
    #[serde(default)]
    pub to: String,
    #[serde(default)]
    pub amount: u64,
    #[serde(default)]
    pub token: Option<String>,
    #[serde(default)]
    pub note: String,
    #[serde(default)]
    pub external_ref: String,
    pub nonce: u64,
    pub timestamp: i64,
    pub pubkey_base58: String,
    pub signature: String,
}

impl SignedTx {
    /// Canonical signing message — identical string is used by the Python client.
    pub fn message(&self) -> Vec<u8> {
        let token = self.token.clone().unwrap_or_else(|| "TOR".to_string());
        format!(
            "oneness-tor:v1|{}|{}|{}|{}|{}|{}|{}|{}",
            self.kind, self.from, self.to, self.amount, token, self.note.trim(),
            self.nonce, self.timestamp
        )
        .into_bytes()
    }
}

pub struct RecordDraft {
    pub kind: String,
    pub from: String,
    pub to: String,
    pub amount: u64,
    pub token: Option<String>,
    pub fee: u64,
    pub note: String,
    pub external_ref: String,
    pub signature: String,
}

pub struct Chain {
    pub dir: PathBuf,
    pub state: State,
    pub blocks: Vec<Block>,
}

fn ensure_dir(path: &Path) -> ChainResult<()> {
    std::fs::create_dir_all(path).map_err(|e| ChainError::io(format!("mkdir {path:?}: {e}")))
}

fn state_path(dir: &Path) -> PathBuf {
    dir.join("state.json")
}

fn blocks_path(dir: &Path) -> PathBuf {
    dir.join("blocks.jsonl")
}


fn block_digest(block: &Block, txids: &[String]) -> ChainResult<String> {
    let payload = serde_json::json!({
        "height": block.height,
        "prev_hash": block.prev_hash,
        "timestamp": block.timestamp,
        "proposer": block.proposer,
        "state_root": block.state_root,
        "txids": txids,
    });
    let text = serde_json::to_string(&payload).map_err(|e| ChainError::json(e))?;
    Ok(sha3_hex(text.as_bytes()))
}

fn state_root_of(state: &State) -> String {
    let mut lines: Vec<String> = Vec::new();
    for (address, account) in state.accounts.iter() {
        let mut tokens: Vec<String> = account
            .tokens
            .iter()
            .map(|(symbol, amount)| format!("{symbol}={amount}"))
            .collect();
        tokens.sort();
        lines.push(format!(
            "a|{}|{}|{}|{}",
            address,
            account.balance,
            account.nonce,
            tokens.join(",")
        ));
    }
    for (symbol, token) in state.tokens.iter() {
        lines.push(format!("t|{}|{}|{}|{}", symbol, token.supply, token.decimals, token.owner));
    }
    for (symbol, pool) in state.pools.iter() {
        lines.push(format!(
            "p|{}|{}|{}|{}",
            symbol, pool.tor_reserve, pool.token_reserve, pool.fee_bps
        ));
    }
    lines.push(format!("s|{}|{}", state.total_supply, state.height));
    sha3_hex(lines.join("\n").as_bytes())
}

/// Replication format: the single proposer exports, every replica verifies.
pub const SNAPSHOT_STANDARD: &str = "oneness-tor-ledger-snapshot/1";

/// The permanent public read model: every balance, every token and every
/// per-token total. It is *public data* — a ledger anybody may audit.
pub const PUBLIC_LEDGER_STANDARD: &str = "oneness-tor-public-ledger/1";

/// Re-hash a snapshot exactly the way this ledger hashes its own chain.
///
/// A replica must never adopt bytes it cannot prove: every block is
/// re-digested with :func:`block_digest`, the `prev_hash` links are walked,
/// block 0 must equal `state.genesis_hash`, the head must equal
/// `state.head_hash`, the head's `state_root` must equal the root recomputed
/// from `state`, and every txid a block lists must exist in `state.records`
/// with the same height and block hash. Anything else is refused.
pub fn verify_snapshot(value: &serde_json::Value) -> ChainResult<(State, Vec<Block>)> {
    let object = value
        .as_object()
        .ok_or_else(|| ChainError::validation("snapshot must be a JSON object"))?;
    let state_value = object
        .get("state")
        .cloned()
        .ok_or_else(|| ChainError::validation("snapshot.state is required"))?;
    let state: State = serde_json::from_value(state_value)
        .map_err(|e| ChainError::json(format!("snapshot.state: {e}")))?;
    let raw_blocks = object
        .get("blocks")
        .and_then(|value| value.as_array())
        .ok_or_else(|| ChainError::validation("snapshot.blocks must be an array"))?;
    if raw_blocks.is_empty() {
        return Err(ChainError::validation("snapshot.blocks is empty"));
    }

    let mut blocks: Vec<Block> = Vec::with_capacity(raw_blocks.len());
    for (index, item) in raw_blocks.iter().enumerate() {
        let block_value = item.get("block").cloned().unwrap_or_else(|| item.clone());
        let block: Block = serde_json::from_value(block_value)
            .map_err(|e| ChainError::json(format!("snapshot.blocks[{index}]: {e}")))?;
        if block.height != index as u64 {
            return Err(ChainError::validation(format!(
                "snapshot.blocks[{index}] has height {} (expected {index})",
                block.height
            )));
        }
        blocks.push(block);
    }
    let head = blocks
        .last()
        .cloned()
        .ok_or_else(|| ChainError::validation("snapshot.blocks is empty"))?;

    let mut seen: HashMap<String, (u64, String)> = HashMap::new();
    for record in state.records.iter() {
        seen.insert(record.txid.clone(), (record.height, record.block_hash.clone()));
    }

    let mut previous = "0".repeat(64);
    for block in blocks.iter() {
        if block.prev_hash != previous {
            return Err(ChainError::validation(format!(
                "block {} does not link to its predecessor",
                block.height
            )));
        }
        let recomputed = block_digest(block, &block.txids)?;
        if recomputed != block.hash {
            return Err(ChainError::validation(format!(
                "block {} stored hash does not re-hash",
                block.height
            )));
        }
        for txid in block.txids.iter() {
            match seen.get(txid) {
                Some((height, block_hash))
                    if *height == block.height && block_hash == &block.hash => {}
                Some(_) => {
                    return Err(ChainError::validation(format!(
                        "block {} lists {txid} but the state places it elsewhere",
                        block.height
                    )))
                }
                None => {
                    return Err(ChainError::validation(format!(
                        "block {} lists {txid} but state.records has no such record",
                        block.height
                    )))
                }
            }
        }
        previous = block.hash.clone();
    }
    if blocks[0].hash != state.genesis_hash {
        return Err(ChainError::validation(
            "block 0 hash is not state.genesis_hash",
        ));
    }
    if previous != state.head_hash {
        return Err(ChainError::validation(
            "the newest block hash is not state.head_hash",
        ));
    }
    if head.height != state.height {
        return Err(ChainError::validation(format!(
            "state.height is {} but the newest block is {}",
            state.height, head.height
        )));
    }
    if state_root_of(&state) != head.state_root {
        return Err(ChainError::validation(
            "state does not hash to the head block's state_root",
        ));
    }
    if state.total_supply == 0 {
        return Err(ChainError::validation("state.total_supply is zero"));
    }
    Ok((state, blocks))
}

fn make_record(draft: &RecordDraft, seq: u64, height: u64, timestamp: i64) -> TxRecord {
    let token = draft.token.clone();
    let token_key = token.clone().unwrap_or_else(|| "TOR".to_string());
    let txid = sha3_hex(
        format!(
            "{}|{}|{}|{}|{}|{}|{}|{}|{}|{}",
            draft.kind,
            draft.from,
            draft.to,
            draft.amount,
            token_key,
            draft.fee,
            draft.note.trim(),
            seq,
            timestamp,
            draft.signature
        )
        .as_bytes(),
    );
    TxRecord {
        txid,
        kind: draft.kind.clone(),
        from: draft.from.clone(),
        to: draft.to.clone(),
        amount: draft.amount,
        token,
        fee: draft.fee,
        note: draft.note.trim().to_string(),
        external_ref: draft.external_ref.clone(),
        signature: draft.signature.clone(),
        seq,
        timestamp,
        height,
        block_hash: String::new(),
        visibility: "public".to_string(),
    }
}

impl Chain {
    pub fn open(dir: &Path) -> ChainResult<Chain> {
        let path = state_path(dir);
        if !path.exists() {
            return Err(ChainError::not_found(format!(
                "no chain at {dir:?} — run `torchain init --founder <wallet.json>` first"
            )));
        }
        let text = std::fs::read_to_string(&path)
            .map_err(|e| ChainError::io(format!("read {path:?}: {e}")))?;
        let state: State = serde_json::from_str(&text)
            .map_err(|e| ChainError::json(format!("state.json is corrupt: {e}")))?;

        let mut blocks: Vec<Block> = Vec::new();
        let bpath = blocks_path(dir);
        if bpath.exists() {
            let raw = std::fs::read_to_string(&bpath)
                .map_err(|e| ChainError::io(format!("read {bpath:?}: {e}")))?;
            for (index, line) in raw.lines().enumerate() {
                if line.trim().is_empty() {
                    continue;
                }
                let stored: serde_json::Value = serde_json::from_str(line).map_err(|e| {
                    ChainError::json(format!("blocks.jsonl line {}: {e}", index + 1))
                })?;
                let block: Block = serde_json::from_value(stored["block"].clone())
                    .map_err(|e| ChainError::json(format!("blocks.jsonl line {}: {e}", index + 1)))?;
                blocks.push(block);
            }
        }
        Ok(Chain { dir: dir.to_path_buf(), state, blocks })
    }

    pub fn save(&self) -> ChainResult<()> {
        ensure_dir(&self.dir)?;
        let text = serde_json::to_string_pretty(&self.state).map_err(|e| ChainError::json(e))?;
        let target = state_path(&self.dir);
        let temp = self.dir.join("state.json.tmp");
        std::fs::write(&temp, text + "\n")
            .map_err(|e| ChainError::io(format!("write {temp:?}: {e}")))?;
        std::fs::rename(&temp, &target)
            .map_err(|e| ChainError::io(format!("rename to {target:?}: {e}")))?;
        Ok(())
    }

    fn append_block(&self, block: &Block, txs: &[TxRecord]) -> ChainResult<()> {
        ensure_dir(&self.dir)?;
        let payload = serde_json::json!({ "block": block, "txs": txs });
        let line = serde_json::to_string(&payload).map_err(|e| ChainError::json(e))?;
        let mut handle = OpenOptions::new()
            .create(true)
            .append(true)
            .open(blocks_path(&self.dir))
            .map_err(|e| ChainError::io(format!("open blocks.jsonl: {e}")))?;
        writeln!(handle, "{line}").map_err(|e| ChainError::io(format!("append block: {e}")))
    }

    pub fn state_root(&self) -> String {
        state_root_of(&self.state)
    }

    /// The whole ledger as one verifiable JSON document (replica pull source).
    pub fn snapshot(&self) -> serde_json::Value {
        let mut by_height: HashMap<u64, Vec<TxRecord>> = HashMap::new();
        for record in self.state.records.iter() {
            by_height
                .entry(record.height)
                .or_default()
                .push(record.clone());
        }
        let blocks: Vec<serde_json::Value> = self
            .blocks
            .iter()
            .map(|block| {
                serde_json::json!({
                    "block": block,
                    "txs": by_height.get(&block.height).cloned().unwrap_or_default(),
                })
            })
            .collect();
        serde_json::json!({
            "standard": SNAPSHOT_STANDARD,
            "chain_id": &self.state.chain_id,
            "genesis_hash": &self.state.genesis_hash,
            "head_hash": &self.state.head_hash,
            "height": self.state.height,
            "state_root": state_root_of(&self.state),
            "tokens": self.state.tokens.len(),
            "accounts": self.state.accounts.len(),
            "records": self.state.records.len(),
            "state": &self.state,
            "blocks": blocks,
        })
    }

    /// Adopt a verified snapshot: `state.json` + `blocks.jsonl` are replaced
    /// atomically, so a crash mid-import leaves the previous ledger intact.
    ///
    /// ``keep_admin_token`` preserves the receiving node's own admin token hash.
    /// That hash is node-local authentication (it is **not** part of
    /// ``state_root``), so a replica keeps its own token while the ledger
    /// content stays byte-identical to the origin.
    pub fn import_snapshot(
        dir: &Path,
        value: &serde_json::Value,
        keep_admin_token: Option<&str>,
    ) -> ChainResult<Chain> {
        let (mut state, blocks) = verify_snapshot(value)?;
        if let Some(hash) = keep_admin_token {
            if !hash.trim().is_empty() {
                state.admin_token_sha256 = hash.trim().to_string();
            }
        }
        ensure_dir(dir)?;
        let mut by_height: HashMap<u64, Vec<TxRecord>> = HashMap::new();
        for record in state.records.iter() {
            by_height
                .entry(record.height)
                .or_default()
                .push(record.clone());
        }
        let mut lines = String::new();
        for block in blocks.iter() {
            let payload = serde_json::json!({
                "block": block,
                "txs": by_height.get(&block.height).cloned().unwrap_or_default(),
            });
            lines.push_str(&serde_json::to_string(&payload).map_err(|e| ChainError::json(e))?);
            lines.push('\n');
        }
        let blocks_temp = dir.join("blocks.jsonl.tmp");
        std::fs::write(&blocks_temp, lines)
            .map_err(|e| ChainError::io(format!("write {blocks_temp:?}: {e}")))?;
        let state_temp = dir.join("state.json.tmp");
        let text = serde_json::to_string_pretty(&state).map_err(|e| ChainError::json(e))?;
        std::fs::write(&state_temp, text + "\n")
            .map_err(|e| ChainError::io(format!("write {state_temp:?}: {e}")))?;
        std::fs::rename(&blocks_temp, blocks_path(dir))
            .map_err(|e| ChainError::io(format!("rename blocks.jsonl: {e}")))?;
        std::fs::rename(&state_temp, state_path(dir))
            .map_err(|e| ChainError::io(format!("rename state.json: {e}")))?;
        Ok(Chain {
            dir: dir.to_path_buf(),
            state,
            blocks,
        })
    }

    /// Create the chain: block 0 allocates the whole supply to the founder wallet.
    pub fn init(
        dir: &Path,
        founder_address: &str,
        founder_pubkey: &str,
        supply_micro: u64,
        admin_token: &str,
    ) -> ChainResult<Chain> {
        if state_path(dir).exists() {
            return Err(ChainError::conflict(format!(
                "a chain already exists at {dir:?} — refusing to overwrite the founder ledger"
            )));
        }
        if !is_address(founder_address) {
            return Err(ChainError::validation(
                "founder_address must look like TOR1…onion",
            ));
        }
        if supply_micro == 0 {
            return Err(ChainError::validation("genesis supply must be greater than zero"));
        }
        ensure_dir(dir)?;
        let now = now_secs();
        let mut accounts: BTreeMap<String, Account> = BTreeMap::new();
        accounts.insert(
            founder_address.to_string(),
            Account {
                address: founder_address.to_string(),
                pubkey_base58: founder_pubkey.to_string(),
                balance: supply_micro,
                tokens: BTreeMap::new(),
                nonce: 0,
                role: "founder".to_string(),
                created_at: now,
            },
        );
        let mut state = State {
            chain_id: CHAIN_ID.to_string(),
            protocol_version: PROTOCOL_VERSION,
            native_coin_id: NATIVE_COIN_ID.to_string(),
            height: 0,
            head_hash: "0".repeat(64),
            genesis_hash: String::new(),
            total_supply: supply_micro,
            founder: founder_address.to_string(),
            fee_receiver: founder_address.to_string(),
            admin_token_sha256: sha3_hex(admin_token.as_bytes()),
            created_at: now,
            seq: 1,
            accounts,
            tokens: BTreeMap::new(),
            pools: BTreeMap::new(),
            records: Vec::new(),
        };

        let draft = RecordDraft {
            kind: "genesis".to_string(),
            from: "genesis".to_string(),
            to: founder_address.to_string(),
            amount: supply_micro,
            token: None,
            fee: 0,
            note: format!("block 0 allocates {} TOR to the founder wallet", format_tor(supply_micro)),
            external_ref: "genesis".to_string(),
            signature: String::new(),
        };
        let mut record = make_record(&draft, 1, 0, now);
        let mut block = Block {
            height: 0,
            prev_hash: "0".repeat(64),
            timestamp: now,
            proposer: founder_address.to_string(),
            state_root: state_root_of(&state),
            txids: vec![record.txid.clone()],
            hash: String::new(),
        };
        let txids = block.txids.clone();
        let digest = block_digest(&block, &txids)?;
        block.hash = digest.clone();
        record.block_hash = digest.clone();
        state.head_hash = digest.clone();
        state.genesis_hash = digest;
        state.records.push(record);

        let chain = Chain { dir: dir.to_path_buf(), state, blocks: vec![block.clone()] };
        chain.append_block(&block, &chain.state.records)?;
        chain.save()?;
        Ok(chain)
    }

    /// Append a block holding every record produced by one transaction batch.
    pub fn commit(&mut self, drafts: Vec<RecordDraft>, proposer: &str) -> ChainResult<Vec<TxRecord>> {
        if drafts.is_empty() {
            return Ok(Vec::new());
        }
        let height = self.state.height + 1;
        let timestamp = now_secs();
        let mut records: Vec<TxRecord> = Vec::new();
        for draft in drafts.iter() {
            self.state.seq += 1;
            let seq = self.state.seq;
            records.push(make_record(draft, seq, height, timestamp));
        }
        let txids: Vec<String> = records.iter().map(|record| record.txid.clone()).collect();
        // Height belongs to the state root, so advance it before hashing the state.
        self.state.height = height;
        let mut block = Block {
            height,
            prev_hash: self.state.head_hash.clone(),
            timestamp,
            proposer: proposer.to_string(),
            state_root: state_root_of(&self.state),
            txids: txids.clone(),
            hash: String::new(),
        };
        let digest = block_digest(&block, &txids)?;
        block.hash = digest.clone();
        for record in records.iter_mut() {
            record.block_hash = digest.clone();
        }
        self.append_block(&block, &records)?;
        self.blocks.push(block);
        self.state.head_hash = digest;
        self.state.records.extend(records.iter().cloned());
        self.save()?;
        Ok(records)
    }

    // ---- account helpers -------------------------------------------------
    fn ensure_account(&mut self, address: &str, pubkey: &str) -> &mut Account {
        let key = address.to_string();
        let entry = self.state.accounts.entry(key).or_insert_with(|| Account {
            address: address.to_string(),
            pubkey_base58: String::new(),
            balance: 0,
            tokens: BTreeMap::new(),
            nonce: 0,
            role: "holder".to_string(),
            created_at: now_secs(),
        });
        if entry.pubkey_base58.is_empty() && !pubkey.is_empty() {
            entry.pubkey_base58 = pubkey.to_string();
        }
        entry
    }

    fn credit(&mut self, address: &str, micro: u64) -> ChainResult<()> {
        if micro == 0 {
            return Ok(());
        }
        let account = self.ensure_account(address, "");
        account.balance = account
            .balance
            .checked_add(micro)
            .ok_or_else(|| ChainError::new("OVERFLOW", "balance overflow"))?;
        Ok(())
    }

    fn debit(&mut self, address: &str, micro: u64) -> ChainResult<()> {
        if micro == 0 {
            return Ok(());
        }
        let account = self.state.accounts.get_mut(address).ok_or_else(|| {
            ChainError::insufficient(format!("account {address} does not exist"))
        })?;
        if account.balance < micro {
            return Err(ChainError::insufficient(format!(
                "{address} holds {} TOR but {} TOR is required",
                format_tor(account.balance),
                format_tor(micro)
            )));
        }
        account.balance -= micro;
        Ok(())
    }

    fn credit_token(&mut self, address: &str, symbol: &str, units: u64) -> ChainResult<()> {
        if units == 0 {
            return Ok(());
        }
        let account = self.ensure_account(address, "");
        let current = *account.tokens.get(symbol).unwrap_or(&0);
        let updated = current
            .checked_add(units)
            .ok_or_else(|| ChainError::new("OVERFLOW", "token balance overflow"))?;
        account.tokens.insert(symbol.to_string(), updated);
        Ok(())
    }

    fn debit_token(&mut self, address: &str, symbol: &str, units: u64) -> ChainResult<()> {
        let account = self.state.accounts.get_mut(address).ok_or_else(|| {
            ChainError::insufficient(format!("account {address} does not exist"))
        })?;
        let current = *account.tokens.get(symbol).unwrap_or(&0);
        if current < units {
            return Err(ChainError::insufficient(format!(
                "{address} holds {current} {symbol} but {units} is required"
            )));
        }
        account.tokens.insert(symbol.to_string(), current - units);
        Ok(())
    }

    fn charge_fee(&mut self, signer: &str, fee: u64) -> ChainResult<()> {
        if fee == 0 {
            return Ok(());
        }
        let receiver = self.state.fee_receiver.clone();
        self.debit(signer, fee)?;
        self.credit(&receiver, fee)
    }

    fn fee_draft(&self, signer: &str, fee: u64, label: &str, signature: &str) -> RecordDraft {
        RecordDraft {
            kind: "fee".to_string(),
            from: signer.to_string(),
            to: self.state.fee_receiver.clone(),
            amount: fee,
            token: None,
            fee: 0,
            note: label.to_string(),
            external_ref: String::new(),
            signature: signature.to_string(),
        }
    }

    // ---- signed transactions --------------------------------------------
    pub fn apply_signed(&mut self, tx: &SignedTx, proposer: &str) -> ChainResult<Vec<TxRecord>> {
        if tx.kind.trim().is_empty() {
            return Err(ChainError::validation("kind is required"));
        }
        if !is_address(&tx.from) {
            return Err(ChainError::validation("from must be a TOR1…onion address"));
        }
        let raw_pub = bs58::decode(tx.pubkey_base58.trim())
            .into_vec()
            .map_err(|e| ChainError::validation(format!("pubkey_base58: {e}")))?;
        if raw_pub.len() != 32 {
            return Err(ChainError::validation("pubkey_base58 must decode to 32 bytes"));
        }
        if address_from_pubkey(&raw_pub) != tx.from {
            return Err(ChainError::new(
                "ADDRESS_MISMATCH",
                "from is not derived from pubkey_base58",
            ));
        }
        verify_signature(&tx.pubkey_base58, &tx.message(), &tx.signature)?;
        let drift = (now_secs() - tx.timestamp).abs();
        if drift > MAX_CLOCK_DRIFT_SECS {
            return Err(ChainError::validation(format!(
                "timestamp differs from node time by {drift}s (max {MAX_CLOCK_DRIFT_SECS}s)"
            )));
        }
        let snapshot = self.state.clone();
        match self.apply_inner(tx, proposer) {
            Ok(records) => Ok(records),
            Err(error) => {
                self.state = snapshot;
                Err(error)
            }
        }
    }

    fn apply_inner(&mut self, tx: &SignedTx, proposer: &str) -> ChainResult<Vec<TxRecord>> {
        let signer = tx.from.clone();
        let expected_nonce = self
            .state
            .accounts
            .get(&signer)
            .map(|account| account.nonce)
            .unwrap_or(0);
        if expected_nonce != tx.nonce {
            return Err(ChainError::new(
                "NONCE_MISMATCH",
                format!("expected nonce {expected_nonce}, got {}", tx.nonce),
            ));
        }
        let kind = tx.kind.trim().to_lowercase();
        let mut drafts: Vec<RecordDraft> = Vec::new();
        match kind.as_str() {
            "transfer" => self.tx_transfer(tx, &mut drafts)?,
            "nft_transfer" => self.tx_nft_transfer(tx, &mut drafts)?,
            "deposit" => self.tx_deposit(tx, &mut drafts)?,
            "withdraw" => self.tx_withdraw(tx, &mut drafts)?,
            "token_create" => self.tx_token_create(tx, &mut drafts)?,
            "pool_create" => self.tx_pool_create(tx, &mut drafts)?,
            "swap" => self.tx_swap(tx, &mut drafts)?,
            "mint" => self.tx_mint(tx, &mut drafts)?,
            other => {
                return Err(ChainError::validation(format!(
                    "unsupported tx kind {other:?} (expected transfer, nft_transfer, deposit, withdraw, token_create, pool_create, swap, mint)"
                )))
            }
        }
        if let Some(account) = self.state.accounts.get_mut(&signer) {
            account.nonce += 1;
        }
        self.commit(drafts, proposer)
    }

    fn tx_transfer(&mut self, tx: &SignedTx, drafts: &mut Vec<RecordDraft>) -> ChainResult<()> {
        if tx.amount == 0 {
            return Err(ChainError::validation("amount must be greater than zero"));
        }
        let to = tx.to.trim().to_string();
        if !is_address(&to) {
            return Err(ChainError::validation("to must be a TOR1…onion address"));
        }
        if to == NATIVE_COIN_ID {
            return Err(ChainError::validation(
                "the native coin id is not a transfer destination",
            ));
        }
        // base layer: no transaction fee. The node/platform fee (0.1 TOR) is a
        // separate, signed transfer that the Python API layer settles.
        let fee = BASE_TX_FEE_MICRO;
        let signature = tx.signature.clone();
        match tx.token.clone().map(|symbol| symbol.trim().to_uppercase()) {
            Some(symbol) if !symbol.is_empty() => {
                if !self.state.tokens.contains_key(&symbol) {
                    return Err(ChainError::new(
                        "UNKNOWN_TOKEN",
                        format!("token {symbol} does not exist"),
                    ));
                }
                self.debit_token(&tx.from, &symbol, tx.amount)?;
                self.credit_token(&to, &symbol, tx.amount)?;
                self.charge_fee(&tx.from, fee)?;
                drafts.push(RecordDraft {
                    kind: "transfer".to_string(),
                    from: tx.from.clone(),
                    to: to.clone(),
                    amount: tx.amount,
                    token: Some(symbol),
                    fee,
                    note: tx.note.clone(),
                    external_ref: tx.external_ref.clone(),
                    signature: signature.clone(),
                });
            }
            _ => {
                self.debit(&tx.from, tx.amount)?;
                self.credit(&to, tx.amount)?;
                self.charge_fee(&tx.from, fee)?;
                drafts.push(RecordDraft {
                    kind: "transfer".to_string(),
                    from: tx.from.clone(),
                    to: to.clone(),
                    amount: tx.amount,
                    token: None,
                    fee,
                    note: tx.note.clone(),
                    external_ref: tx.external_ref.clone(),
                    signature: signature.clone(),
                });
            }
        }
        if fee > 0 {
            drafts.push(self.fee_draft(&tx.from, fee, "base-layer transfer fee", &signature));
        }
        Ok(())
    }

    /// Move a unique edition — an NFT — from its current holder to another wallet.
    ///
    /// This is the primitive §19.2 said the chain did not have. It is **not**
    /// `transfer --token`: moving N units of a symbol says nothing about
    /// uniqueness and never updates who owns the edition. Here the asset is
    /// identified by its symbol, the supply must be exactly one, and the only
    /// signature the chain accepts is the current holder's.
    ///
    /// The ten rules, in the order they fail:
    ///
    /// 1. `token` is required (the NFT's identifier);
    /// 2. `amount` must be 1 — zero and two are refused, never silently coerced;
    /// 3. `to` must be a real `TOR1…onion` address, and not the native coin id;
    /// 4. a transfer to the current holder is refused (`CONFLICT`) instead of
    ///    writing a meaningless record;
    /// 5. a symbol that does not exist answers `UNKNOWN_TOKEN` (404);
    /// 6. a token whose supply is not 1 refuses `nft_transfer` and points at
    ///    `transfer`: an NFT on this chain *is* a supply-1, 0-decimal token;
    /// 7. the edition must have a holder at all (a burned or half-written
    ///    supply is `CONFLICT`, not a silent success);
    /// 8. the signer must **be** that holder (`UNAUTHORIZED`) — ownership is
    ///    derived from the balances the state root commits to, never from a
    ///    field a client could set;
    /// 9. the debit and the credit happen in one `apply_signed` call on a
    ///    cloned state, so a failure anywhere leaves the ledger untouched;
    /// 10. the record is permanent (`nft_transfer`, in a block, with the
    ///     signer's signature and the sender's privacy ref), and the token
    ///     table's `owner` becomes the new holder — the creator is still
    ///     readable from the `token_create` record (`nft_json` returns both).
    ///
    /// Replay is the nonce: `apply_signed` checks it before this runs, so the
    /// same signed bytes posted twice answers `NONCE_MISMATCH` (409), and
    /// re-signing as the *old* owner after the transfer answers `UNAUTHORIZED`.
    fn tx_nft_transfer(&mut self, tx: &SignedTx, drafts: &mut Vec<RecordDraft>) -> ChainResult<()> {
        let symbol = tx.token.clone().unwrap_or_default().trim().to_uppercase();
        if symbol.is_empty() {
            return Err(ChainError::validation(
                "token is required: a unique edition is identified by its symbol",
            ));
        }
        if tx.amount != 1 {
            return Err(ChainError::validation(format!(
                "amount must be 1: an edition is a single unit, got {}",
                tx.amount
            )));
        }
        let to = tx.to.trim().to_string();
        if !is_address(&to) {
            return Err(ChainError::validation("to must be a TOR1…onion address"));
        }
        if to == NATIVE_COIN_ID {
            return Err(ChainError::validation(
                "the native coin id is not a transfer destination",
            ));
        }
        if to == tx.from {
            return Err(ChainError::conflict(format!(
                "{to} already owns {symbol} — a transfer to the holder is not a transfer"
            )));
        }
        self.unique_edition(&symbol)?;
        let owner = self.nft_owner(&symbol).ok_or_else(|| {
            ChainError::conflict(format!(
                "NFT {symbol} has no holder on this chain: the token table carries it but no \
                 account holds its supply"
            ))
        })?;
        if owner != tx.from {
            return Err(ChainError::unauthorized(format!(
                "only the current owner of {symbol} may transfer it (the owner is {owner})"
            )));
        }
        let fee = BASE_TX_FEE_MICRO;
        self.debit_token(&tx.from, &symbol, 1)?;
        self.credit_token(&to, &symbol, 1)?;
        self.charge_fee(&tx.from, fee)?;
        // The token table's `owner` is the holder from now on. The creator stays
        // readable from the `token_create` record, so identity is not lost by
        // following ownership.
        if let Some(token) = self.state.tokens.get_mut(&symbol) {
            token.owner = to.clone();
        }
        let note = if tx.note.trim().is_empty() {
            format!("transferred unique edition {symbol}")
        } else {
            tx.note.trim().to_string()
        };
        drafts.push(RecordDraft {
            kind: "nft_transfer".to_string(),
            from: tx.from.clone(),
            to: to.clone(),
            amount: 1,
            token: Some(symbol.clone()),
            fee,
            note,
            external_ref: tx.external_ref.clone(),
            signature: tx.signature.clone(),
        });
        if fee > 0 {
            drafts.push(self.fee_draft(&tx.from, fee, "base-layer NFT transfer fee", &tx.signature));
        }
        Ok(())
    }

    fn tx_deposit(&mut self, tx: &SignedTx, drafts: &mut Vec<RecordDraft>) -> ChainResult<()> {
        if tx.from != self.state.founder {
            return Err(ChainError::unauthorized(
                "deposits must be signed by the founder wallet",
            ));
        }
        if !is_address(&tx.to) {
            return Err(ChainError::validation("to must be a TOR1…onion address"));
        }
        if tx.amount == 0 {
            return Err(ChainError::validation("amount must be greater than zero"));
        }
        self.credit(&tx.to, tx.amount)?;
        self.state.total_supply = self
            .state
            .total_supply
            .checked_add(tx.amount)
            .ok_or_else(|| ChainError::new("OVERFLOW", "supply overflow"))?;
        let note = if tx.note.trim().is_empty() {
            "external deposit".to_string()
        } else {
            tx.note.trim().to_string()
        };
        drafts.push(RecordDraft {
            kind: "deposit".to_string(),
            from: "external".to_string(),
            to: tx.to.clone(),
            amount: tx.amount,
            token: None,
            fee: 0,
            note,
            external_ref: tx.external_ref.clone(),
            signature: tx.signature.clone(),
        });
        Ok(())
    }

    fn tx_withdraw(&mut self, tx: &SignedTx, drafts: &mut Vec<RecordDraft>) -> ChainResult<()> {
        if tx.from != self.state.founder {
            return Err(ChainError::unauthorized(
                "withdrawals must be signed by the founder wallet",
            ));
        }
        if !is_address(&tx.to) {
            return Err(ChainError::validation("to must be a TOR1…onion address"));
        }
        if tx.amount == 0 {
            return Err(ChainError::validation("amount must be greater than zero"));
        }
        self.debit(&tx.to, tx.amount)?;
        self.state.total_supply = self.state.total_supply.saturating_sub(tx.amount);
        let note = if tx.note.trim().is_empty() {
            "external withdrawal".to_string()
        } else {
            tx.note.trim().to_string()
        };
        drafts.push(RecordDraft {
            kind: "withdraw".to_string(),
            from: tx.to.clone(),
            to: "external".to_string(),
            amount: tx.amount,
            token: None,
            fee: 0,
            note,
            external_ref: tx.external_ref.clone(),
            signature: tx.signature.clone(),
        });
        Ok(())
    }

    fn tx_token_create(&mut self, tx: &SignedTx, drafts: &mut Vec<RecordDraft>) -> ChainResult<()> {
        let symbol = tx.token.clone().unwrap_or_default().trim().to_uppercase();
        if symbol.len() < 2
            || symbol.len() > 10
            || !symbol.chars().all(|c| c.is_ascii_alphanumeric())
        {
            return Err(ChainError::validation(
                "token must be 2-10 ASCII letters/digits — it is the ticker",
            ));
        }
        if tx.amount == 0 {
            return Err(ChainError::validation(
                "amount must be the whole-token supply",
            ));
        }
        if self.state.tokens.contains_key(&symbol) {
            return Err(ChainError::conflict(format!("token {symbol} already exists")));
        }
        let fee = pricing::price_micro("meme_token_create")?;
        self.charge_fee(&tx.from, fee)?;
        let name = if tx.note.trim().is_empty() {
            symbol.clone()
        } else {
            tx.note.trim().to_string()
        };
        self.state.tokens.insert(
            symbol.clone(),
            Token {
                symbol: symbol.clone(),
                name: name.clone(),
                supply: tx.amount,
                decimals: 0,
                owner: tx.from.clone(),
                created_at: now_secs(),
            },
        );
        self.credit_token(&tx.from, &symbol, tx.amount)?;
        drafts.push(RecordDraft {
            kind: "token_create".to_string(),
            from: tx.from.clone(),
            to: tx.from.clone(),
            amount: tx.amount,
            token: Some(symbol.clone()),
            fee,
            note: name,
            external_ref: tx.external_ref.clone(),
            signature: tx.signature.clone(),
        });
        if fee > 0 {
            drafts.push(self.fee_draft(&tx.from, fee, "token creation fee (5 TOR)", &tx.signature));
        }
        Ok(())
    }

    fn tx_pool_create(&mut self, tx: &SignedTx, drafts: &mut Vec<RecordDraft>) -> ChainResult<()> {
        let symbol = tx.token.clone().unwrap_or_default().trim().to_uppercase();
        if !self.state.tokens.contains_key(&symbol) {
            return Err(ChainError::new(
                "UNKNOWN_TOKEN",
                format!("token {symbol} does not exist — create it first"),
            ));
        }
        if self.state.pools.contains_key(&symbol) {
            return Err(ChainError::conflict(format!(
                "a liquidity pool for {symbol} already exists"
            )));
        }
        if tx.amount == 0 {
            return Err(ChainError::validation(
                "amount must be the token reserve (whole tokens)",
            ));
        }
        let tor_reserve = parse_tor(&tx.note)?;
        let fee = pricing::price_micro("meme_pool_create")?;
        self.debit_token(&tx.from, &symbol, tx.amount)?;
        self.debit(&tx.from, tor_reserve)?;
        self.charge_fee(&tx.from, fee)?;
        let pool_account = pool_address(&symbol);
        self.credit(&pool_account, tor_reserve)?;
        self.credit_token(&pool_account, &symbol, tx.amount)?;
        self.state.pools.insert(
            symbol.clone(),
            Pool {
                token: symbol.clone(),
                tor_reserve,
                token_reserve: tx.amount,
                fee_bps: SWAP_FEE_BPS,
                created_at: now_secs(),
            },
        );
        drafts.push(RecordDraft {
            kind: "pool_create".to_string(),
            from: tx.from.clone(),
            to: "pool".to_string(),
            amount: tx.amount,
            token: Some(symbol.clone()),
            fee,
            note: format!("{} TOR seeded against {} {symbol}", format_tor(tor_reserve), tx.amount),
            external_ref: tx.external_ref.clone(),
            signature: tx.signature.clone(),
        });
        if fee > 0 {
            drafts.push(self.fee_draft(&tx.from, fee, "pool launch fee (10 TOR)", &tx.signature));
        }
        Ok(())
    }

    fn tx_swap(&mut self, tx: &SignedTx, drafts: &mut Vec<RecordDraft>) -> ChainResult<()> {
        let symbol = tx.token.clone().unwrap_or_default().trim().to_uppercase();
        let pool = self.state.pools.get(&symbol).cloned().ok_or_else(|| {
            ChainError::new("POOL_NOT_FOUND", format!("no liquidity pool for {symbol}"))
        })?;
        if tx.amount == 0 {
            return Err(ChainError::validation("amount must be greater than zero"));
        }
        let direction = tx.note.trim().to_lowercase();
        let (out, fee, note) = match direction.as_str() {
            "buy" | "" => {
                let fee = tx.amount * SWAP_FEE_BPS / 10_000;
                let net = tx.amount - fee;
                let numerator = (pool.token_reserve as u128) * (net as u128);
                let denominator = (pool.tor_reserve as u128) + (net as u128);
                let out = (numerator / denominator) as u64;
                if out == 0 || out >= pool.token_reserve {
                    return Err(ChainError::validation("swap is too small to fill"));
                }
                self.debit(&tx.from, tx.amount)?;
                let receiver = self.state.fee_receiver.clone();
                self.credit(&receiver, fee)?;
                let pool_account = pool_address(&symbol);
                self.credit(&pool_account, net)?;
                self.debit_token(&pool_account, &symbol, out)?;
                self.credit_token(&tx.from, &symbol, out)?;
                if let Some(entry) = self.state.pools.get_mut(&symbol) {
                    entry.tor_reserve = entry
                        .tor_reserve
                        .checked_add(net)
                        .ok_or_else(|| ChainError::new("OVERFLOW", "pool reserve overflow"))?;
                    entry.token_reserve -= out;
                }
                (out, fee, format!("buy {symbol} for {} TOR", format_tor(tx.amount)))
            }
            "sell" => {
                if tx.amount > pool.token_reserve {
                    return Err(ChainError::insufficient(format!(
                        "pool only holds {} {symbol}",
                        pool.token_reserve
                    )));
                }
                let numerator = (pool.tor_reserve as u128) * (tx.amount as u128);
                let denominator = (pool.token_reserve as u128) + (tx.amount as u128);
                let gross = (numerator / denominator) as u64;
                let fee = gross * SWAP_FEE_BPS / 10_000;
                let out = gross - fee;
                if out == 0 {
                    return Err(ChainError::validation("swap is too small to fill"));
                }
                self.debit_token(&tx.from, &symbol, tx.amount)?;
                let pool_account = pool_address(&symbol);
                self.credit_token(&pool_account, &symbol, tx.amount)?;
                self.debit(&pool_account, gross)?;
                self.credit(&tx.from, out)?;
                let receiver = self.state.fee_receiver.clone();
                self.credit(&receiver, fee)?;
                if let Some(entry) = self.state.pools.get_mut(&symbol) {
                    entry.token_reserve = entry
                        .token_reserve
                        .checked_add(tx.amount)
                        .ok_or_else(|| ChainError::new("OVERFLOW", "pool reserve overflow"))?;
                    entry.tor_reserve = entry.tor_reserve.saturating_sub(gross);
                }
                (out, fee, format!("sell {} {symbol} for TOR", tx.amount))
            }
            other => {
                return Err(ChainError::validation(format!(
                    "swap note must be 'buy' or 'sell', got {other:?}"
                )))
            }
        };
        drafts.push(RecordDraft {
            kind: "swap".to_string(),
            from: tx.from.clone(),
            to: "pool".to_string(),
            amount: tx.amount,
            token: Some(symbol.clone()),
            fee,
            note: format!("{note} → received {out}"),
            external_ref: tx.external_ref.clone(),
            signature: tx.signature.clone(),
        });
        Ok(())
    }

    fn tx_mint(&mut self, tx: &SignedTx, drafts: &mut Vec<RecordDraft>) -> ChainResult<()> {
        if tx.from != self.state.founder && tx.from != self.state.fee_receiver {
            return Err(ChainError::unauthorized(
                "only the founder / authorized minter may mint",
            ));
        }
        let symbol = tx.token.clone().unwrap_or_default().trim().to_uppercase();
        if tx.amount == 0 {
            return Err(ChainError::validation("amount must be greater than zero"));
        }
        {
            let token = self.state.tokens.get_mut(&symbol).ok_or_else(|| {
                ChainError::new("UNKNOWN_TOKEN", format!("token {symbol} does not exist"))
            })?;
            token.supply = token
                .supply
                .checked_add(tx.amount)
                .ok_or_else(|| ChainError::new("OVERFLOW", "token supply overflow"))?;
        }
        self.credit_token(&tx.from, &symbol, tx.amount)?;
        let fee = pricing::price_micro("mint")?;
        self.charge_fee(&tx.from, fee)?;
        drafts.push(RecordDraft {
            kind: "mint".to_string(),
            from: tx.from.clone(),
            to: tx.from.clone(),
            amount: tx.amount,
            token: Some(symbol.clone()),
            fee,
            note: format!("minted {} {symbol}", tx.amount),
            external_ref: tx.external_ref.clone(),
            signature: tx.signature.clone(),
        });
        if fee > 0 {
            drafts.push(self.fee_draft(&tx.from, fee, "mint fee (3 TOR)", &tx.signature));
        }
        Ok(())
    }

    // ---- read models ---------------------------------------------------
    pub fn info(&self) -> serde_json::Value {
        serde_json::json!({
            "chain_id": self.state.chain_id,
            "protocol_version": self.state.protocol_version,
            "native_coin_id": self.state.native_coin_id,
            "coin": crate::coin::as_json(),
            "height": self.state.height,
            "head_hash": self.state.head_hash,
            "genesis_hash": self.state.genesis_hash,
            "total_supply": tor_value(self.state.total_supply),
            "total_supply_micro": self.state.total_supply,
            "micro_per_tor": MICRO_PER_TOR,
            "founder": self.state.founder,
            "fee_receiver": self.state.fee_receiver,
            "accounts": self.state.accounts.len(),
            "tokens": self.state.tokens.len(),
            // unique editions (supply 1, 0 decimals) — the count the NFT panes
            // care about, answered here so a health read never needs a second call
            "nfts": self
                .state
                .tokens
                .values()
                .filter(|token| token.supply == 1 && token.decimals == 0)
                .count(),
            "pools": self.state.pools.len(),
            "records": self.state.records.len(),
            "peer_count": 0,
            "consensus": "Proof-of-Authority (single authorized proposer until peers join)",
        })
    }

    pub fn account_json(&self, address: &str) -> ChainResult<serde_json::Value> {
        let account = self
            .state
            .accounts
            .get(address)
            .ok_or_else(|| ChainError::not_found(format!("account {address} not found")))?;
        Ok(serde_json::json!({
            "address": account.address.clone(),
            "pubkey_base58": account.pubkey_base58.clone(),
            "balance": tor_value(account.balance),
            "balance_micro": account.balance,
            "tokens": account.tokens.clone(),
            "nonce": account.nonce,
            "role": account.role.clone(),
            "created_at": account.created_at,
        }))
    }

    pub fn balance_of(&self, address: &str) -> u64 {
        self.state
            .accounts
            .get(address)
            .map(|account| account.balance)
            .unwrap_or(0)
    }

    /// Where a unique-edition token keeps its metadata document.
    ///
    /// `token_create` always mints `decimals: 0`, so a token whose whole supply
    /// is 1 behaves as a unique asset (an NFT in this chain's terms). The
    /// `token` field of the creation record names the symbol; its `note`
    /// carries the display name (see `tx_token_create`) and its `external_ref`
    /// is the metadata URI the creator chose to publish (`ipfs://…` or an
    /// `https://` document). This resolves that URI, if any.
    fn token_metadata_uri(&self, symbol: &str) -> String {
        self.state
            .records
            .iter()
            .rev()
            .find(|record| {
                record.kind == "token_create"
                    && record.token.as_deref() == Some(symbol)
                    && !record.external_ref.trim().is_empty()
            })
            .map(|record| record.external_ref.trim().to_string())
            .unwrap_or_default()
    }

    /// Who minted an edition: the signer of the `token_create` that made it.
    ///
    /// The token table's `owner` field follows *ownership*, so after the first
    /// `nft_transfer` it names the holder, not the creator. Both facts are asked
    /// for by a marketplace and by an explorer, so both are answered: the creator
    /// comes from the permanent creation record.
    fn token_creator(&self, symbol: &str) -> String {
        self.state
            .records
            .iter()
            .find(|record| {
                record.kind == "token_create" && record.token.as_deref() == Some(symbol)
            })
            .map(|record| record.from.clone())
            .unwrap_or_else(|| {
                self.state
                    .tokens
                    .get(symbol)
                    .map(|token| token.owner.clone())
                    .unwrap_or_default()
            })
    }

    /// Who holds an edition **now** — derived from the balances, never asserted.
    ///
    /// The owner of a unique edition is the account holding its whole supply. That
    /// is the same number the state root hashes, so `GET /api/nft/<symbol>` cannot
    /// disagree with the ledger it claims to describe. `None` means "no holder"
    /// (a supply that exists in the token table but in nobody's balance), which
    /// `tx_nft_transfer` refuses rather than pretending to move it.
    pub fn nft_owner(&self, symbol: &str) -> Option<String> {
        let supply = self.state.tokens.get(symbol)?.supply;
        if supply == 0 {
            return None;
        }
        self.state
            .accounts
            .iter()
            .find(|(_, account)| account.tokens.get(symbol).copied().unwrap_or(0) >= supply)
            .map(|(address, _)| address.clone())
    }

    /// Everything holding such an address, newest first — the integrity read
    /// behind "supply 1 must mean exactly one holder, holding exactly 1".
    fn edition_holders(&self, symbol: &str) -> Vec<String> {
        self.state
            .accounts
            .iter()
            .filter(|(_, account)| account.tokens.get(symbol).copied().unwrap_or(0) > 0)
            .map(|(address, _)| address.clone())
            .collect()
    }

    /// Resolve a symbol to a **unique edition**, or say precisely why it is not one.
    fn unique_edition(&self, symbol: &str) -> ChainResult<&Token> {
        let token = self.state.tokens.get(symbol).ok_or_else(|| {
            ChainError::new(
                "UNKNOWN_TOKEN",
                format!("NFT {symbol} does not exist on this chain"),
            )
        })?;
        if token.supply != 1 || token.decimals != 0 {
            return Err(ChainError::validation(format!(
                "{symbol} is not a unique edition (supply {}, decimals {}): an NFT on this \
                 chain is a token whose whole supply is 1 with 0 decimals (create it with \
                 token_create --amount 1); move fungible tokens with `transfer`",
                token.supply, token.decimals
            )));
        }
        Ok(token)
    }


    /// One address's holdings, joined with the token table.
    ///
    /// A wallet pop-up needs three things `account_json` does not answer: the
    /// token's *name*, *symbol* and *decimals* (the account only stores a
    /// symbol → raw amount map), whether an asset is a unique edition, and that
    /// edition's metadata URI. This returns all of it in one read, TOR first,
    /// with `tokens` and `nfts` split so a UI renders two panes from a single
    /// response without guessing.
    ///
    /// Balances here are **public data**, exactly like `/api/account/{address}`:
    /// a stranger may ask what an address holds. Nothing about *moving* value is
    /// revealed.
    pub fn account_holdings(&self, address: &str) -> ChainResult<serde_json::Value> {
        let address = address.trim();
        if !is_address(address) {
            return Err(ChainError::validation(
                "address must be a TOR1…onion address",
            ));
        }
        // A wallet that has just been generated has no account row yet. That is
        // not an error: the pop-up must open and show 0 TOR, 0 tokens, 0
        // editions, or the very first thing a new user sees is a 404.
        let exists = self.state.accounts.contains_key(address);
        let empty: BTreeMap<String, u64> = BTreeMap::new();
        let account = self.state.accounts.get(address);
        let held_tokens = account.map(|row| &row.tokens).unwrap_or(&empty);
        let mut held: Vec<serde_json::Value> = Vec::new();
        for (symbol, amount) in held_tokens.iter() {
            if *amount == 0 {
                continue;
            }
            let meta = self.state.tokens.get(symbol);
            let supply = meta.map(|token| token.supply).unwrap_or(0);
            let decimals = meta.map(|token| token.decimals).unwrap_or(0);
            let unique = supply == 1;
            held.push(json!({
                "symbol": symbol,
                "name": meta.map(|token| token.name.clone()).unwrap_or_else(|| symbol.clone()),
                "balance_raw": amount,
                "balance": if decimals == 0 { json!(amount) } else { json!(tor_value(*amount)) },
                "decimals": decimals,
                "supply": supply,
                "owner": meta.map(|token| token.owner.clone()).unwrap_or_default(),
                "created_at": meta.map(|token| token.created_at).unwrap_or(0),
                "standard": if unique { "oneness-tor-nft/1" } else { "oneness-tor-token/1" },
                "nft": unique,
                "metadata_uri": if unique { self.token_metadata_uri(symbol) } else { String::new() },
            }));
        }
        let nfts: Vec<serde_json::Value> = held
            .iter()
            .filter(|row| row["nft"] == json!(true))
            .cloned()
            .collect();
        let fungible: Vec<serde_json::Value> = held
            .iter()
            .filter(|row| row["nft"] != json!(true))
            .cloned()
            .collect();
        Ok(json!({
            "address": address,
            "exists": exists,
            "height": self.state.height,
            "tor": {
                "symbol": "TOR",
                "name": "OneNess TOR",
                "balance_micro": account.map(|row| row.balance).unwrap_or(0),
                "balance": tor_value(account.map(|row| row.balance).unwrap_or(0)),
                "decimals": TOR_DECIMALS,
                "native_coin_id": self.state.native_coin_id.clone(),
            },
            "tokens": fungible,
            "nfts": nfts,
            "token_count": fungible.len(),
            "nft_count": nfts.len(),
            "nonce": account.map(|row| row.nonce).unwrap_or(0),
            "role": account.map(|row| row.role.clone()).unwrap_or_else(|| "unfunded".to_string()),
            "note": if exists {
                "tokens are joined with the chain token table; an asset with supply 1 is a unique edition (token_create always mints 0 decimals)"
            } else {
                "this address has no account on the chain yet: every balance is zero until something is sent to it"
            },
        }))
    }

    /// One address's whole transaction history — the wallet's third pane.
    ///
    /// Every record where this address is the sender or the recipient, newest
    /// first, each carrying the two facts a wallet must show and
    /// `/api/public/records` does not give per address: **direction**
    /// (`in`/`out`/`self`, computed here from the address rather than guessed by
    /// the client) and **confirmations** (blocks on top of the record, so `>= 1`
    /// means settled — this node confirms synchronously, so a record only exists
    /// once it is inside a block).
    ///
    /// Privacy is the sender's choice, per transaction (`external_ref` on the
    /// signed tx). A record whose `visibility` is not `public` is listed only
    /// when the caller presents the chain admin token — the same rule
    /// `GET /api/chain/tx/{txid}` already applies, so the history pane can never
    /// become a side channel that leaks what the single-record route withholds.
    ///
    /// `totals` describe **every** matching record, never just the returned page.
    pub fn account_history(
        &self,
        address: &str,
        limit: usize,
        offset: usize,
        include_private: bool,
    ) -> ChainResult<serde_json::Value> {
        let address = address.trim();
        if !is_address(address) {
            return Err(ChainError::validation(
                "address must be a TOR1…onion address",
            ));
        }
        let limit = limit.clamp(1, 500);
        let mut rows: Vec<serde_json::Value> = Vec::new();
        let mut tor_in: u64 = 0;
        let mut tor_out: u64 = 0;
        let mut token_totals: BTreeMap<String, (u64, u64)> = BTreeMap::new();
        let mut withheld: usize = 0;
        for record in self.state.records.iter().rev() {
            let outgoing = record.from == address;
            let incoming = record.to == address;
            if !outgoing && !incoming {
                continue;
            }
            if record.visibility != "public" && !include_private {
                withheld += 1;
                continue;
            }
            let direction = if outgoing && incoming {
                "self"
            } else if outgoing {
                "out"
            } else {
                "in"
            };
            let counterparty = if outgoing && !incoming {
                record.to.clone()
            } else if incoming && !outgoing {
                record.from.clone()
            } else {
                address.to_string()
            };
            let decimals = record
                .token
                .as_deref()
                .and_then(|symbol| self.state.tokens.get(symbol))
                .map(|token| token.decimals)
                .unwrap_or(0);
            let amount = match record.token.as_deref() {
                None => json!(tor_value(record.amount)),
                Some(_) if decimals == 0 => json!(record.amount),
                Some(_) => json!(tor_value(record.amount)),
            };
            match record.token.as_deref() {
                Some(symbol) => {
                    let slot = token_totals.entry(symbol.to_string()).or_insert((0, 0));
                    if direction != "in" {
                        slot.1 = slot.1.saturating_add(record.amount);
                    }
                    if direction != "out" {
                        slot.0 = slot.0.saturating_add(record.amount);
                    }
                }
                None => {
                    if direction != "in" {
                        tor_out = tor_out.saturating_add(record.amount);
                    }
                    if direction != "out" {
                        tor_in = tor_in.saturating_add(record.amount);
                    }
                }
            }
            let confirmations = self
                .state
                .height
                .saturating_sub(record.height)
                .saturating_add(1);
            rows.push(json!({
                "txid": record.txid.clone(),
                "kind": record.kind.clone(),
                "direction": direction,
                "from": record.from.clone(),
                "to": record.to.clone(),
                "counterparty": counterparty,
                "token": match record.token.as_deref() {
                    Some(symbol) => json!(symbol),
                    None => serde_json::Value::Null,
                },
                "symbol": record.token.clone().unwrap_or_else(|| "TOR".to_string()),
                "decimals": if record.token.is_some() { json!(decimals) } else { json!(TOR_DECIMALS) },
                "amount_raw": record.amount,
                "amount": amount,
                "fee": record.fee,
                "note": record.note.clone(),
                "height": record.height,
                "block_hash": record.block_hash.clone(),
                "timestamp": record.timestamp,
                "seq": record.seq,
                "visibility": record.visibility.clone(),
                "private": record.visibility != "public",
                // The privacy mode is the sender's per-transaction choice and it
                // rides in `external_ref` (`oneness-privacy:1|mode=anonymous`).
                // The masking rule lives in the Python layer
                // (`oneness_tor/services/privacy.py`), so the field travels with
                // the row and that one rule can be applied to it; the node does
                // not re-implement it. Already public: `/api/public/records`
                // returns the same field.
                "external_ref": record.external_ref.clone(),
                "confirmations": confirmations,
                "confirmed": confirmations >= 1,
                "status": if confirmations >= 1 { "confirmed" } else { "pending" },
                "detail": format!("/api/chain/tx/{}", record.txid),
            }));
        }
        let matched = rows.len();
        let page: Vec<serde_json::Value> = rows.into_iter().skip(offset).take(limit).collect();
        let tokens: Vec<serde_json::Value> = token_totals
            .iter()
            .map(|(symbol, (received, sent))| {
                json!({
                    "symbol": symbol,
                    "in_raw": received,
                    "out_raw": sent,
                    "decimals": self.state.tokens.get(symbol).map(|token| token.decimals).unwrap_or(0),
                })
            })
            .collect();
        Ok(json!({
            "address": address,
            "height": self.state.height,
            "count": matched,
            "limit": limit,
            "offset": offset,
            "private_included": include_private,
            "withheld_private": withheld,
            "totals": {
                "tor_in_micro": tor_in,
                "tor_out_micro": tor_out,
                "tor_in": tor_value(tor_in),
                "tor_out": tor_value(tor_out),
                "tokens": tokens,
            },
            "records": page,
        }))
    }

    // ---- NFT read models ------------------------------------------------
    //
    // Six questions a wallet, a marketplace and an explorer ask about a unique
    // edition, answered from the ledger and nothing else: what is it
    // (`nft_json`), who holds it (`account_nfts`), what exists (`nft_catalogue`),
    // what moved (`nft_history`), and can this receipt be checked
    // (`nft_receipt`). The sixth — *submit a transfer* — is `apply_signed`, the
    // same door every other kind uses.

    /// One unique edition: identity, metadata, **who holds it now**, who minted
    /// it, and its whole transfer history in summary.
    pub fn nft_json(&self, symbol: &str) -> ChainResult<serde_json::Value> {
        let symbol = symbol.trim().to_uppercase();
        let token = self.unique_edition(&symbol)?;
        let owner = self.nft_owner(&symbol).unwrap_or_default();
        let holders = self.edition_holders(&symbol);
        let transfers: Vec<&TxRecord> = self
            .state
            .records
            .iter()
            .filter(|record| {
                record.kind == "nft_transfer" && record.token.as_deref() == Some(symbol.as_str())
            })
            .collect();
        let last = transfers.last();
        let mint = self
            .state
            .records
            .iter()
            .find(|record| {
                record.kind == "token_create" && record.token.as_deref() == Some(symbol.as_str())
            });
        Ok(json!({
            "standard": "oneness-tor-nft/1",
            "symbol": symbol,
            "name": token.name.clone(),
            "decimals": token.decimals,
            "supply": token.supply,
            // the holder of record, derived from the balances the state root commits to
            "owner": owner,
            "creator": self.token_creator(&symbol),
            "holders": holders,
            // supply 1 must mean exactly one holder holding exactly 1 — an auditable
            // invariant rather than a promise
            "verified": holders.len() == 1,
            "metadata_uri": self.token_metadata_uri(&symbol),
            "created_at": token.created_at,
            "mint": mint.map(|record| json!({
                "txid": record.txid.clone(),
                "from": record.from.clone(),
                "height": record.height,
                "timestamp": record.timestamp,
                "metadata_uri": record.external_ref.clone(),
            })).unwrap_or(serde_json::Value::Null),
            "transfer_count": transfers.len(),
            "last_transfer": last.map(|record| json!({
                "txid": record.txid.clone(),
                "from": record.from.clone(),
                "to": record.to.clone(),
                "height": record.height,
                "timestamp": record.timestamp,
                "note": record.note.clone(),
                "external_ref": record.external_ref.clone(),
            })).unwrap_or(serde_json::Value::Null),
            "fee": {
                "transfer_micro": BASE_TX_FEE_MICRO,
                "transfer_tor": 0.0,
                "note": "the base layer charges nothing to move an edition; a node's \
                         platform fee is a separate signed transfer",
            },
            "chain": {
                "chain_id": self.state.chain_id.clone(),
                "height": self.state.height,
                "head_hash": self.state.head_hash.clone(),
                "state_root": self.state_root(),
            },
            "detail": format!("/api/nft/{symbol}"),
            "history": format!("/api/nft/history?symbol={symbol}"),
        }))
    }

    /// Every unique edition on the chain, with its current holder.
    pub fn nft_catalogue(&self, limit: usize, offset: usize) -> serde_json::Value {
        let limit = limit.clamp(1, 500);
        let mut rows: Vec<serde_json::Value> = self
            .state
            .tokens
            .keys()
            .filter(|symbol| {
                self.state
                    .tokens
                    .get(*symbol)
                    .map(|token| token.supply == 1 && token.decimals == 0)
                    .unwrap_or(false)
            })
            .map(|symbol| {
                let owner = self.nft_owner(symbol).unwrap_or_default();
                let transfers = self
                    .state
                    .records
                    .iter()
                    .filter(|record| {
                        record.kind == "nft_transfer"
                            && record.token.as_deref() == Some(symbol.as_str())
                    })
                    .count();
                json!({
                    "symbol": symbol,
                    "name": self.state.tokens.get(symbol).map(|token| token.name.clone())
                        .unwrap_or_else(|| symbol.clone()),
                    "owner": owner,
                    "creator": self.token_creator(symbol),
                    "metadata_uri": self.token_metadata_uri(symbol),
                    "created_at": self.state.tokens.get(symbol).map(|token| token.created_at).unwrap_or(0),
                    "transfer_count": transfers,
                })
            })
            .collect();
        let count = rows.len();
        rows.sort_by(|left, right| {
            left["symbol"]
                .as_str()
                .unwrap_or("")
                .cmp(right["symbol"].as_str().unwrap_or(""))
        });
        let page: Vec<serde_json::Value> = rows.into_iter().skip(offset).take(limit).collect();
        json!({
            "standard": "oneness-tor-nft-catalogue/1",
            "height": self.state.height,
            "count": count,
            "limit": limit,
            "offset": offset,
            "nfts": page,
            "note": "a unique edition is a token whose whole supply is 1 with 0 decimals; \
                     `owner` is the holder of the whole supply",
        })
    }

    /// The NFT pane of one wallet: the unique editions it holds right now.
    pub fn account_nfts(&self, address: &str) -> ChainResult<serde_json::Value> {
        let holdings = self.account_holdings(address)?;
        let rows = holdings
            .get("nfts")
            .and_then(|value| value.as_array())
            .cloned()
            .unwrap_or_default();
        Ok(json!({
            "standard": "oneness-tor-nft-holdings/1",
            "address": holdings.get("address").cloned().unwrap_or(serde_json::Value::Null),
            "exists": holdings.get("exists").cloned().unwrap_or(json!(false)),
            "height": self.state.height,
            "count": rows.len(),
            "nfts": rows,
            "note": "this address's unique editions (supply 1, 0 decimals); a wallet with none \
                     answers an empty list rather than 404 — the pane must open for a new wallet",
        }))
    }

    /// NFT history: every mint and every transfer, newest first.
    ///
    /// `address` narrows it to the records where that wallet was the sender or the
    /// receiver (and adds `direction`); `symbol` narrows it to one edition.
    /// A creation record appears as kind `token_create` with amount 1 — the mint
    /// *is* a `token_create`, and pretending otherwise would mean two sources of
    /// truth for the same event.
    pub fn nft_history(
        &self,
        address: Option<&str>,
        symbol: Option<&str>,
        limit: usize,
    ) -> ChainResult<serde_json::Value> {
        let address = address.map(|value| value.trim().to_string());
        if let Some(wanted) = address.as_deref() {
            if !wanted.is_empty() && !is_address(wanted) {
                return Err(ChainError::validation(
                    "address must be a TOR1…onion address",
                ));
            }
        }
        let symbol = symbol.map(|value| value.trim().to_uppercase());
        let limit = limit.clamp(1, 500);
        let mut rows: Vec<serde_json::Value> = Vec::new();
        for record in self.state.records.iter().rev() {
            let record_symbol = record.token.clone().unwrap_or_default();
            let unique_mint = record.kind == "token_create"
                && self
                    .state
                    .tokens
                    .get(&record_symbol)
                    .map(|token| token.supply == 1 && token.decimals == 0)
                    .unwrap_or(false);
            if record.kind != "nft_transfer" && !unique_mint {
                continue;
            }
            if let Some(wanted) = symbol.as_deref() {
                if !wanted.is_empty() && record_symbol != wanted {
                    continue;
                }
            }
            let direction = match address.as_deref() {
                Some(wanted) if !wanted.is_empty() => {
                    if record.from == wanted && record.to == wanted {
                        Some("self")
                    } else if record.from == wanted {
                        Some("out")
                    } else if record.to == wanted {
                        Some("in")
                    } else {
                        continue;
                    }
                }
                _ => None,
            };
            let counterparty = match direction {
                Some("in") => record.from.clone(),
                Some(_) => record.to.clone(),
                None => record.to.clone(),
            };
            let confirmations = self
                .state
                .height
                .saturating_sub(record.height)
                .saturating_add(1);
            rows.push(json!({
                "txid": record.txid.clone(),
                "kind": record.kind.clone(),
                "action": if record.kind == "nft_transfer" { "transfer" } else { "mint" },
                "symbol": record_symbol.clone(),
                "name": self.state.tokens.get(&record_symbol)
                    .map(|token| token.name.clone())
                    .unwrap_or_else(|| record_symbol.clone()),
                "from": record.from.clone(),
                "to": record.to.clone(),
                "counterparty": counterparty,
                "direction": match direction {
                    Some(value) => json!(value),
                    None => serde_json::Value::Null,
                },
                "amount": record.amount,
                "fee": record.fee,
                "note": record.note.clone(),
                "height": record.height,
                "block_hash": record.block_hash.clone(),
                "timestamp": record.timestamp,
                "seq": record.seq,
                "confirmations": confirmations,
                "confirmed": confirmations >= 1,
                "status": if confirmations >= 1 { "confirmed" } else { "pending" },
                "visibility": record.visibility.clone(),
                "private": record.visibility != "public",
                // the sender's privacy choice rides here for every kind, so the
                // Python layer applies one masking rule to NFT rows too
                "external_ref": record.external_ref.clone(),
                "signature": record.signature.clone(),
                "metadata_uri": if record.kind == "token_create" {
                    record.external_ref.clone()
                } else {
                    self.token_metadata_uri(&record_symbol)
                },
                "detail": format!("/api/nft/tx/{}", record.txid),
            }));
        }
        let count = rows.len();
        let page: Vec<serde_json::Value> = rows.into_iter().take(limit).collect();
        Ok(json!({
            "standard": "oneness-tor-nft-history/1",
            "height": self.state.height,
            "address": match address {
                Some(value) => json!(value),
                None => serde_json::Value::Null,
            },
            "symbol": match symbol {
                Some(value) => json!(value),
                None => serde_json::Value::Null,
            },
            "count": count,
            "limit": limit,
            "records": page,
        }))
    }

    /// The transfer receipt: a record you can check, not a promise.
    ///
    /// It answers the questions a receipt has to answer — is it in a block, how
    /// deep, whose signature is on it, where is the edition **now**, and does the
    /// destination actually hold it — and it re-states the chain head so the
    /// caller can see the state root the answer came from.
    pub fn nft_receipt(&self, txid: &str) -> ChainResult<serde_json::Value> {
        let record = self
            .record_by_id(txid.trim())
            .ok_or_else(|| ChainError::not_found(format!("transaction {txid} not found")))?;
        let symbol = record.token.clone().unwrap_or_default();
        let unique_mint = record.kind == "token_create"
            && self
                .state
                .tokens
                .get(&symbol)
                .map(|token| token.supply == 1 && token.decimals == 0)
                .unwrap_or(false);
        if record.kind != "nft_transfer" && !unique_mint {
            return Err(ChainError::validation(format!(
                "{} is a {} record, not an NFT record",
                record.txid, record.kind
            )));
        }
        let owner_now = self.nft_owner(&symbol).unwrap_or_default();
        let destination_holds = self
            .state
            .accounts
            .get(&record.to)
            .map(|account| account.tokens.get(&symbol).copied().unwrap_or(0) > 0)
            .unwrap_or(false);
        let source_holds = self
            .state
            .accounts
            .get(&record.from)
            .map(|account| account.tokens.get(&symbol).copied().unwrap_or(0) > 0)
            .unwrap_or(false);
        let confirmations = self
            .state
            .height
            .saturating_sub(record.height)
            .saturating_add(1);
        let ownership_ok = owner_now == record.to && destination_holds;
        let verified = confirmations >= 1 && !record.signature.trim().is_empty() && ownership_ok;
        Ok(json!({
            "standard": "oneness-tor-nft-receipt/1",
            "txid": record.txid.clone(),
            "kind": record.kind.clone(),
            "action": if record.kind == "nft_transfer" { "transfer" } else { "mint" },
            "confirmed": confirmations >= 1,
            "confirmations": confirmations,
            "status": if confirmations >= 1 { "confirmed" } else { "pending" },
            "height": record.height,
            "block_hash": record.block_hash.clone(),
            "timestamp": record.timestamp,
            "seq": record.seq,
            "nft": {
                "symbol": symbol.clone(),
                "name": self.state.tokens.get(&symbol).map(|token| token.name.clone())
                    .unwrap_or_else(|| symbol.clone()),
                "standard": "oneness-tor-nft/1",
                "metadata_uri": self.token_metadata_uri(&symbol),
                "creator": self.token_creator(&symbol),
            },
            "transfer": {
                "from": record.from.clone(),
                "to": record.to.clone(),
                "amount": record.amount,
                "fee": record.fee,
                "note": record.note.clone(),
                "signature": record.signature.clone(),
                "external_ref": record.external_ref.clone(),
                "visibility": record.visibility.clone(),
            },
            "ownership": {
                "owner_now": owner_now,
                "destination_holds": destination_holds,
                "source_holds": source_holds,
                "verified": ownership_ok,
            },
            "chain": {
                "chain_id": self.state.chain_id.clone(),
                "height": self.state.height,
                "head_hash": self.state.head_hash.clone(),
                "state_root": self.state_root(),
                "native_coin_id": self.state.native_coin_id.clone(),
                "micro_per_tor": MICRO_PER_TOR,
            },
            "verified": verified,
            "note": if verified {
                "the edition is inside a block, the signature is on the record, and the \
                 destination holds it now"
            } else {
                "this receipt does not verify: the record is unconfirmed, unsigned, or the \
                 edition is no longer where the record says it went"
            },
        }))
    }

    /// The **permanent public ledger**: every account balance, every token
    /// balance and every per-token total, in one auditable document.
    ///
    /// Balances are public data on this chain. A stranger may always ask *how
    /// much TOR does this address hold* and *how much of token X exists, who
    /// holds it and how much is pooled* — no session, no admin token. Only
    /// moving value stays private to the key that signs it.
    ///
    /// `limit`/`offset` page the `accounts` array (the chain is small, but the
    /// page keeps the response bounded); `totals` and `tokens` always describe
    /// **all** accounts, never the page.
    pub fn public_ledger(&self, limit: usize, offset: usize) -> serde_json::Value {
        let limit = limit.clamp(1, 5000);
        let mut total_balance: u64 = 0;
        let mut held: BTreeMap<String, u64> = BTreeMap::new();
        let mut holders: BTreeMap<String, u64> = BTreeMap::new();
        let mut accounts: Vec<serde_json::Value> = Vec::with_capacity(self.state.accounts.len());
        for account in self.state.accounts.values() {
            total_balance = total_balance.saturating_add(account.balance);
            for (symbol, amount) in account.tokens.iter() {
                if *amount == 0 {
                    continue;
                }
                *held.entry(symbol.clone()).or_insert(0) =
                    held.get(symbol).copied().unwrap_or(0).saturating_add(*amount);
                *holders.entry(symbol.clone()).or_insert(0) =
                    holders.get(symbol).copied().unwrap_or(0) + 1;
            }
            accounts.push(serde_json::json!({
                "address": account.address.clone(),
                "balance_micro": account.balance,
                "balance": tor_value(account.balance),
                "token_balances": account.tokens.clone(),
                "role": account.role.clone(),
                "created_at": account.created_at,
            }));
        }
        let account_count = accounts.len();
        let page: Vec<serde_json::Value> = accounts.into_iter().skip(offset).take(limit).collect();
        let tokens: serde_json::Map<String, serde_json::Value> = self
            .state
            .tokens
            .iter()
            .map(|(symbol, token)| {
                let in_pools = self
                    .state
                    .pools
                    .get(symbol)
                    .map(|pool| pool.token_reserve)
                    .unwrap_or(0);
                (
                    symbol.clone(),
                    serde_json::json!({
                        "symbol": token.symbol,
                        "name": token.name,
                        "supply": token.supply,
                        "decimals": token.decimals,
                        "owner": token.owner,
                        "created_at": token.created_at,
                        "in_pools": in_pools,
                        "held": held.get(symbol).copied().unwrap_or(0),
                        "holders": holders.get(symbol).copied().unwrap_or(0),
                        "circulating": token.supply.saturating_sub(in_pools),
                    }),
                )
            })
            .collect();
        serde_json::json!({
            "standard": PUBLIC_LEDGER_STANDARD,
            "chain_id": self.state.chain_id.clone(),
            "native_coin_id": self.state.native_coin_id.clone(),
            "height": self.state.height,
            "head_hash": self.state.head_hash.clone(),
            "genesis_hash": self.state.genesis_hash.clone(),
            "micro_per_tor": MICRO_PER_TOR,
            "total_supply_micro": self.state.total_supply,
            "total_supply": tor_value(self.state.total_supply),
            "founder": self.state.founder.clone(),
            "fee_receiver": self.state.fee_receiver.clone(),
            "counts": {
                "accounts": account_count,
                "tokens": self.state.tokens.len(),
                "pools": self.state.pools.len(),
                "records": self.state.records.len(),
            },
            "totals": {
                "accounts": account_count,
                "balance_micro": total_balance,
                "balance": tor_value(total_balance),
                "tokens": self.state.tokens.len(),
                "pools": self.state.pools.len(),
                "holders": holders.len(),
                "token_supply": self
                    .state
                    .tokens
                    .iter()
                    .map(|(symbol, token)| (symbol.clone(), token.supply))
                    .collect::<BTreeMap<String, u64>>(),
            },
            "accounts_total": account_count,
            "offset": offset,
            "limit": limit,
            "accounts": page,
            "tokens": tokens,
            "pools": self.state.pools,
        })
    }

    /// Every record the chain holds, newest first. Records are permanent: the
    /// ledger has no expiry clock and seals nothing.
    pub fn public_records(&self, limit: usize) -> Vec<TxRecord> {
        let mut out: Vec<TxRecord> = self
            .state
            .records
            .iter()
            .filter(|record| record.visibility == "public")
            .cloned()
            .collect();
        out.reverse();
        out.truncate(limit);
        out
    }

    /// Records that a previous release sealed into the private archive. New
    /// records are never sealed: the window that did it no longer exists.
    pub fn private_records(&self, limit: usize) -> Vec<TxRecord> {
        let mut out: Vec<TxRecord> = self
            .state
            .records
            .iter()
            .filter(|record| record.visibility != "public")
            .cloned()
            .collect();
        out.reverse();
        out.truncate(limit);
        out
    }

    /// Point every platform/service fee at a different wallet (admin only at the
    /// HTTP layer). The account is created on the first credit, so a brand-new
    /// admin wallet can start collecting immediately.
    pub fn set_fee_receiver(&mut self, address: &str) -> ChainResult<()> {
        let wanted = address.trim();
        if !is_address(wanted) {
            return Err(ChainError::validation(
                "fee receiver must look like TOR1…onion",
            ));
        }
        if wanted == self.state.fee_receiver {
            return Ok(());
        }
        self.state.fee_receiver = wanted.to_string();
        self.save()
    }

    pub fn check_admin_token(&self, token: &str) -> ChainResult<()> {
        if token.trim().is_empty() {
            return Err(ChainError::unauthorized("admin token is required for private data"));
        }
        if sha3_hex(token.trim().as_bytes()) != self.state.admin_token_sha256 {
            return Err(ChainError::unauthorized("admin token is invalid"));
        }
        Ok(())
    }

    pub fn record_by_id(&self, txid: &str) -> Option<&TxRecord> {
        self.state.records.iter().find(|record| record.txid == txid)
    }

    pub fn block_by_height(&self, height: u64) -> Option<&Block> {
        self.blocks.iter().find(|block| block.height == height)
    }

    pub fn blocks_range(&self, from: u64, to: u64) -> Vec<Block> {
        self.blocks
            .iter()
            .filter(|block| block.height >= from && block.height <= to)
            .cloned()
            .collect()
    }

    /// Re-hash the whole block chain and re-check supply conservation.
    pub fn verify(&self) -> serde_json::Value {
        let mut errors: Vec<String> = Vec::new();
        let mut previous = "0".repeat(64);
        for block in self.blocks.iter() {
            if block.prev_hash != previous {
                errors.push(format!("height {}: prev_hash mismatch", block.height));
            }
            let txids = block.txids.clone();
            match block_digest(block, &txids) {
                Ok(digest) => {
                    if digest != block.hash {
                        errors.push(format!("height {}: block hash mismatch", block.height));
                    }
                }
                Err(error) => errors.push(format!("height {}: {error}", block.height)),
            }
            previous = block.hash.clone();
        }
        if let Some(last) = self.blocks.last() {
            if last.hash != self.state.head_hash {
                errors.push("state.head_hash does not match the newest block".to_string());
            }
            if last.state_root != state_root_of(&self.state) {
                errors.push("state root does not match the checkpoint state".to_string());
            }
        }
        let balance_sum: u128 = self
            .state
            .accounts
            .values()
            .map(|account| account.balance as u128)
            .sum();
        if balance_sum != self.state.total_supply as u128 {
            errors.push(format!(
                "supply mismatch: balances {balance_sum} != total_supply {}",
                self.state.total_supply
            ));
        }
        serde_json::json!({
            "ok": errors.is_empty(),
            "blocks": self.blocks.len(),
            "height": self.state.height,
            "total_supply_micro": self.state.total_supply,
            "balance_sum_micro": balance_sum,
            "records": self.state.records.len(),
            "errors": errors,
        })
    }
}


