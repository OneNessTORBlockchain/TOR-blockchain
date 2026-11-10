# OneNess TOR — start the ledger + the API in two background processes.
# Usage:  .\scripts\run_all.ps1 [-ChainDir data\chain-api] [-AdminToken oneness-dev-token]
param(
    [string]$ChainDir = "data\chain-api",
    [string]$AdminToken = "oneness-dev-token",
    [string]$NodeBind = "127.0.0.1:8545",
    [int]$ApiPort = 8090
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$binary = Join-Path $root "rust\torchain\target\release\torchain.exe"
if (-not (Test-Path $binary)) { throw "build the ledger first: cd rust/torchain; cargo build --release" }
if (-not (Test-Path (Join-Path $root "$ChainDir\state.json"))) {
    Write-Host "no chain in $ChainDir — running genesis (50,000,000 TOR)"
    & $binary init --founder "$root\data\wallets\founder.json" --dir $ChainDir --admin-token $AdminToken
}

Write-Host "starting torchain on $NodeBind (chain $ChainDir)"
Start-Process -FilePath $binary -ArgumentList "serve", "--bind", $NodeBind, "--dir", $ChainDir `
    -RedirectStandardOutput "$root\_node_api.log" -RedirectStandardError "$root\_node_api.err.log" `
    -WindowStyle Hidden

$env:ONENESS_CHAIN_DIR = $ChainDir
$env:ONENESS_NODE_API = "http://$NodeBind"
$env:ONENESS_ADMIN_TOKEN = $AdminToken
$env:ONENESS_PUBLIC_BASE_URL = "http://127.0.0.1:$ApiPort"

Write-Host "starting the API on http://127.0.0.1:$ApiPort (the same start command as Render/Docker)"
Start-Process -FilePath "python" -ArgumentList "scripts/start.py", "--port", "$ApiPort" `
    -RedirectStandardOutput "$root\_api.log" -RedirectStandardError "$root\_api.err.log" `
    -WindowStyle Hidden


Start-Sleep -Seconds 3
Write-Host ""
Write-Host "  pages      http://127.0.0.1:$ApiPort/"
Write-Host "  dashboard  http://127.0.0.1:$ApiPort/dashboard"
Write-Host "  docs       http://127.0.0.1:$ApiPort/docs"
Write-Host "  api        http://127.0.0.1:$ApiPort/api/health"
Write-Host "  token      $AdminToken"
Write-Host ""
Write-Host "smoke test: python tools/live_check.py --spend"
Write-Host "logs: _node_api.log / _node_api.err.log / _api.log / _api.err.log"
