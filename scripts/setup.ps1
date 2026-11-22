$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$python = if (Test-Path '.\.venv\Scripts\python.exe') { '.\.venv\Scripts\python.exe' } else { 'python' }
& $python -m relaxbook.cli setup @args
exit $LASTEXITCODE
