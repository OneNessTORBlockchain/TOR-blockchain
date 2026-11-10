//! Minimal dependency-free HTTP/JSON node API (the Python core talks to this).
//!
//! Every response uses the project envelope `{ok, data, error}` so
//! `core/node_client.py` and the web pages can treat the Rust node exactly like
//! the FastAPI layer.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::json;

use crate::chain::{Chain, SignedTx};
use crate::pricing;
use crate::util::{ChainError, ChainResult};

pub struct App {
    pub chain: Mutex<Chain>,
}

pub fn serve(chain: Chain, bind: &str) -> ChainResult<()> {
    let listener = TcpListener::bind(bind)
        .map_err(|e| ChainError::io(format!("cannot bind {bind}: {e}")))?;
    println!("torchain node listening on http://{bind}");
    println!("  GET  /api/chain/info        chain height, supply, founder, fee receiver");
    println!("  GET  /api/account/<address> balance + token holdings");
    println!("  GET  /api/account/<address>/holdings  TOR + tokens + NFTs, joined with the token table");
    println!("  GET  /api/account/<address>/history   that address's records, newest first, + confirmations");
    println!("  GET  /api/account/<address>/nfts      that address's unique editions (NFTs)");
    println!("  GET  /api/nft                         every unique edition + its current owner");
    println!("  GET  /api/nft/<symbol>                ownership lookup: holder, metadata, mint");
    println!("  GET  /api/nft/history?address=&symbol= every NFT mint and transfer, newest first");
    println!("  GET  /api/nft/tx/<txid>               the transfer receipt: block, confirmations, owner now");
    println!("  POST /api/nft/transfer                signed nft_transfer (kind must be nft_transfer)");
    println!("  GET  /api/public/records    every record, newest first");
    println!("  POST /api/chain/tx          signed transaction");
    println!("  POST /api/admin/fee-receiver point platform fees at a wallet");
    println!("  GET  /api/admin/sync/export verified ledger snapshot (for replicas)");
    println!("  POST /api/admin/sync/import adopt a verified snapshot (replica pull)");
    let app = Arc::new(App { chain: Mutex::new(chain) });
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                let shared = Arc::clone(&app);
                std::thread::spawn(move || {
                    let _ = handle(stream, shared);
                });
            }
            Err(error) => eprintln!("accept error: {error}"),
        }
    }
    Ok(())
}

fn handle(mut stream: TcpStream, app: Arc<App>) -> std::io::Result<()> {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(15)));
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line)? == 0 {
        return Ok(());
    }
    let mut headers: Vec<(String, String)> = Vec::new();
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            break;
        }
        let trimmed = line.trim_end();
        if trimmed.is_empty() {
            break;
        }
        if let Some((name, value)) = trimmed.split_once(':') {
            headers.push((name.trim().to_lowercase(), value.trim().to_string()));
        }
    }
    let content_length: usize = headers
        .iter()
        .find(|(name, _)| name == "content-length")
        .and_then(|(_, value)| value.parse().ok())
        .unwrap_or(0);
    let mut body = vec![0u8; content_length];
    if content_length > 0 {
        reader.read_exact(&mut body)?;
    }
    let parts: Vec<&str> = request_line.split_whitespace().collect();
    if parts.len() < 2 {
        return respond(&mut stream, 400, &failure(ChainError::validation("malformed request")));
    }
    let method = parts[0].to_uppercase();
    let target = parts[1].to_string();
    let (path, query) = split_target(&target);
    let admin_token = headers
        .iter()
        .find(|(name, _)| name == "x-admin-token")
        .map(|(_, value)| value.clone())
        .unwrap_or_default();

    match app.chain.lock() {
        Ok(mut chain) => match route(&mut chain, &method, &path, &query, &body, &admin_token) {
            Ok(value) => respond(&mut stream, 200, &success(value)),
            Err(error) => {
                let status = status_for(&error.code);
                respond(&mut stream, status, &failure(error))
            }
        },
        Err(_) => respond(
            &mut stream,
            500,
            &failure(ChainError::new("LOCK_ERROR", "chain state lock is poisoned")),
        ),
    }
}

