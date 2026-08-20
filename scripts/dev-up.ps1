$ErrorActionPreference = "Stop"
$ProjectDirectory = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDirectory

if (-not (Test-Path ".env.local")) {
    Copy-Item ".env.example" ".env.local"
    Write-Host "Created .env.local from safe local-development defaults."
}

docker compose --env-file .env.local up -d --build
$Url = "http://127.0.0.1:8080"
for ($Attempt = 0; $Attempt -lt 120; $Attempt++) {
    try {
        Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing | Out-Null
        Start-Process $Url
        Write-Host "Setup Wizard: $Url"
        exit 0
    } catch {
        Start-Sleep -Seconds 2
    }
}
throw "The Web/API health endpoint did not become available. Run docker compose --env-file .env.local ps."
