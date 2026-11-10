//! `torchain` — the OneNess TOR native chain manager.
//!
//! Permanent ledger, Ed25519 Phantom-compatible wallets, transfers, deposits,
//! withdrawals, token creation, AMM swaps and minting all live here. Records never
//! expire; the sending wallet picks public or anonymous per transaction.

mod chain;
mod coin;
mod pricing;
mod server;
mod util;
mod wallet;

use std::path::PathBuf;

use chain::{Chain, SignedTx};
use util::{format_tor, now_secs, parse_tor, sha3_hex, ChainError, ChainResult, MICRO_PER_TOR};
use wallet::{load_wallet, KeyPair};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Err(error) = run(&args) {
        eprintln!("error {error}");
        std::process::exit(1);
    }
}

fn print_help() {
    println!(
        "torchain — OneNess TOR chain manager (native coin {})\n\
\n\
USAGE\n\
  torchain <command> [flags]\n\
\n\
SETUP\n\
  init --founder <wallet.json> [--supply 50000000] [--admin-token X]\n\
  wallet new [--words 24] [--out path]      generate a Phantom-compatible wallet\n\
  wallet from-mnemonic --mnemonic \"...\"     restore from a BIP39 phrase\n\
  wallet show --wallet <file>               print address / pubkey / secret\n\
  address --pubkey <base58 | --wallet file> derive the TOR1…onion address\n\
\n\
LEDGER\n\
  balance --address <TOR1…onion | --wallet file>\n\
  transfer --key <wallet> --to <addr> --amount 1.5 [--token SYM]\n\
  deposit  --key <founder> --to <addr> --amount 10 [--ref ext-id]\n\
  withdraw --key <founder> --to <addr> --amount 2.5 [--ref ext-id]\n\
  token-create --key <wallet> --token MEME --amount 1000000 [--note \"name\"]\n\
  nft-transfer --key <owner> --token SYM --to <addr> [--note \"\"]   move a unique edition\n\
  nft list | show --token SYM | history [--token SYM] [--address A] | holdings --address A\n\
  pool-create  --key <wallet> --token MEME --amount 500000 --tor 100\n\
  swap --key <wallet> --token MEME --amount 5 --direction buy|sell\n\
  mint --key <founder> --token MEME --amount 1000\n\
\n\
LEDGER / NODE\n\
  public [--limit 50]        every record, newest first (permanent)\n\
  private --admin-token X    records sealed by an older release\n\
  verify                     re-hash the block chain, check supply\n\
  serve [--bind 127.0.0.1:8545]\n\
  pricing                    the locked price table
  coin                       TOR coin metadata (name, symbol, decimals, logo)\n\
\n\
Every command accepts --dir <chain dir> (default ./data/chain).",
        util::NATIVE_COIN_ID
    );
}

fn flag(args: &[String], name: &str) -> Option<String> {
    let mut index = 0;
    while index < args.len() {
        if args[index] == name {
            return args.get(index + 1).cloned();
        }
        index += 1;
    }
    None
}

fn data_dir(args: &[String]) -> PathBuf {
    flag(args, "--dir")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("data").join("chain"))
}

fn run(args: &[String]) -> ChainResult<()> {
    let command = args.first().map(|value| value.as_str()).unwrap_or("help");
    match command {
        "help" | "--help" | "-h" => {
            print_help();
            Ok(())
        }
        "pricing" => {
            for row in pricing::PRICE_TABLE.iter() {
                println!(
                    "{:>7} TOR  {:<20} {}{}",
                    row.tor,
                    row.code,
                    row.label,
                    if row.locked { "" } else { "  (configurable)" }
                );
            }
            Ok(())
        }
        "coin" => {
            let document =
                serde_json::to_string_pretty(&coin::as_metadata()).map_err(|e| ChainError::json(e))?;
            println!("{document}");
            Ok(())
        }
        "init" => cmd_init(args),
        "wallet" => cmd_wallet(args),
        "address" => cmd_address(args),
        "balance" => cmd_balance(args),
        "transfer" => cmd_send(args, "transfer"),
        "deposit" => cmd_send(args, "deposit"),
        "withdraw" => cmd_send(args, "withdraw"),
        "token-create" => cmd_send(args, "token_create"),
        "nft-transfer" => cmd_send(args, "nft_transfer"),
        "nft" => cmd_nft(args),
        "pool-create" => cmd_send(args, "pool_create"),
        "swap" => cmd_send(args, "swap"),
        "mint" => cmd_send(args, "mint"),
        "public" => cmd_public(args),
        "private" => cmd_private(args),
        "verify" => cmd_verify(args),
        "serve" => cmd_serve(args),
        other => Err(ChainError::validation(format!(
            "unknown command {other:?} — run `torchain help`"
        ))),
    }
}