fn split_target(target: &str) -> (String, String) {
    match target.split_once('?') {
        Some((path, query)) => (path.to_string(), query.to_string()),
        None => (target.to_string(), String::new()),
    }
}

fn query_value(query: &str, key: &str) -> Option<String> {
    for pair in query.split('&') {
        let mut parts = pair.splitn(2, '=');
        if parts.next() == Some(key) {
            let raw = parts.next().unwrap_or("");
            return Some(raw.replace('+', " ").replace("%20", " "));
        }
    }
    None
}

fn limit_of(query: &str) -> usize {
    query_value(query, "limit")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(50)
        .clamp(1, 500)
}

fn offset_of(query: &str) -> usize {
    query_value(query, "offset")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(0)
}

fn success(data: serde_json::Value) -> String {
    json!({ "ok": true, "data": data, "error": null }).to_string()
}

fn failure(error: ChainError) -> String {
    json!({
        "ok": false,
        "data": null,
        "error": { "code": error.code, "message": error.message }
    })
    .to_string()
}

fn status_for(code: &str) -> u16 {
    match code {
        "NOT_FOUND" | "UNKNOWN_TOKEN" | "POOL_NOT_FOUND" => 404,
        "UNAUTHORIZED" => 403,
        "INSUFFICIENT_FUNDS" => 402,
        "VALIDATION_ERROR" | "BAD_JSON" | "INVALID_SIGNATURE" | "ADDRESS_MISMATCH" => 422,
        "NONCE_MISMATCH" | "CONFLICT" => 409,
        "IO_ERROR" | "LOCK_ERROR" | "OVERFLOW" => 500,
        _ => 400,
    }
}

fn respond(stream: &mut TcpStream, status: u16, body: &str) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        402 => "Payment Required",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        422 => "Unprocessable Entity",
        _ => "Internal Server Error",
    };
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {}\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Headers: content-type,x-admin-token\r\nConnection: close\r\n\r\n",
        body.as_bytes().len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body.as_bytes())?;
    stream.flush()
}

