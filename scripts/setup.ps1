$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
Push-Location (Join-Path $PSScriptRoot "..")
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
    }
    & uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed" }
    & uv run --locked boss-local-mcp install-browser
    if ($LASTEXITCODE -ne 0) { throw "Chrome installation failed" }
    & uv run --locked boss-local-mcp doctor
    if ($LASTEXITCODE -ne 0) { throw "Environment inspection failed" }
}
finally {
    Pop-Location
}
