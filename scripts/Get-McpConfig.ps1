#Requires -Version 7.0
[CmdletBinding()]
param([ValidateSet('CodexToml', 'StdioJson')][string]$Format = 'CodexToml')

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$environmentPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $environmentPython -PathType Leaf)) {
    throw 'Run scripts/Setup.ps1 in this installation first'
}

if ($Format -eq 'StdioJson') {
    # A process descriptor, not a promise about a host-specific config schema.
    [ordered]@{
        command = $environmentPython
        args = @((Join-Path $projectRoot 'main.py'), 'mcp')
        cwd = $projectRoot
    } | ConvertTo-Json -Depth 4
    return
}

# JSON strings use escapes accepted by TOML basic strings, including Windows paths.
# Render only; the caller chooses whether and where to save or merge the fragment.
$templatePath = Join-Path $projectRoot 'assets/codex-mcp.toml.template'
$config = Get-Content -LiteralPath $templatePath -Raw -Encoding utf8
$config = $config.Replace('{{PYTHON_COMMAND}}', (ConvertTo-Json -InputObject $environmentPython -Compress))
$config = $config.Replace('{{ENTRY_POINT}}', (ConvertTo-Json -InputObject (Join-Path $projectRoot 'main.py') -Compress))
$config = $config.Replace('{{PROJECT_ROOT}}', (ConvertTo-Json -InputObject $projectRoot -Compress))
Write-Output $config.TrimEnd()
