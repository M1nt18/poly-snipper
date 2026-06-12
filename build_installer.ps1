$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onefile `
  --name PolySnipper `
  --hidden-import pystray._win32 `
  poly_snipper.py

$isccCandidates = @(
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
  "$env:LocalAppData\Programs\Inno Setup 6\ISCC.exe"
)

$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
  if ($cmd) { $iscc = $cmd.Source }
}
if (-not $iscc) {
  throw "Inno Setup Compiler (ISCC.exe) was not found."
}

& $iscc ".\PolySnipper.iss"

Write-Host "Built: $projectRoot\dist\PolySnipper.exe"
Write-Host "Built: $projectRoot\installer\PolySnipperSetup.exe"
