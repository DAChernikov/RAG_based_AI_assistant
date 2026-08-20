$ErrorActionPreference = "Stop"
$ProjectDirectory = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDirectory
docker compose --env-file .env.local down