fn cmd_init(args: &[String]) -> ChainResult<()> {
    let dir = data_dir(args);
    let founder_path = flag(args, "--founder")
        .ok_or_else(|| ChainError::validation("--founder <wallet.json> is required"))?;
    let founder = load_wallet(&founder_path)?;
    let supply = match flag(args, "--supply") {
        Some(text) => parse_tor(&text)?,
        None => 50_000_000 * MICRO_PER_TOR,
    };
    let admin_token = flag(args, "--admin-token").unwrap_or_else(|| {
        sha3_hex(
            format!(
                "{}:{}:{}",
                founder.pubkey_base58,
                now_secs(),
                std::process::id()
            )
            .as_bytes(),
        )
    });
    let chain = Chain::init(
        &dir,
        &founder.address,
        &founder.pubkey_base58,
        supply,
        &admin_token,
    )?;
    println!("chain           : {}", dir.display());
    println!("chain id        : {}", chain.state.chain_id);
    println!("genesis hash    : {}", chain.state.genesis_hash);
    println!("founder wallet  : {}", chain.state.founder);
    println!("fee receiver    : {}", chain.state.fee_receiver);
    println!("supply minted   : {} TOR", format_tor(chain.state.total_supply));
    println!("admin token     : {admin_token}");
    println!("note            : only sha3-256(admin token) is stored — keep it safe");
    Ok(())
}

fn cmd_wallet(args: &[String]) -> ChainResult<()> {
    let action = args.get(1).map(|value| value.as_str()).unwrap_or("new");
    match action {
        "new" => {
            let words = flag(args, "--words")
                .and_then(|value| value.parse::<usize>().ok())
                .unwrap_or(24);
            let (keypair, phrase) = KeyPair::generate(words, wallet::DERIVATION_PATH)?;
            let file = keypair.wallet_file(Some(phrase.clone()));
            let address = keypair.address();
            let short = if address.len() > 16 { &address[..16] } else { &address[..] };
            let out = flag(args, "--out")
                .unwrap_or_else(|| format!("data/wallets/{short}.json"));
            if let Some(parent) = PathBuf::from(&out).parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|e| ChainError::io(format!("mkdir {}: {e}", parent.display())))?;
            }
            let text = serde_json::to_string_pretty(&file).map_err(|e| ChainError::json(e))?;
            std::fs::write(&out, text + "\n")
                .map_err(|e| ChainError::io(format!("write {out}: {e}")))?;
            println!("wallet file : {out}");
            println!("address     : {}", file.address);
            println!("pubkey b58  : {}", file.pubkey_base58);
            println!("secret b58  : {}", file.secret_key_base58);
            if !args.iter().any(|value| value == "--hide-mnemonic") {
                println!("mnemonic    : {phrase}");
            }
            Ok(())
        }
        "from-mnemonic" => {
            let phrase = flag(args, "--mnemonic")
                .ok_or_else(|| ChainError::validation("--mnemonic \"...\" is required"))?;
            let keypair = KeyPair::from_mnemonic(&phrase, wallet::DERIVATION_PATH)?;
            println!("address     : {}", keypair.address());
            println!("pubkey b58  : {}", keypair.pubkey_base58());
            println!("secret b58  : {}", keypair.secret_base58());
            Ok(())
        }
        "show" => {
            let path = flag(args, "--wallet")
                .or_else(|| flag(args, "--key"))
                .ok_or_else(|| ChainError::validation("--wallet <file> is required"))?;
            let file = load_wallet(&path)?;
            println!("address     : {}", file.address);
            println!("pubkey b58  : {}", file.pubkey_base58);
            println!("secret b58  : {}", file.secret_key_base58);
            Ok(())
        }
        other => Err(ChainError::validation(format!(
            "unknown wallet action {other:?} (new | from-mnemonic | show)"
        ))),
    }
}