fn route(
    chain: &mut Chain,
    method: &str,
    path: &str,
    query: &str,
    body: &[u8],
    admin_token: &str,
) -> ChainResult<serde_json::Value> {
    match (method, path) {
        ("GET", "/health") => Ok(json!({
            "status": "ok",
            "service": "torchain",
            "height": chain.state.height,
        })),
        ("GET", "/api/chain/info") => Ok(chain.info()),
        ("GET", "/api/coin") => Ok(crate::coin::as_metadata()),
        ("GET", "/api/pricing") => Ok(json!({
            "rows": pricing::as_json(),
            "native_coin_id": chain.state.native_coin_id.clone(),
            "coin": crate::coin::as_json(),
            "fees": {
                "base_layer_tx_fee_micro": crate::chain::BASE_TX_FEE_MICRO,
                "base_layer_tx_fee_tor": 0.0,
                "note": "the chain charges no transaction fee; rows are the node/platform fee schedule enforced by the Python API",
            },
        })),
        ("POST", "/api/admin/fee-receiver") => {
            chain.check_admin_token(admin_token)?;
            let payload: serde_json::Value = serde_json::from_slice(body)
                .map_err(|e| ChainError::json(format!("body must be JSON: {e}")))?;
            let address = payload
                .get("address")
                .and_then(|value| value.as_str())
                .ok_or_else(|| ChainError::validation("address is required"))?;
            chain.set_fee_receiver(address)?;
            Ok(json!({
                "fee_receiver": chain.state.fee_receiver,
                "base_layer_tx_fee_micro": crate::chain::BASE_TX_FEE_MICRO,
                "height": chain.state.height,
            }))
        }
        ("GET", "/api/chain/pending") => Ok(json!({
            "pending": [],
            "note": "single-node PoA confirms synchronously; a mempool arrives with peer sync",
        })),
        ("GET", "/api/chain/events") => Ok(json!({
            "events": chain.public_records(limit_of(query)),
            "note": "every record is permanent; the wallet picks public or anonymous per transaction",
        })),
        ("GET", "/api/chain/blocks") => {
            let from = query_value(query, "from")
                .and_then(|value| value.parse::<u64>().ok())
                .unwrap_or(0);
            let to = query_value(query, "to")
                .and_then(|value| value.parse::<u64>().ok())
                .unwrap_or(chain.state.height);
            Ok(json!(chain.blocks_range(from, to)))
        }
        ("GET", "/api/admin/sync/export") => {
            chain.check_admin_token(admin_token)?;
            Ok(chain.snapshot())
        }
        ("POST", "/api/admin/sync/import") => {
            chain.check_admin_token(admin_token)?;
            let snapshot: serde_json::Value = serde_json::from_slice(body)
                .map_err(|e| ChainError::json(format!("snapshot is not valid JSON: {e}")))?;
            let dir = chain.dir.clone();
            let local_admin = chain.state.admin_token_sha256.clone();
            let merged = Chain::import_snapshot(&dir, &snapshot, Some(&local_admin))?;
            let summary = json!({
                "imported": true,
                "blocks": merged.blocks.len(),
                "height": merged.state.height,
                "head_hash": merged.state.head_hash,
                "genesis_hash": merged.state.genesis_hash,
                "state_root": merged.state_root(),
                "tokens": merged.state.tokens.len(),
                "accounts": merged.state.accounts.len(),
                "records": merged.state.records.len(),
            });
            *chain = merged;
            Ok(summary)
        }
        // ---- the NFT transfer surface --------------------------------------
        // Six questions, six answers: what exists (`/api/nft`), what is one
        // edition (`/api/nft/<symbol>`), what a wallet holds
        // (`/api/account/<addr>/nfts`), what moved (`/api/nft/history`), did it
        // land (`/api/nft/tx/<txid>`), and the door that moves it
        // (`POST /api/nft/transfer` — the same `apply_signed` as
        // `POST /api/chain/tx`, refusing any other kind).
        ("GET", "/api/nft") => Ok(chain.nft_catalogue(limit_of(query), offset_of(query))),
        ("GET", "/api/nft/history") => chain.nft_history(
            query_value(query, "address").as_deref(),
            query_value(query, "symbol").as_deref(),
            limit_of(query),
        ),
        ("POST", "/api/nft/transfer") => {
            let tx: SignedTx = serde_json::from_slice(body)
                .map_err(|e| ChainError::json(format!("signed tx is not valid JSON: {e}")))?;
            if tx.kind.trim().to_lowercase() != "nft_transfer" {
                return Err(ChainError::validation(format!(
                    "this door only moves a unique edition: kind must be nft_transfer, got {:?} \
                     (use POST /api/chain/tx for every other kind)",
                    tx.kind
                )));
            }
            let proposer = chain.state.founder.clone();
            let records = chain.apply_signed(&tx, &proposer)?;
            let receipt = records
                .iter()
                .find(|record| record.kind == "nft_transfer")
                .map(|record| chain.nft_receipt(&record.txid))
                .transpose()?;
            Ok(json!({
                "accepted": records.len(),
                "height": chain.state.height,
                "head_hash": chain.state.head_hash.clone(),
                "records": records,
                "receipt": receipt,
            }))
        }
        ("GET", "/api/public/records") => Ok(json!({
            "records": chain.public_records(limit_of(query)),
        })),
        ("GET", "/api/pools") => Ok(json!(chain.state.pools)),
        ("GET", "/api/tokens") => Ok(json!(chain.state.tokens)),
        ("GET", "/api/public/ledger") => Ok(chain.public_ledger(
            query_value(query, "limit")
                .and_then(|value| value.parse::<usize>().ok())
                .unwrap_or(500),
            query_value(query, "offset")
                .and_then(|value| value.parse::<usize>().ok())
                .unwrap_or(0),
        )),
        ("POST", "/api/private/records") => {
            chain.check_admin_token(admin_token)?;
            Ok(json!({ "records": chain.private_records(limit_of(query)) }))
        }
        ("POST", "/api/chain/tx") => {
            let tx: SignedTx = serde_json::from_slice(body)
                .map_err(|e| ChainError::json(format!("signed tx is not valid JSON: {e}")))?;
            let proposer = chain.state.founder.clone();
            let records = chain.apply_signed(&tx, &proposer)?;
            Ok(json!({
                "accepted": records.len(),
                "height": chain.state.height,
                "head_hash": chain.state.head_hash.clone(),
                "records": records,
            }))
        }
        _ => dynamic_route(chain, method, path, query, admin_token),
    }
}

