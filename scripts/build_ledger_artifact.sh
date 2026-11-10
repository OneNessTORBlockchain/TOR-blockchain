#!/usr/bin/env bash
# Rebuild the *committed Linux ledger artifact* (`bin/torchain`).
#
# Why this file exists: a Render **native Python** service (no Dockerfile) has no
# Rust toolchain at build time unless its Build Command runs
# `scripts/build_render.sh`. When it does not, `core.ledger_host.find_binary()`
# falls back to the prebuilt Linux binary committed in `bin/` — and that file is
# only as new as the last person who rebuilt it. On 9 Nov the public host was
# running a `bin/torchain` from 6 Nov: it answered `/api/chain/info` and
# `/api/payments/plans` but every `/api/nft…` and `/api/account/<a>/holdings`
# door came back `404 no route for GET /api/nft`, because the *node* behind the
# (already deployed) Python API was older than the NFT release.
#
# So: rebuild it here, on a machine with Docker, and commit the result. The
# builder is Debian bullseye (glibc 2.31) on purpose — the artifact must run on
# Render's Ubuntu 22.04 (glibc 2.35) and on older hosts, and a bookworm build
# would need GLIBC_2.36. Rust 1.85 is the floor: `zeroize` in the lockfile uses
# edition2024.
#
#     bash scripts/build_ledger_artifact.sh          # writes bin/torchain
#     bash scripts/build_ledger_artifact.sh --check  # verify, do not write
#
# The better fix where the service allows it: set the Build Command to
# `bash scripts/build_render.sh` so the node is compiled from the same commit as
# the API every deploy, and this artifact is only the fallback.
set -euo pipefail
cd "$(dirname "$0")/.."

BUILDER="${ONENESS_RUST_IMAGE:-rust:1.85-bullseye}"
OUT="${TMPDIR:-/tmp}/torchain-artifact"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required to cross-build the Linux artifact" >&2
  exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT"
echo "--- building rust/torchain in $BUILDER (release, locked) ---"
docker run --rm \
  -v "$PWD/rust/torchain:/src" \
  -v "$OUT:/out" \
  -w /src -e CARGO_TARGET_DIR=/out \
  "$BUILDER" cargo build --release --locked

BINARY="$OUT/release/torchain"
[ -f "$BINARY" ] || { echo "the build produced no binary" >&2; exit 1; }

echo "--- verifying the artifact ---"
python3 - "$BINARY" <<'PY'
import pathlib, re, sys

path = pathlib.Path(sys.argv[1])
blob = path.read_bytes()
if blob[:4] != b"\x7fELF":
    raise SystemExit(f"{path} is not a Linux ELF binary")
missing = [word.decode() for word in (b"nft_transfer", b"token_create")
           if word not in blob]
if missing:
    raise SystemExit(f"{path} is older than this source tree: missing {missing}")


def version(text):
    parts = [int(p) for p in text.split("_", 1)[1].split(".") if p.isdigit()]
    return tuple(parts + [0] * (3 - len(parts)))


symbols = {m.decode() for m in re.findall(rb"GLIBC_[0-9.]+", blob)}
highest = max(symbols, key=version) if symbols else "static"
print(f"elf ok · {len(blob)} bytes · highest glibc symbol {highest}")
if symbols and version(highest) > (2, 28, 0):
    print(f"WARNING: {highest} — a host older than Debian 10 may refuse to run this")
PY

if [ "$CHECK_ONLY" = "1" ]; then
  echo "--- check only: bin/torchain untouched ($BINARY) ---"
  exit 0
fi

cp "$BINARY" bin/torchain
chmod +x bin/torchain
echo "--- bin/torchain replaced ---"
ls -l bin/torchain