fn cmd_address(args: &[String]) -> ChainResult<()> {
    if let Some(pubkey) = flag(args, "--pubkey") {
        let raw = bs58::decode(pubkey.trim())
            .into_vec()
            .map_err(|e| ChainError::validation(format!("pubkey is not base58: {e}")))?;
        if raw.len() != 32 {
            return Err(ChainError::validation("pubkey must decode to 32 bytes"));
        }
        println!("{}", util::address_from_pubkey(&raw));
        return Ok(());
    }
    if let Some(path) = flag(args, "--wallet") {
        let file = load_wallet(&path)?;
        println!("{}", file.address);
        return Ok(());
    }
    Err(ChainError::validation("--pubkey <base58> or --wallet <file> is required"))
}

fn cmd_balance(args: &[String]) -> ChainResult<()> {
    let dir = data_dir(args);
    let chain = Chain::open(&dir)?;
    let target = match flag(args, "--address") {
        Some(address) => address,
        None => {
            let path = flag(args, "--wallet")
                .ok_or_else(|| ChainError::validation("--address or --wallet is required"))?;
            load_wallet(&path)?.address
        }
    };
    let account = chain.account_json(&target)?;
    println!("address : {}", account["address"]);
    println!("balance : {} TOR ({} microTOR)", account["balance"], account["balance_micro"]);
    println!("nonce   : {}", account["nonce"]);
    println!("role    : {}", account["role"]);
    println!("tokens  : {}", account["tokens"]);
    Ok(())
}

fn cmd_send(args: &[String], kind: &str) -> ChainResult<()> {
    let dir = data_dir(args);
    let mut chain = Chain::open(&dir)?;
    let wallet_path = flag(args, "--key")
        .or_else(|| flag(args, "--wallet"))
        .ok_or_else(|| ChainError::validation("--key <wallet.json> is required"))?;
    let file = load_wallet(&wallet_path)?;
    let keypair = KeyPair::from_secret_b58(&file.secret_key_base58)?;
    if keypair.address() != file.address {
        return Err(ChainError::new(
            "WALLET_MISMATCH",
            "wallet address does not match its secret key",
        ));
    }
    let token = flag(args, "--token");
    let mut note = flag(args, "--note").unwrap_or_default();
    match kind {
        "pool_create" => {
            note = flag(args, "--tor")
                .ok_or_else(|| ChainError::validation("--tor <TOR reserve> is required"))?;
        }
        "swap" => {
            note = flag(args, "--direction").unwrap_or_else(|| "buy".to_string());
        }
        _ => {}
    }
    let amount_text = flag(args, "--amount").unwrap_or_else(|| {
        if kind == "nft_transfer" {
            // an edition is a single unit; the chain refuses anything else, so the
            // CLI defaults to the only value that can work
            "1".to_string()
        } else {
            "0".to_string()
        }
    });
    let whole = |text: &str| -> ChainResult<u64> {
        text.trim().parse::<u64>().map_err(|_| {
            ChainError::validation(format!(
                "{kind} amount must be a whole number of token units, got {text:?}"
            ))
        })
    };
    let amount = match kind {
        "token_create" | "mint" | "pool_create" | "nft_transfer" => whole(&amount_text)?,
        "swap" if note.eq_ignore_ascii_case("sell") => whole(&amount_text)?,
        "transfer" if token.is_some() => whole(&amount_text)?,
        _ => parse_tor(&amount_text)?,
    };
    let nonce = chain
        .state
        .accounts
        .get(&file.address)
        .map(|account| account.nonce)
        .unwrap_or(0);
    let mut tx = SignedTx {
        kind: kind.to_string(),
        from: file.address.clone(),
        to: flag(args, "--to").unwrap_or_default(),
        amount,
        token,
        note,
        external_ref: flag(args, "--ref").unwrap_or_default(),
        nonce,
        timestamp: now_secs(),
        pubkey_base58: keypair.pubkey_base58(),
        signature: String::new(),
    };
    tx.signature = keypair.sign_base58(&tx.message());
    let proposer = chain.state.founder.clone();
    let records = chain.apply_signed(&tx, &proposer)?;
    for record in records.iter() {
        println!(
            "{}  {} → {}  amount={} fee={}\n  txid {}  block {}",
            record.kind.to_uppercase(),
            record.from,
            record.to,
            record.amount,
            record.fee,
            record.txid,
            record.height,
        );
    }
    if let Ok(account) = chain.account_json(&file.address) {
        println!("balance now : {} TOR", account["balance"]);
    }
    Ok(())
}

