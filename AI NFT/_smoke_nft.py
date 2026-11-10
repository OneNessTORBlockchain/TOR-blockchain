"""One-off live smoke for the NFT studio endpoints (temp DB, real OpenRouter).

Run once from the wallet root:  python _smoke_nft.py
"""
import os
import sys
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "smoke_nft.db")
os.environ["SESSION_SECRET"] = os.environ.get("SESSION_SECRET", "x" * 48)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402
from app.api import app  # noqa: E402

c = TestClient(app)
TOK = ""

print("healthz:", c.get("/api/healthz").status_code)
r = c.post("/api/auth/register", json={"username": "nftsmoke", "password": "Str0ngPass!x1"})
print("register:", r.status_code)
l = c.post("/api/auth/login", json={"username": "nftsmoke", "password": "Str0ngPass!x1"})
print("login:", l.status_code)
TOK = l.json().get("token", "")
H = {"Authorization": f"Bearer {TOK}"}

# static page served?
p = c.get("/nft.html")
print("nft.html:", p.status_code, len(p.content), "bytes")

# OpenRouter free model catalog (live)
m = c.get("/api/nft/ai/models", headers=H)
body = m.json()
models = body.get("models", [])
print("models:", m.status_code, "| configured:", body.get("configured"), "| count:", len(models))
if models:
    print("  sample:", [x["id"] for x in models[:5]])

# NFT bundle for a real token: WSOL is the native mint (supply 0 by design) →
# the chatbox correctly rejects it; USDC has real supply → loads; a famous NFT
# mint gets the full Metaplex + off-chain JSON treatment.
wsol = "So11111111111111111111111111111111111111112"
usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
b = c.get(f"/api/nft/{wsol}", headers=H)
print("bundle WSOL (expect 404 native mint):", b.status_code,
      str((b.json() or {}).get("detail", ""))[:60])
b = c.get(f"/api/nft/{usdc}", headers=H)
d2 = b.json() or {}
print("bundle USDC:", b.status_code, "| source:", d2.get("source"),
      "| name:", d2.get("name", ""), "| is_nft:", d2.get("is_nft"))

CHAT_MINT = usdc  # supply-backed token mint that definitely resolves
for candidate in ("9ARngkVDhr45GN3R2eT6DgsdcKcmYutGqi1Y1FV4MDkC",):  # famous 1/1 mint
    bn = c.get(f"/api/nft/{candidate}", headers=H)
    bj = bn.json() or {}
    print("bundle candidate:", candidate[:8], bn.status_code,
          "| is_nft:", bj.get("is_nft"), "| name:", bj.get("name", "")[:36],
          "| image:", str(bj.get("image", ""))[:60])
    if bn.status_code == 200 and bj.get("image"):
        CHAT_MINT = candidate  # prefer a real NFT for the persona prompt

# invalid mint → 404
bad = "1" * 31 + "z"
resp = c.get(f"/api/nft/{bad}", headers=H)
print("bad mint:", resp.status_code, str((resp.json() or {}).get("detail", ""))[:70])

# live chat turn — proves the API key + 'You are <description>' injection path
if models:
    chat = c.post("/api/nft/ai/chat", headers=H, json={
        "mint": CHAT_MINT, "token_id": "1", "model": "",
        "messages": [{"role": "user", "content": "In one sentence: who are you?"}],
    })
    d = chat.json() or {}
    print("chat:", chat.status_code, "| model:", d.get("model"))
    print("  reply:", str(d.get("content"))[:220])

# chat with a paid model must be rejected (free-only enforcement)
if models:
    paid = c.post("/api/nft/ai/chat", headers=H, json={
        "mint": wsol, "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
    })
    print("paid-model chat:", paid.status_code, str((paid.json() or {}).get("detail"))[:80])

# no auth → 401
print("unauth models:", c.get("/api/nft/ai/models").status_code)