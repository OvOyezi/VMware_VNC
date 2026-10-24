# Windows guest test fixture; launch explicitly in the selected guest, never by Setup/Test.
param([Parameter(Mandatory)][string]$ControlPath, [Parameter(Mandatory)][string]$ResultPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object Windows.Forms.Form
$form.Text = 'VNC visual condition test'
$form.StartPosition = 'Manual'
$form.Location = New-Object Drawing.Point(80, 60)
$form.ClientSize = New-Object Drawing.Size(700, 340)
$form.TopMost = $true
$label = New-Object Windows.Forms.Label
$label.Location = New-Object Drawing.Point(20, 15)
$label.Size = New-Object Drawing.Size(660, 45)
$label.Text = 'Read-only visual test: changing panel (left), constant panel (right).'
$left = New-Object Windows.Forms.Panel
$left.Location = New-Object Drawing.Point(20, 75)
$left.Size = New-Object Drawing.Size(280, 200)
$left.BackColor = [Drawing.Color]::FromArgb(30, 90, 160)
$right = New-Object Windows.Forms.Panel
$right.Location = New-Object Drawing.Point(370, 75)
$right.Size = New-Object Drawing.Size(280, 200)
$right.BackColor = [Drawing.Color]::FromArgb(45, 70, 95)
$form.Controls.AddRange(@($label, $left, $right))
$script:generation = -1
$script:mode = 'static'
$script:phase = 0
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 100
$timer.Add_Tick({
    if (Test-Path -LiteralPath $ControlPath) {
        try {
            $control = Get-Content -LiteralPath $ControlPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($control.generation -ne $script:generation) {
                if ($control.mode -notin @('static', 'pulse')) { throw 'Invalid fixture mode' }
                $script:generation = $control.generation
                $script:mode = $control.mode
                $left.BackColor = [Drawing.Color]::FromArgb([int]$control.red, [int]$control.green, [int]$control.blue)
            }
        } catch { } # The host may be copying the next control file; wait for a complete one.
    }
    $script:phase++
    if ($script:mode -eq 'pulse') {
        # A long colour cycle avoids aliasing a two-frame blink with polling.
        $left.BackColor = [Drawing.Color]::FromArgb(($script:phase * 37) % 256, ($script:phase * 73) % 256, ($script:phase * 109) % 256)
    }
    $originLeft = $left.PointToScreen([Drawing.Point]::Empty)
    $originRight = $right.PointToScreen([Drawing.Point]::Empty)
    $result = @{generation=$script:generation; mode=$script:mode; process_id=$PID; phase=$script:phase;
                left=@{x=$originLeft.X;y=$originLeft.Y;width=$left.Width;height=$left.Height};
                right=@{x=$originRight.X;y=$originRight.Y;width=$right.Width;height=$right.Height};
                left_rgb=@($left.BackColor.R,$left.BackColor.G,$left.BackColor.B)}
    $temporary = $ResultPath + '.tmp'
    [IO.File]::WriteAllText($temporary, ($result | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $ResultPath) { [IO.File]::Replace($temporary, $ResultPath, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $ResultPath) }
})
$form.Add_Shown({ $timer.Start() })
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })
[Windows.Forms.Application]::Run($form)
