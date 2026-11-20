$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-LocalPort([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

if (-not (Test-LocalPort 27017)) {
    $mongoService = Get-Service -Name MongoDB -ErrorAction SilentlyContinue
    if (-not $mongoService) {
        throw 'MongoDB is not listening and the Windows MongoDB service was not found. Start MongoDB, then run start.ps1 again.'
    }
    if ($mongoService.Status -ne 'Running') {
        Start-Service -Name MongoDB
    }

    $mongoReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-LocalPort 27017) {
            $mongoReady = $true
            break
        }
        Start-Sleep -Seconds 1
    }

    if (-not $mongoReady) {
        throw 'MongoDB is not listening on port 27017. Start the MongoDB service, then run start.ps1 again.'
    }
}

if (-not (Test-Path (Join-Path $root 'server\node_modules\express'))) {
    throw 'Server dependencies are missing. Run npm install in the server folder.'
}
if (-not (Test-Path (Join-Path $root 'client\node_modules\vite'))) {
    throw 'Client dependencies are missing. Run npm install in the client folder.'
}
$layer2Index = Join-Path $root 'ai_engine\layer2\index_storage\forbidden_prompts.index'
if (-not (Test-Path (Join-Path $root '.env'))) {
    throw 'The root .env file is missing. Configure the AI provider settings before starting PromptGuard.'
}
if (-not (Test-Path $layer2Index)) {
    throw 'The Layer 2 index asset is missing. Restore the required local index before starting.'
}
Push-Location $root
python -c 'import uvicorn, fastapi, httpx; from ai_engine.api.app import app' 2>$null
$pythonCheck = $LASTEXITCODE
Pop-Location
if ($pythonCheck -ne 0) {
    throw 'Python API dependencies are missing from the selected Python environment.'
}

$serverCommand = "Set-Location -LiteralPath '$root\server'; node index.js"
$engineCommand = "Set-Location -LiteralPath '$root'; python -m uvicorn ai_engine.api.app:app --host 127.0.0.1 --port 8000 --reload"
$clientCommand = "Set-Location -LiteralPath '$root\client'; npm run dev -- --host 127.0.0.1"

Start-Process powershell.exe -WorkingDirectory $root -ArgumentList @('-NoExit', '-Command', $serverCommand)
Start-Process powershell.exe -WorkingDirectory $root -ArgumentList @('-NoExit', '-Command', $engineCommand)
Start-Process powershell.exe -WorkingDirectory $root -ArgumentList @('-NoExit', '-Command', $clientCommand)

Write-Host 'Started PromptGuard services in separate PowerShell windows.'
Write-Host 'Client:    http://localhost:5173'
Write-Host 'Node API:  http://localhost:5000/health'
Write-Host 'AI engine: http://localhost:8000/health'
Write-Host 'Close the service windows to stop the processes.'
