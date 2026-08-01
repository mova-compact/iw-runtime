param(
    [switch]$SkipDockerCheck
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

$Version = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $Version -notin @("3.11", "3.12")) {
    throw "Python 3.11 or 3.12 is required. Found: $Version"
}

if (-not $SkipDockerCheck) {
    & docker info --format "Docker {{.ServerVersion}}" | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop is required and must be running."
    }
}

if (-not (Test-Path -LiteralPath ".venv")) {
    & python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}

Write-Host ""
Write-Host "Installation complete."
Write-Host "1. Edit .env and choose your provider/model."
Write-Host "2. Store the API key: .\.venv\Scripts\python.exe -m runtime.secret_cli set llm_api_key"
Write-Host "3. Check readiness: .\.venv\Scripts\python.exe -m runtime.cli doctor"
Write-Host "4. Run the sample: .\.venv\Scripts\python.exe examples\example_run.py"
