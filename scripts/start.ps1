$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$python = '.\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'Virtual environment not found. Run scripts/setup.ps1 first.'
    exit 1
}
& $python -m relaxbook.cli start @args
exit $LASTEXITCODE
