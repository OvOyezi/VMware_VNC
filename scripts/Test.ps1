#Requires -Version 7.0
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$environmentPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $environmentPython)) { throw 'Run scripts/Setup.ps1 first' }
$resultsPath = Join-Path $projectRoot 'checks/results'
if (-not (Test-Path -LiteralPath $resultsPath)) {
    New-Item -ItemType Directory -Path $resultsPath -ErrorAction Stop | Out-Null
}
$checks = @(
    @{ Name = 'smoke'; Script = 'vnc_mcp_smoke.py' },
    @{ Name = 'lifecycle'; Script = 'vnc_mcp_regression.py' },
    @{ Name = 'deep'; Script = 'vnc_mcp_deep_checks.py' },
    @{ Name = 'multisession'; Script = 'vnc_mcp_multisession.py' },
    @{ Name = 'diagnostics'; Script = 'vnc_mcp_diagnostics.py' },
    @{ Name = 'extensions'; Script = 'vnc_mcp_extensions.py' },
    @{ Name = 'visual'; Script = 'vnc_mcp_visual.py' },
    @{ Name = 'codec'; Script = 'vnc_mcp_codec.py' },
    @{ Name = 'native-rfb'; Script = 'vnc_mcp_native_rfb.py' },
    @{ Name = 'inspection'; Script = 'vnc_mcp_inspection.py' },
    @{ Name = 'fidelity'; Script = 'vnc_mcp_fidelity.py' }
)
$count = 0
foreach ($check in $checks) {
    $outputPath = Join-Path $resultsPath ("project-{0}.json" -f $check.Name)
    $scriptPath = Join-Path $projectRoot ("checks/{0}" -f $check.Script)
    & $environmentPython $scriptPath --source $projectRoot --output $outputPath `
        1> (Join-Path $resultsPath ("project-{0}.runner.stdout.log" -f $check.Name)) `
        2> (Join-Path $resultsPath ("project-{0}.runner.stderr.log" -f $check.Name))
    if ($LASTEXITCODE -ne 0) { throw ("{0} failed; inspect {1}" -f $check.Name, $outputPath) }
    $report = Get-Content -LiteralPath $outputPath -Raw -Encoding utf8 | ConvertFrom-Json
    if (-not $report.success) { throw ("{0} reported failure" -f $check.Name) }
    $passed = if ($check.Name -eq 'lifecycle') {
        @($report.refresh.PSObject.Properties).Count + @($report.fresh.PSObject.Properties).Count
    } else { @($report.checks.PSObject.Properties).Count }
    $count += $passed
    Write-Output ("PASS {0}: {1} checks" -f $check.Name, $passed)
}
Write-Output ("PASS total: {0} checks; synthetic desktops only" -f $count)
