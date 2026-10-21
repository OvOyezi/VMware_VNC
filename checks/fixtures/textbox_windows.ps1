param([Parameter(Mandatory)][string]$ResultPath)
# Run only inside an explicitly selected test guest (built-in PowerShell 5.1).
# This disposable test application intentionally records its text for comparison.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = 'VNC MCP Unicode verification'
$form.Width = 1000
$form.Height = 650
$form.StartPosition = 'CenterScreen'
$box = New-Object System.Windows.Forms.TextBox
$box.Multiline = $true
$box.AcceptsTab = $true
$box.AcceptsReturn = $true
$box.MaxLength = 200000
$box.Dock = 'Fill'
$box.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 18)
$form.Controls.Add($box)
$save = {
    $result = @{text=$box.Text; length=$box.Text.Length; process_id=$PID; updated=[DateTime]::UtcNow.ToString('o')}
    [IO.File]::WriteAllText($ResultPath, ($result | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
}
# Native multiline Edit controls do not consistently implement Ctrl+A.
$box.Add_KeyDown({ param($sender,$event)
    if ($event.Control -and $event.KeyCode -eq [System.Windows.Forms.Keys]::A) {
        $box.SelectAll()
        $event.SuppressKeyPress = $true
    }
})
$box.Add_TextChanged($save)
$form.Add_Shown({ $form.Activate(); $box.Focus(); & $save })
[System.Windows.Forms.Application]::Run($form)
