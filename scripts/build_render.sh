#!/usr/bin/env bash
# Render build step for a *native Python* service: install deps, then compile the
# Rust ledger the API is supposed to read.
#
#     Render dashboard → your service → Settings → Build & Deploy
#       Build Command:  bash scripts/build_render.sh
#       Start Command:  python scripts/start.py
#
# A Render Docker service does this in the Dockerfile and does not need this
# script. A native Python service does: without a `torchain` binary the API is
# up, `/api/health` says "degraded", and every chain route answers
# 503 CHAIN_UNAVAILABLE — which is exactly what the public site was doing.
#
# The binary lands where `chain.binary` already points
# (rust/torchain/target/release/torchain), so core.ledger_host finds it with no
# configuration; seed/chain-snapshot.json then gives it the real chain.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v cargo >/dev/null 2>&1; then
  echo "--- installing the Rust toolchain (minimal profile) ---"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
  # shellcheck disable=SC1091
  . "$HOME/.cargo/env"
fi
export PATH="$HOME/.cargo/bin:$PATH"

echo "--- building torchain (release) ---"
cargo build --release --locked --manifest-path rust/torchain/Cargo.toml

echo "--- python dependencies ---"
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

echo "--- ledger binary ---"
ls -l rust/torchain/target/release/torchain
