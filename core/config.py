"""Configuration loader â€” ``config.yaml`` at the project root + env overrides.

Dotted lookups keep callers readable (``settings.get("meme.promo_codes")``)
while a single file stays the source of truth. Env vars always win, which is
what CI and the two-node test harness use.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

# env var -> dotted config path (str/int/float/bool converted on read)
_ENV_MAP = {
    "ONENESS_NODE_API": "node.api_url",
    "ONENESS_NODE_AUTOSTART": "node.autostart",
    "ONENESS_NODE_BINARY": "node.binary",
    "ONENESS_NODE_SEED": "node.seed",
    "ONENESS_NODE_START_TIMEOUT": "node.start_timeout",
    "ONENESS_ADMIN_TOKEN": "api.admin_token",
    "ONENESS_API_HOST": "api.host",
    "ONENESS_API_PORT": "api.port",
    "ONENESS_DATA_DIR": "paths.data_dir",
    "ONENESS_CHAIN_DIR": "chain.data_dir",
    "ONENESS_OLLAMA_URL": "ollama.base_url",
    "ONENESS_OLLAMA_API_KEY": "ollama.api_key",
    "ONENESS_OLLAMA_MODEL": "ollama.model",
    "ONENESS_OLLAMA_AUTO_INSTALL": "ollama.auto_install",
    "ONENESS_OLLAMA_PULL": "ollama.pull_on_start",
    "ONENESS_TOR_CONTROL_PORT": "tor.control_port",
    "ONENESS_TOR_SOCKS_PORT": "tor.socks_port",
    "ONENESS_PUBLIC_BASE_URL": "paths.public_base_url",
    # per-transaction privacy: the wallet picks PUBLIC or ANONYMOUS per send
    "ONENESS_PRIVACY_DEFAULT": "privacy.default_mode",
    "ONENESS_PRIVACY_LABEL": "privacy.anonymous_label",
    "ONENESS_SOLANA_RPC": "nft.solana_rpc",
    "ONENESS_TOKEN_IMAGES": "tokens.images.enabled",
    "ONENESS_TOKEN_IMAGES": "tokens.images.enabled",
    "ONENESS_UNIVERSE": "tokens.universe.enabled",
    "ONENESS_NODE_ROLE": "sync.role",
    "ONENESS_SYNC_ORIGIN": "sync.origin_url",
    "ONENESS_SYNC_TOKEN": "sync.origin_token",
    "ONENESS_SYNC_AUTO": "sync.auto",
    "ONENESS_SYNC_INTERVAL": "sync.interval_seconds",
    "ONENESS_SYNC_PROXY": "sync.proxy",
    # origin → replica push (the automatism the admin panel's "push now" shares)
    "ONENESS_SYNC_PUSH": "sync.push.enabled",
    "ONENESS_SYNC_PUSH_TARGETS": "sync.push.targets",
    "ONENESS_SYNC_PUSH_INTERVAL": "sync.push.interval_seconds",
    "ONENESS_SYNC_PUSH_KEEP": "sync.push.keep",
    "ONENESS_SYNC_PUSH_SAVE": "sync.push.save",
    "ONENESS_TOR_PUBLISH_NODE": "tor.publish_node",
    # a node that is not on a Tor-installed host can still be an onion: when
    # auto_start is on the start command runs the bundled/system `tor` itself
    "ONENESS_TOR_AUTOSTART": "tor.auto_start",
    # a container without the package (a service created from a Python runtime
    # instead of this repository's Dockerfile) installs it at boot when allowed
    "ONENESS_TOR_AUTO_INSTALL": "tor.auto_install",
    # base64 ED25519-V3 key: re-creating the *same* .onion after a redeploy (a
    # Render container's disk is ephemeral, so the key cannot live there alone)
    "ONENESS_TOR_ONION_KEY": "tor.onion_key",
    "ONENESS_TOR_BINARY": "tor.binary",
    # IPFS / Pinata (meme launch metadata, AI-NFT metadata, listing images)
    "PINATA_JWT": "ipfs.pinata.jwt",
    "PINATA_API_KEY": "ipfs.pinata.api_key",
    "PINATA_API_SECRET": "ipfs.pinata.secret",
    "PINATA_SECRET_API_KEY": "ipfs.pinata.secret",
    "IPFS_GATEWAY": "ipfs.gateway",
    "ONENESS_IPFS_GATEWAY": "ipfs.gateway",
    "ONENESS_IPFS_PIN": "ipfs.pinata.enabled",
    # developer API keys
    "ONENESS_REQUIRE_API_KEY": "api.require_key",
    "ONENESS_API_KEY_PER_MINUTE": "dev_keys.rate_per_minute",
    "ONENESS_API_KEY_PER_DAY": "dev_keys.rate_per_day",
    # OpenRouter â€” the AI-NFT ChatBox on every TOR platform speaks this gateway
    "ONENESS_OPENROUTER_API_KEY": "openrouter.api_key",
    "OPENROUTER_API_KEY": "openrouter.api_key",
    "ONENESS_OPENROUTER_BASE": "openrouter.base_url",
    "ONENESS_OPENROUTER_MODEL": "openrouter.model",
    # the price engine's TOR anchor (USD) â€” every token price derives from it
    "ONENESS_TOR_USD": "pricing.tor_usd",
}


_DEFAULTS: dict[str, Any] = {
    "chain.chain_id": "072316",
    "chain.native_coin_id": "TOR111111111111111111111111111111onion",
    # the Rust ledger (rust/torchain) â€” authoritative balances & permanent ledger
    "chain.data_dir": "data/chain",
    "chain.founder_wallet": "data/wallets/founder.json",
    "chain.founder_allocation": "data/genesis/founder_allocation.json",
    "chain.binary": "rust/torchain/target/release/torchain",
    "node.api_url": "http://127.0.0.1:8545",
    "node.timeout": 10.0,
    # The start command hosts the ledger itself when node.api_url is loopback
    # (Render/Docker/VPS): no second service, no "node down" on every route.
    # A remote node.api_url is never touched, and autostart=0 turns it off.
    "node.autostart": True,
    "node.binary": "",
    # a verified snapshot a fresh container adopts before the node starts
    "node.seed": "",
    "node.start_timeout": 25.0,
    "api.host": "127.0.0.1",
    "api.port": 8090,
    "api.admin_token": "change-me",
    # TOR is quoted in USD by the price engine. The anchor is the launch price
    # ($3.60); the engine drifts it with real pool turnover, clamped to Â±15%.
    "pricing.tor_usd": 3.60,
    # the price & market-cap engine (Solarix dashboard, screener, every app)
    "market.cache_seconds": 15.0,
    "market.sample_seconds": 60.0,
    "market.history_points": 2000,
    "market.chart_points": 168,
    # OpenRouter â€” free models only; the key never reaches a browser
    "openrouter.base_url": "https://openrouter.ai/api/v1",
    "openrouter.api_key": "",
    "openrouter.model": "openrouter/free",
    "openrouter.timeout": 90.0,
    "openrouter.referer": "https://www.onenesstorblockchain.com",
    "openrouter.title": "TOR AI-NFT Wallet",
    "api.cors_origins": ["http://127.0.0.1:8080", "http://localhost:8080"],
    # Set to true only when the API is served from its own host (no browser
    # pages): every /api call then needs x-api-key, an admin credential or a
    # wallet session. The developer surface /api/v1 always needs a key.
    "api.require_key": False,
    "dev_keys.rate_per_minute": 300,
    "dev_keys.rate_per_day": 50000,
    "dev_keys.default_days": 30,
    "dev_keys.max_days": 365,

    "paths.data_dir": "data",
    "paths.public_base_url": "http://127.0.0.1:8080",
    # per-transaction privacy (oneness_tor.services.privacy): the wallet picks
    # PUBLIC (its address is on chain) or ANONYMOUS (the label is) per send
    "privacy.default_mode": "public",
    "privacy.anonymous_label": "Tor-Anonymous-onion",
    "ollama.base_url": "http://127.0.0.1:11434",
    # a hosted Ollama (ollama.com, a tunnel) authenticates with this bearer
    # token; OLLAMA_API_KEY is honoured too, and neither is ever rendered
    "ollama.api_key": "",
    "ollama.model": "qwen2.5:7b-instruct",
    "ollama.models": {},
    "ollama.timeout": 180.0,
    "ollama.landing_models": [],
    # the start command installs + serves Ollama when it is missing; pulling the
    # default model is opt-in because a 7B download is several GB
    "ollama.auto_install": False,
    "ollama.pull_on_start": False,
    "ollama.start_timeout": 45.0,
    # token logos: public metadata (CoinGecko, DexScreener) cached on this node
    "tokens.images.enabled": True,
    "tokens.images.ttl_hours": 24.0,
    "tokens.images.per_page": 250,
    "tokens.images.timeout": 8.0,
    "tokens.images.retry_seconds": 600.0,
    "tokens.images.dexscreener": True,
    # every logo is copied onto this node (data/metadata/token-logos) and served
    # from /assets/tokens/, so no page depends on a third party being reachable
    "tokens.images.mirror": True,
    "tokens.images.mirror_limit": 500,
    "tokens.images.mirror_max_bytes": 2_000_000,


    # the 120-asset market universe (top 100 by market cap + top 20 Solana
    # memecoins): key-less public providers with two fallbacks each, cached here
    "tokens.universe.enabled": True,
    "tokens.universe.ttl_hours": 6.0,
    "tokens.universe.timeout": 20.0,
    "tokens.universe.retry_seconds": 300.0,
    "tor.control_port": 9051,

    "tor.socks_port": 9050,
    "tor.hostname": "127.0.0.1",
    "tor.publish_node": False,
    "tor.node_label": "node",
    # Start Tor ourselves when the control port is closed: on Render/Docker the
    # image installs `tor`, and a daemon nobody started is the one reason an
    # onion deployment "just doesn't work". OFF locally unless asked for.
    "tor.auto_start": False,
    # when the binary is missing too, install it with the host's package manager
    # (apt/apk/dnf/yum/brew) instead of only naming the fix in the log
    "tor.auto_install": False,
    "tor.binary": "tor",
    "tor.onion_key": "",
    "tor.torrc": "",
    "tor.start_timeout": 25.0,
    # origin → replicas: export the verified snapshot, POST it to every target,
    # and keep a copy on disk. A replica still re-hashes everything it receives.
    "sync.push.enabled": False,
    "sync.push.targets": [],
    "sync.push.interval_seconds": 0,        # 0 = follow sync.interval_seconds
    "sync.push.keep": 5,                    # snapshot files kept in data/snapshots
    "sync.push.save": True,
    # one proposer + verified replicas (snapshot pull, never gossip)
    "sync.role": "origin",
    "sync.origin_url": "",
    "sync.origin_token": "",
    "sync.auto": True,
    "sync.interval_seconds": 60,
    "sync.timeout": 30.0,
    "sync.proxy": "",
    "pricing.overrides": {},
    "meme.page_path": "/pages/meme.html",
    "meme.promo_codes": "",
    "nft.page_path": "/pages/ai-nft.html",
    "nft.max_metadata_bytes": 262144,
    "nft.ipfs_gateways": ["https://ipfs.io/ipfs/", "https://cloudflare-ipfs.com/ipfs/"],
    "nft.solana_rpc": "https://api.mainnet-beta.solana.com",
    "nft.timeout": 10.0,
    # IPFS: Pinata pins the permanent public metadata of meme tokens and
    # AI-NFTs; the gateway is what pages and listings load images through.
    # Credentials live in .env at the repo root (tracked: this repo is private)
    # or in the environment, which always wins.
    "ipfs.gateway": "https://ipfs.io/ipfs/",
    "ipfs.gateway_token": "",
    "ipfs.pinata.enabled": True,
    "ipfs.pinata.jwt": "",
    "ipfs.pinata.api_key": "",
    "ipfs.pinata.secret": "",
    "ipfs.pinata.timeout": 30.0,
    "ipfs.pinata.json_url": "https://api.pinata.cloud/pinning/pinJSONToIPFS",
    "ipfs.pinata.file_url": "https://api.pinata.cloud/pinning/pinFileToIPFS",
}


def load_dotenv(path: Path = ROOT / ".env") -> int:
    """Load ``KEY=VALUE`` lines into ``os.environ`` without overriding real env.

    A tiny parser on purpose: the project ships no ``python-dotenv``. The file is
    committed here because the repository is private and single-operator; real
    environment variables still win. Returns how many keys were set, and never
    prints a value.
    """
    if not path.exists():
        return 0
    applied = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        if text.lower().startswith("export "):
            text = text[7:].strip()
        key, _, value = text.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key or not key.replace("_", "").isalnum():
            continue
        if os.environ.get(key):
            continue          # a real environment variable always wins
        os.environ[key] = value
        applied += 1
    return applied


load_dotenv()


def _coerce(value: Any, like: Any) -> Any:
    if isinstance(like, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int) and not isinstance(like, bool):
        return int(float(value))
    if isinstance(like, float):
        return float(value)
    if isinstance(like, list):
        return [part.strip() for part in str(value).split(",") if part.strip()]
    return value


def _load_raw() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{CONFIG_PATH} must contain a YAML mapping")
    return loaded


class Settings:
    """Dotted-path settings view over the YAML file, with env overrides."""

    def __init__(self, raw: dict | None = None) -> None:
        self.raw: dict = raw if raw is not None else _load_raw()

    # -- reading ---------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is None:
            node = _DEFAULTS.get(path, default)
        for env, dotted in _ENV_MAP.items():
            if dotted == path and os.environ.get(env) not in (None, ""):
                return _coerce(os.environ[env], node)
        return node

    # -- convenience -----------------------------------------------------
    @property
    def node_api_url(self) -> str:
        return str(self.get("node.api_url")).rstrip("/")

    @property
    def node_timeout(self) -> float:
        return float(self.get("node.timeout"))

    @property
    def admin_token(self) -> str:
        """The token this deployment presents to its own ledger.

        ``ONENESS_ADMIN_TOKEN`` / ``api.admin_token`` when the operator set one;
        otherwise the token the node created for itself at boot and keeps in
        ``data/admin/chain_token`` (see ``oneness_tor.services.chain_token``), so
        no panel ever asks for a token ``torchain init`` printed once.
        """
        text = str(self.get("api.admin_token") or "").strip()
        if text and text != "change-me":
            return text
        try:
            stored = (self.data_dir / "admin" / "chain_token").read_text(
                encoding="utf-8").strip()
        except OSError:
            stored = ""
        return stored or text

    @property
    def ollama_url(self) -> str:
        return str(self.get("ollama.base_url")).rstrip("/")

    @property
    def ollama_model(self) -> str:
        return str(self.get("ollama.model"))

    @property
    def data_dir(self) -> Path:
        raw = Path(str(self.get("paths.data_dir")))
        path = raw if raw.is_absolute() else ROOT / raw
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def chain_dir(self) -> Path:
        """Directory of the Rust ledger (state.json + blocks.jsonl)."""
        raw = Path(str(self.get("chain.data_dir")))
        return raw if raw.is_absolute() else ROOT / raw

    @property
    def founder_wallet_path(self) -> Path:
        raw = Path(str(self.get("chain.founder_wallet")))
        return raw if raw.is_absolute() else ROOT / raw

    @property
    def chain_binary(self) -> Path:
        """Path to the torchain binary (``.exe`` appended on Windows)."""
        raw = Path(str(self.get("chain.binary")))
        base = raw if raw.is_absolute() else ROOT / raw
        if os.name == "nt" and base.suffix != ".exe":
            return base.with_suffix(".exe")
        return base


settings = Settings()
