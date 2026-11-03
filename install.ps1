# Job Radar installer for Windows (PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/piotr-malek/jobradar-eu/main/install.ps1 | iex"
# Installs uv (Astral's Python tool manager, which brings its own Python) if it's missing, then Job Radar,
# then opens the setup wizard in your browser. Nothing needs admin rights.
$ErrorActionPreference = "Stop"
$spec = if ($env:JOBRADAR_SPEC) { $env:JOBRADAR_SPEC } else { "jobradar-eu[full]" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> Installing uv (it installs Python for Job Radar)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "==> Installing Job Radar..."
uv tool install --upgrade --python 3.13 $spec
uv tool update-shell | Out-Null

$jr = Join-Path (uv tool dir --bin) "jr.exe"
Write-Host "==> Opening the setup in your browser. Keep this window open while you use Job Radar."
& $jr setup
