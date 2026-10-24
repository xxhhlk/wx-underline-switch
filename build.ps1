# build.ps1 - one-click build for WxUnderlineSwitch
#
# ASCII only on purpose: PowerShell 5.1 decodes non-BOM files as ANSI/GBK,
# and non-ASCII bytes there can swallow quotes and break the script.
#
# Requires: a Python 3 installation that includes tkinter.
# Usage:    powershell -ExecutionPolicy Bypass -File build.ps1

param([string]$PythonPath, [string]$BuildEnv)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# --- 1. locate a Python that has tkinter -------------------------------------
$pyCandidates = @(
    $PythonPath,
    "$env:USERPROFILE\.workbuddy\binaries\python\versions\3.13.12\python.exe",
    "C:\Program Files\Python312\python.exe",
    "C:\Program Files\Python311\python.exe",
    "C:\Program Files\Python310\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd) { $pyCandidates += $cmd.Source }

$python = $null
foreach ($p in $pyCandidates) {
    if (-not $p) { continue }
    if (-not (Test-Path $p)) { continue }
    & $p -c "import tkinter" 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $p; break }
}
if (-not $python) {
    Write-Host "ERROR: no Python with tkinter found. Install Python 3 (python.org build)." -ForegroundColor Red
    exit 1
}
Write-Host ("Python: " + $python)

# --- 2. venv -----------------------------------------------------------------
$venv = $BuildEnv
if (-not $venv) {
    $venv = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\envs\wx-release312"
}
$venvParent = Split-Path -Parent $venv
if (-not (Test-Path $venvParent -PathType Container)) {
    Write-Host "ERROR: BuildEnv parent directory must exist." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $venv)) {
    Write-Host "Creating venv..."
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: venv creation failed." -ForegroundColor Red; exit 1 }
}
$vpy = Join-Path $venv "Scripts\python.exe"

Write-Host "Installing build requirements..."
& $vpy -m pip install --disable-pip-version-check -q -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: pip install failed." -ForegroundColor Red; exit 1 }

# --- 3. pre-build checks -----------------------------------------------------
Write-Host "Running pyflakes..."
& $vpy -m pyflakes "src\wx_underline_switch.py" "src\offline_combo.py" "scripts\combo_test.py"
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: pyflakes reported problems." -ForegroundColor Red; exit 1 }

Write-Host "Running offline transaction tests..."
& $vpy "scripts\combo_test.py"
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: transaction tests failed." -ForegroundColor Red; exit 1 }

Write-Host "Running GUI smoke test..."
& $vpy "scripts\gui_smoketest.py"
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: GUI smoke test failed." -ForegroundColor Red; exit 1 }

# --- 4. icon + build ---------------------------------------------------------
& $vpy "scripts\make_icon.py" "assets\app.ico"
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: icon generation failed." -ForegroundColor Red; exit 1 }

# Use absolute paths because spec-relative paths resolve beneath build/.
$iconPath = Join-Path $root "assets\app.ico"
$entryPath = Join-Path $root "src\wx_underline_switch.py"

Write-Host "Building executable..."
& $vpy -m PyInstaller --noconfirm --onefile --noconsole `
    --name WxUnderlineSwitch `
    --icon $iconPath `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") `
    --specpath (Join-Path $root "build") `
    $entryPath
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: PyInstaller failed." -ForegroundColor Red; exit 1 }

# --- 5. add a Chinese-named copy (built from code points to stay ASCII) ------
$exe = Join-Path $root "dist\WxUnderlineSwitch.exe"
if (Test-Path $exe) {
    $cnName = -join ([char[]](0x5FAE, 0x4FE1, 0x5212, 0x7EBF, 0x5F00, 0x5173))
    $cnExe = Join-Path $root ("dist\" + $cnName + ".exe")
    Copy-Item $exe $cnExe -Force
    $hash = (Get-FileHash -Algorithm SHA1 $exe).Hash
    Write-Host ""
    Write-Host "Build OK" -ForegroundColor Green
    Write-Host ("  " + $exe)
    Write-Host ("  " + $cnExe)
    Write-Host ("  SHA1 " + $hash)
} else {
    Write-Host "ERROR: output executable not found." -ForegroundColor Red
    exit 1
}