fn limit_of(args: &[String]) -> usize {
    flag(args, "--limit")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(50)
        .clamp(1, 500)
}

/// `torchain nft …` — the read side of the NFT transfer surface.
///
/// The write side is `nft-transfer`, which goes through the same `apply_signed`
/// door as every other kind, so nothing about ownership is decided here.
fn cmd_nft(args: &[String]) -> ChainResult<()> {
    let chain = Chain::open(&data_dir(args))?;
    let action = args.get(1).map(|value| value.as_str()).unwrap_or("list");
    let symbol = flag(args, "--token")
        .or_else(|| flag(args, "--symbol"))
        .or_else(|| args.get(2).filter(|value| !value.starts_with("--")).cloned());
    let print = |value: &serde_json::Value| -> ChainResult<()> {
        println!(
            "{}",
            serde_json::to_string_pretty(value).map_err(|e| ChainError::json(e))?
        );
        Ok(())
    };
    match action {
        "list" => print(&chain.nft_catalogue(limit_of(args), 0)),
        "show" => {
            let wanted = symbol.ok_or_else(|| {
                ChainError::validation("nft show --token <SYMBOL> is required")
            })?;
            print(&chain.nft_json(&wanted)?)
        }
        "history" => print(&chain.nft_history(
            flag(args, "--address").as_deref(),
            symbol.as_deref(),
            limit_of(args),
        )?),
        "holdings" => {
            let address = match flag(args, "--address") {
                Some(value) => value,
                None => {
                    let path = flag(args, "--wallet").ok_or_else(|| {
                        ChainError::validation(
                            "nft holdings --address <TOR1…onion> (or --wallet <file>) is required",
                        )
                    })?;
                    load_wallet(&path)?.address
                }
            };
            print(&chain.account_nfts(&address)?)
        }
        "receipt" => {
            let txid = flag(args, "--txid").or_else(|| symbol.clone()).ok_or_else(|| {
                ChainError::validation("nft receipt --txid <id> is required")
            })?;
            print(&chain.nft_receipt(&txid)?)
        }
        other => Err(ChainError::validation(format!(
            "unknown nft action {other:?} (list | show | history | holdings | receipt)"
        ))),
    }
}

fn cmd_public(args: &[String]) -> ChainResult<()> {
    let chain = Chain::open(&data_dir(args))?;
    let records = chain.public_records(limit_of(args));
    println!("{} permanent record(s)", records.len());
    for record in records.iter() {
        println!(
            "#{} {} {} → {} amount={} fee={}",
            record.seq,
            record.kind,
            record.from,
            record.to,
            record.amount,
            record.fee
        );
    }
    Ok(())
}

fn cmd_private(args: &[String]) -> ChainResult<()> {
    let chain = Chain::open(&data_dir(args))?;
    let token = flag(args, "--admin-token")
        .ok_or_else(|| ChainError::unauthorized("--admin-token is required for private data"))?;
    chain.check_admin_token(&token)?;
    let records = chain.private_records(limit_of(args));
    println!(
        "{} record(s) sealed by an older release (nothing is sealed any more)",
        records.len()
    );
    for record in records.iter() {
        println!(
            "#{} {} {} → {} amount={} fee={} visibility={}",
            record.seq,
            record.kind,
            record.from,
            record.to,
            record.amount,
            record.fee,
            record.visibility
        );
    }
    Ok(())
}

fn cmd_verify(args: &[String]) -> ChainResult<()> {
    let chain = Chain::open(&data_dir(args))?;
    let report = chain.verify();
    println!(
        "{}",
        serde_json::to_string_pretty(&report).map_err(|e| ChainError::json(e))?
    );
    if report["ok"].as_bool() != Some(true) {
        return Err(ChainError::new("CHAIN_INVALID", "verification found problems"));
    }
    Ok(())
}

fn cmd_serve(args: &[String]) -> ChainResult<()> {
    let dir = data_dir(args);
    let chain = Chain::open(&dir)?;
    let bind = flag(args, "--bind").unwrap_or_else(|| "127.0.0.1:8545".to_string());
    server::serve(chain, &bind)
}