fn dynamic_route(
    chain: &mut Chain,
    method: &str,
    path: &str,
    query: &str,
    admin_token: &str,
) -> ChainResult<serde_json::Value> {
    if method != "GET" {
        return Err(ChainError::not_found(format!("no route for {method} {path}")));
    }
    if let Some(rest) = path.strip_prefix("/api/account/") {
        // `/api/account/<address>`            balance + the raw token map
        // `/api/account/<address>/holdings`   the same, joined with the token table
        // `/api/account/<address>/history`    this address's records + confirmations
        // `/api/account/<address>/nfts`       this address's unique editions
        let mut parts = rest.splitn(2, '/');
        let address = parts.next().unwrap_or("");
        return match parts.next() {
            None => chain.account_json(address),
            Some("holdings") => chain.account_holdings(address),
            Some("nfts") => chain.account_nfts(address),
            Some("history") => {
                // A history that withholds nothing needs the chain admin token;
                // without it, non-public records are simply not listed.
                let include_private =
                    !admin_token.is_empty() && chain.check_admin_token(admin_token).is_ok();
                let limit = query_value(query, "limit")
                    .and_then(|value| value.parse::<usize>().ok())
                    .unwrap_or(100);
                let offset = query_value(query, "offset")
                    .and_then(|value| value.parse::<usize>().ok())
                    .unwrap_or(0);
                chain.account_history(address, limit, offset, include_private)
            }
            Some(other) => Err(ChainError::not_found(format!(
                "no route for GET {path} (expected /history, /holdings or /nfts, got /{other})"
            ))),
        };
    }
    // `/api/nft/tx/<txid>` — the receipt, and the same privacy rule the single
    // record route applies: a non-public record needs the chain admin token.
    if let Some(txid) = path.strip_prefix("/api/nft/tx/") {
        if let Some(record) = chain.record_by_id(txid) {
            if record.visibility != "public" {
                chain.check_admin_token(admin_token)?;
            }
        }
        return chain.nft_receipt(txid);
    }
    // `/api/nft/<symbol>` — ownership lookup for one edition.
    if let Some(symbol) = path.strip_prefix("/api/nft/") {
        if !symbol.is_empty() && !symbol.contains('/') {
            return chain.nft_json(symbol);
        }
    }
    if let Some(txid) = path.strip_prefix("/api/chain/tx/") {
        let record = chain
            .record_by_id(txid)
            .ok_or_else(|| ChainError::not_found(format!("transaction {txid} not found")))?;
        // Records a previous release sealed stay behind the admin token. Nothing
        // is sealed any more: privacy is chosen by the sender, per transaction.
        if record.visibility != "public" {
            chain.check_admin_token(admin_token)?;
        }
        return serde_json::to_value(record).map_err(|e| ChainError::json(e));
    }
    if let Some(raw) = path.strip_prefix("/api/chain/block/") {
        let block = match raw.parse::<u64>() {
            Ok(height) => chain.block_by_height(height).cloned(),
            Err(_) => chain
                .blocks
                .iter()
                .find(|block| block.hash == raw)
                .cloned(),
        };
        return match block {
            Some(found) => serde_json::to_value(found).map_err(|e| ChainError::json(e)),
            None => Err(ChainError::not_found(format!("block {raw} not found"))),
        };
    }
    Err(ChainError::not_found(format!("no route for {method} {path}")))
}

