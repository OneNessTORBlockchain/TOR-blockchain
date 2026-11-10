//! Ed25519 wallets — Phantom-compatible (BIP39 mnemonic + SLIP-0010 `m/44'/501'/0'/0'`).
//!
//! The secret key is `seed || pubkey` (64 bytes) base58-encoded, which is exactly
//! what Phantom imports under "private key". Addresses remain `TOR1…onion`.

use crate::util::{address_from_pubkey, ChainError, ChainResult};
use bip39::{Language, Mnemonic};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha512;

pub const DERIVATION_PATH: &str = "m/44'/501'/0'/0'";
const HARDENED: u32 = 0x8000_0000;

pub struct KeyPair {
    pub signing: SigningKey,
    pub pubkey: [u8; 32],
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WalletFile {
    pub address: String,
    pub pubkey_base58: String,
    pub secret_key_base58: String,
    #[serde(default)]
    pub mnemonic: Option<String>,
}

pub fn slip10_ed25519(seed: &[u8], path: &str) -> ChainResult<[u8; 32]> {
    let mut mac = Hmac::<Sha512>::new_from_slice(b"ed25519 seed")
        .map_err(|e| ChainError::new("CRYPTO_ERROR", e.to_string()))?;
    mac.update(seed);
    let digest = mac.finalize().into_bytes();
    let mut key = [0u8; 32];
    key.copy_from_slice(&digest[..32]);
    let mut chain = [0u8; 32];
    chain.copy_from_slice(&digest[32..]);

    for chunk in path.split('/').skip(1) {
        let trimmed = chunk.trim_end_matches('\'');
        let index: u32 = trimmed
            .parse()
            .map_err(|_| ChainError::validation(format!("bad derivation index {chunk:?}")))?;
        let mut mac = Hmac::<Sha512>::new_from_slice(&chain)
            .map_err(|e| ChainError::new("CRYPTO_ERROR", e.to_string()))?;
        mac.update(&[0u8]);
        mac.update(&key);
        mac.update(&(index | HARDENED).to_be_bytes());
        let digest = mac.finalize().into_bytes();
        key.copy_from_slice(&digest[..32]);
        chain.copy_from_slice(&digest[32..]);
    }
    Ok(key)
}

impl KeyPair {
    pub fn from_seed(seed: &[u8; 32]) -> Self {
        let signing = SigningKey::from_bytes(seed);
        let pubkey = signing.verifying_key().to_bytes();
        Self { signing, pubkey }
    }

    /// Accepts a 64-byte Phantom secret key (seed||pubkey) or a raw 32-byte seed.
    pub fn from_secret_b58(secret: &str) -> ChainResult<Self> {
        let raw = bs58::decode(secret.trim())
            .into_vec()
            .map_err(|e| ChainError::validation(format!("secret key is not valid base58: {e}")))?;
        if raw.len() != 64 && raw.len() != 32 {
            return Err(ChainError::validation(format!(
                "secret key must decode to 32 or 64 bytes, got {}",
                raw.len()
            )));
        }
        let mut seed = [0u8; 32];
        seed.copy_from_slice(&raw[..32]);
        let keypair = Self::from_seed(&seed);
        if raw.len() == 64 && raw[32..] != keypair.pubkey {
            return Err(ChainError::validation(
                "secret key tail does not match its public key (corrupt Phantom key)",
            ));
        }
        Ok(keypair)
    }

    pub fn from_mnemonic(phrase: &str, path: &str) -> ChainResult<Self> {
        let mnemonic = Mnemonic::parse_in(Language::English, phrase.trim())
            .map_err(|e| ChainError::validation(format!("invalid mnemonic: {e}")))?;
        let seed = mnemonic.to_seed("");
        let private = slip10_ed25519(&seed, path)?;
        Ok(Self::from_seed(&private))
    }

    pub fn generate(word_count: usize, path: &str) -> ChainResult<(Self, String)> {
        let mnemonic = Mnemonic::generate_in(Language::English, word_count)
            .map_err(|e| ChainError::new("CRYPTO_ERROR", e.to_string()))?;
        let phrase = mnemonic.to_string();
        let seed = mnemonic.to_seed("");
        let private = slip10_ed25519(&seed, path)?;
        Ok((Self::from_seed(&private), phrase))
    }

    pub fn pubkey_base58(&self) -> String {
        bs58::encode(self.pubkey).into_string()
    }

    pub fn address(&self) -> String {
        address_from_pubkey(&self.pubkey)
    }

    /// 64 bytes = seed || pubkey, base58 — the Phantom private-key format.
    pub fn secret_base58(&self) -> String {
        let mut raw = Vec::with_capacity(64);
        raw.extend_from_slice(self.signing.as_bytes());
        raw.extend_from_slice(&self.pubkey);
        bs58::encode(raw).into_string()
    }

    pub fn sign_base58(&self, message: &[u8]) -> String {
        let signature: Signature = self.signing.sign(message);
        bs58::encode(signature.to_bytes()).into_string()
    }

    pub fn wallet_file(&self, mnemonic: Option<String>) -> WalletFile {
        WalletFile {
            address: self.address(),
            pubkey_base58: self.pubkey_base58(),
            secret_key_base58: self.secret_base58(),
            mnemonic,
        }
    }
}

pub fn verify_signature(pubkey_b58: &str, message: &[u8], signature_b58: &str) -> ChainResult<()> {
    let raw_pub = bs58::decode(pubkey_b58.trim())
        .into_vec()
        .map_err(|e| ChainError::validation(format!("pubkey is not base58: {e}")))?;
    if raw_pub.len() != 32 {
        return Err(ChainError::validation("pubkey must be 32 bytes"));
    }
    let mut pub_bytes = [0u8; 32];
    pub_bytes.copy_from_slice(&raw_pub);
    let verifying = VerifyingKey::from_bytes(&pub_bytes)
        .map_err(|e| ChainError::validation(format!("pubkey is not a valid ed25519 key: {e}")))?;

    let raw_sig = bs58::decode(signature_b58.trim())
        .into_vec()
        .map_err(|e| ChainError::validation(format!("signature is not base58: {e}")))?;
    if raw_sig.len() != 64 {
        return Err(ChainError::validation("signature must be 64 bytes"));
    }
    let signature = Signature::from_slice(&raw_sig)
        .map_err(|e| ChainError::validation(format!("bad signature: {e}")))?;
    verifying.verify(message, &signature).map_err(|_| {
        ChainError::new("INVALID_SIGNATURE", "signature does not verify for this pubkey")
    })
}

pub fn load_wallet(path: &str) -> ChainResult<WalletFile> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| ChainError::io(format!("cannot read wallet {path}: {e}")))?;
    serde_json::from_str(&text)
        .map_err(|e| ChainError::json(format!("wallet {path} is not valid JSON: {e}")))
}

