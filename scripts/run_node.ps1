# Start the OneNess TOR ledger node (Rust) on the API port.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\run_node.ps1
param(
    [string]$Bind = "127.0.0.1:8545",
    [string]$ChainDir = "data\chain"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root "rust\torchain\target\release\torchain.exe"
if (-not (Test-Path $bin)) {
    throw "torchain is not built yet - run scripts\install_rust.ps1 first"
}
$dir = if ([System.IO.Path]::IsPathRooted($ChainDir)) { $ChainDir } else { Join-Path $root $ChainDir }
& $bin serve --dir $dir --bind $Bind
