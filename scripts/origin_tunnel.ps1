# Publish this machine (the proposing node) so Render can reach it.
#
#   powershell -ExecutionPolicy Bypass -File scripts/origin_tunnel.ps1
#
# Starts a Cloudflare quick tunnel to the local API, writes the new hostname into
# config.fleet.yaml, then verifies that the origin really serves its ledger
# snapshot over that URL. A quick tunnel gets a NEW hostname on every restart, so
# run this again after a reboot and commit the changed line:
#
#   git add config.fleet.yaml && git commit -m "fleet: origin url" && git push
#
# Keep the window (and cloudflared) running: while it is down, every replica
# reports SYNC_ORIGIN_UNREACHABLE and keeps serving the blocks it already has.
#
# -Commit also commits + pushes the new URL, so Render redeploys the replicas on
# its own:  powershell -ExecutionPolicy Bypass -File scripts/origin_tunnel.ps1 -Commit
param(
    [int]$Port = 8090,
    [string]$Cloudflared = "$env:USERPROFILE\Downloads\cloudflared.exe",
    [int]$HealthWait = 300,
    [switch]$Commit
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
# a unique log per run: a previous cloudflared may still hold the old one open,
# and Select-String would then "find" that tunnel's dead URL
$log = Join-Path $env:TEMP ("oneness-origin-tunnel-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + ".log")

if (-not (Test-Path $Cloudflared)) {
    Write-Host "downloading cloudflared -> $Cloudflared"
    Invoke-WebRequest -UseBasicParsing `
        -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' `
        -OutFile $Cloudflared
}

# Publishing a URL for a node that is not serving would only teach Render to 502:
# wait for the API (a logon task races the operator's own start command).
Write-Host "waiting  : http://127.0.0.1:$Port/api/health"
$ready = $false
$tries = [Math]::Max(1, [int]($HealthWait / 5))
for ($attempt = 0; $attempt -lt $tries; $attempt++) {
    try {
        $null = Invoke-WebRequest -UseBasicParsing `
            -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 5
        $ready = $true
        break
    } catch {
        Start-Sleep -Seconds 5
    }
}
if (-not $ready) {
    throw "no API on 127.0.0.1:$Port after ${HealthWait}s - start it first (python scripts/start.py)"
}

Start-Process -FilePath $Cloudflared -WindowStyle Hidden -ArgumentList @(
    'tunnel', '--url', "http://127.0.0.1:$Port", '--no-autoupdate',
    '--logfile', $log, '--loglevel', 'info')

$url = $null
for ($attempt = 0; $attempt -lt 45 -and -not $url; $attempt++) {
    Start-Sleep -Seconds 2
    if (Test-Path $log) {
        $hit = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
            Select-Object -First 1
        if ($hit) { $url = $hit.Matches[0].Value }
    }
}
if (-not $url) { throw "cloudflared never published a URL - see $log" }

Write-Host "tunnel   : $url"
python (Join-Path $root 'scripts\fleet_origin.py') --set-url $url --verify
if ($LASTEXITCODE -ne 0) { throw "the tunnel did not serve the ledger snapshot" }

if ($Commit) {
    Push-Location $root
    try {
        git add config.fleet.yaml .env
        git commit -m "fleet: origin url $url"
        git push origin main
    } finally {
        Pop-Location
    }
}
