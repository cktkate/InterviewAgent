$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

python -m pip install -r requirements-desktop.txt
python -m PyInstaller --noconfirm --clean InterviewAgent.spec

$ExampleEnv = Join-Path $ProjectRoot ".env.example"
$OutputEnv = Join-Path $ProjectRoot "dist\InterviewAgent\.env.example"
Copy-Item -LiteralPath $ExampleEnv -Destination $OutputEnv -Force

Write-Host "Build complete: dist\InterviewAgent\InterviewAgent.exe"
Write-Host "Copy your .env into dist\InterviewAgent before starting the app."
