# OneNess TOR — the same start command as Render and as a local checkout:
#     docker build -t oneness-tor .
#     docker run -p 8090:8090 oneness-tor
# `python scripts/start.py` binds 0.0.0.0:$PORT, starts the Rust ledger on
# 127.0.0.1:8545 (node.autostart) and then serves the API — one container, one
# command, no second service to forget.

# ---- the ledger: compiled here so the runtime image carries a real node ----
# Rust ≥ 1.85: the lockfile pulls zeroize 1.9, whose manifest needs edition2024.
FROM rust:1.85-bookworm AS ledger
WORKDIR /build
COPY rust/torchain/Cargo.toml rust/torchain/Cargo.lock ./
COPY rust/torchain/src ./src
RUN cargo build --release --locked

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ONENESS_API_HOST=0.0.0.0 \
    ONENESS_OLLAMA_AUTO_INSTALL=1 \
    ONENESS_OLLAMA_PULL=0 \
    ONENESS_NODE_API=http://127.0.0.1:8545 \
    ONENESS_NODE_AUTOSTART=1 \
    PORT=8090

# curl + tar (+ zstd) are what the Ollama installer needs; ca-certificates lets
# the token-logo metadata reach CoinGecko/DexScreener. `tor` is what makes this
# container browsable as an .onion service: the start command writes a torrc
# (SOCKS 9050 + ControlPort 9051, cookie auth) and runs the daemon itself
# (ONENESS_TOR_AUTOSTART=1), then ADD_ONIONs the API (ONENESS_TOR_PUBLISH_NODE=1).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tar zstd tor \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# `torchain` lands on PATH, so core.ledger_host finds it without a config edit;
# seed/chain-snapshot.json (if present) gives a fresh container the chain to
# serve instead of an empty 503.
COPY --from=ledger /build/target/release/torchain /usr/local/bin/torchain
RUN chmod +x /usr/local/bin/torchain

EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/api/health', timeout=4)"

# exactly the Render start command
CMD ["python", "scripts/start.py"]
