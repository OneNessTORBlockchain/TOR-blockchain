# Install the Rust toolchain (rustup) and build the torchain ledger.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\install_rust.ps1
$ErrorActionPreference = "Stop"

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Rust via winget (Rustlang.Rustup)..."
    winget install --id Rustlang.Rustup -e --silent `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
    $env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
}

cargo --version

$crate = Join-Path $PSScriptRoot "..\rust\torchain"
Push-Location $crate
try {
    cargo build --release
} finally {
    Pop-Location
}

Write-Host "built: rust\torchain\target\release\torchain.exe"
