param([string]$Python = "")
$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not $Python) {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        $env:UV_CACHE_DIR = Join-Path $projectRoot ".runtime\uv-cache"
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $projectRoot ".runtime\python"
        & $uv.Source python install 3.11 --no-registry --no-bin
        if ($LASTEXITCODE -ne 0) { throw "Python download failed." }
        & $uv.Source venv --python 3.11 .venv
        if ($LASTEXITCODE -ne 0) { throw "Virtual environment setup failed." }
        & $uv.Source pip install --python .venv\Scripts\python.exe -r backend\requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    } else {
        throw "Install Python 3.11 or uv first, then run this script with -Python pointing to Python 3.11."
    }
} else {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment setup failed." }
    & .venv\Scripts\python.exe -m pip install -r backend\requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
}
Write-Host "Ready. Double-click start-windows.cmd to open VideoToNo."
