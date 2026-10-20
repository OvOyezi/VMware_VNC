#Requires -Version 7.0
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$skillRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$environmentPython = Join-Path $skillRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $environmentPython -PathType Leaf)) {
    throw 'Run scripts/Setup.ps1 in this skill installation first'
}

# JSON strings use escapes accepted by TOML basic strings, including Windows paths.
# Render only; the caller chooses whether and where to save or merge the fragment.
$templatePath = Join-Path $skillRoot 'assets/codex-mcp.toml.template'
$config = Get-Content -LiteralPath $templatePath -Raw -Encoding utf8
$config = $config.Replace('{{PYTHON_COMMAND}}', (ConvertTo-Json -InputObject $environmentPython -Compress))
$config = $config.Replace('{{ENTRY_POINT}}', (ConvertTo-Json -InputObject (Join-Path $skillRoot 'main.py') -Compress))
$config = $config.Replace('{{SKILL_ROOT}}', (ConvertTo-Json -InputObject $skillRoot -Compress))
Write-Output $config.TrimEnd()
