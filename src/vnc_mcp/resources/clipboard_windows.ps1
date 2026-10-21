param([Parameter(Mandatory)][string]$PayloadPath, [Parameter(Mandatory)][string]$ResultPath)
# Guest Windows PowerShell 5.1 is intentional: it is included with Windows.
# Host-side maintenance continues to use PowerShell 7. ExecutionPolicy is scoped
# to this process by the invoker; no machine policy or keyboard setting changes.
$ErrorActionPreference = 'Stop'
$payload = Get-Content -LiteralPath $PayloadPath -Raw -Encoding UTF8 | ConvertFrom-Json
$result = @{ success=$false; token=$payload.token }
try {
    Add-Type -AssemblyName System.Windows.Forms
    $text = [string]$payload.text
    for ($attempt=0; $attempt -lt 5; $attempt++) {
        try {
            if ($text.Length) { [System.Windows.Forms.Clipboard]::SetText($text, [System.Windows.Forms.TextDataFormat]::UnicodeText) }
            else { [System.Windows.Forms.Clipboard]::Clear() }
            break
        } catch [System.Runtime.InteropServices.ExternalException] {
            if ($attempt -eq 4) { throw }
            Start-Sleep -Milliseconds 50
        }
    }
    $observed = [System.Windows.Forms.Clipboard]::GetText([System.Windows.Forms.TextDataFormat]::UnicodeText)
    if ($observed -cne $text) { throw 'Clipboard verification failed' }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $result.sha256 = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($observed)))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
    $result.success = $true
} catch {
    # Do not include input text, usernames or underlying command arguments.
    $result.error = $_.Exception.GetType().Name
}
[IO.File]::WriteAllText($ResultPath, ($result | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
