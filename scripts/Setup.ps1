#Requires -Version 7.0
[CmdletBinding()]
param([string]$PythonExecutable = 'python')

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$environmentPath = Join-Path $projectRoot '.venv'
$environmentPython = Join-Path $environmentPath 'Scripts/python.exe'

& $PythonExecutable -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required by the runtime and checks"'
if ($LASTEXITCODE -ne 0) { throw 'Python version check failed' }

if (-not (Test-Path -LiteralPath $environmentPython)) {
    & $PythonExecutable -m venv $environmentPath
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}

& $environmentPython -c 'import sys; assert sys.version_info >= (3, 11), "Existing .venv uses an unsupported Python; recreate it with Python 3.11+"'
if ($LASTEXITCODE -ne 0) { throw 'Existing environment Python version check failed' }

& $environmentPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed' }

& $environmentPython -m pip install --disable-pip-version-check --no-deps --editable $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'Editable project installation failed' }

& $environmentPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed' }

& $environmentPython -c 'import sys,vnc_mcp; print("Python:",sys.executable); print("Source:",vnc_mcp.__file__)'
if ($LASTEXITCODE -ne 0) { throw 'Project import failed' }
