# cswap one-line installer (Windows PowerShell).  NOT TESTED on Windows yet.
#
#   irm https://raw.githubusercontent.com/saurabh-awate96/claude-swap/main/install.ps1 | iex
#
# Installs uv if missing, installs cswap with it, then opens `cswap setup`.
# Optional: $env:CSWAP_SPEC (path / git+https URL / PyPI name), $env:CSWAP_NO_SETUP.
$ErrorActionPreference = "Stop"

$Spec = if ($env:CSWAP_SPEC) { $env:CSWAP_SPEC } else { "git+https://github.com/saurabh-awate96/claude-swap.git" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (the Python tool installer) from astral.sh"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "Installing cswap from $Spec"
uv tool install --force --from $Spec claude-swap
uv tool update-shell | Out-Null

if (-not $env:CSWAP_NO_SETUP) { cswap setup } else { Write-Host "Next: cswap setup" }
