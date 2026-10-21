[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ResultPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Token
)

# Manual live-input fixture. Run explicitly inside the selected Windows guest.
# It uses only the guest's built-in Windows PowerShell 5.1 and WinForms.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$script:FullResultPath = [IO.Path]::GetFullPath($ResultPath)
$script:TemporaryPath = $script:FullResultPath + '.' + $PID + '.tmp'
$script:Counts = [ordered]@{
    mouse_down = 0
    mouse_up = 0
    mouse_move = 0
    wheel = 0
    key_down = 0
    key_up = 0
}
$script:ButtonClicks = 0
$script:ColorName = 'green'
$script:Dragging = $false
$script:LastDragPoint = $null
$script:Drag = $null

$script:Form = New-Object System.Windows.Forms.Form
$script:Form.Text = "VNC input verification - $Token"
$script:Form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$script:Form.ClientSize = New-Object System.Drawing.Size(1000, 650)
$script:Form.MinimumSize = New-Object System.Drawing.Size(1016, 689)
$script:Form.TopMost = $true
$script:Form.KeyPreview = $true

$script:TextBox = New-Object System.Windows.Forms.TextBox
$script:TextBox.Multiline = $true
$script:TextBox.AcceptsTab = $true
$script:TextBox.AcceptsReturn = $true
$script:TextBox.MaxLength = 200000
$script:TextBox.WordWrap = $false
$script:TextBox.ScrollBars = [System.Windows.Forms.ScrollBars]::Both
$script:TextBox.Location = New-Object System.Drawing.Point(20, 55)
$script:TextBox.Size = New-Object System.Drawing.Size(580, 565)
$script:TextBox.Anchor = [System.Windows.Forms.AnchorStyles]::Top -bor
    [System.Windows.Forms.AnchorStyles]::Bottom -bor
    [System.Windows.Forms.AnchorStyles]::Left -bor
    [System.Windows.Forms.AnchorStyles]::Right
$script:TextBox.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 16)

$script:ToggleButton = New-Object System.Windows.Forms.Button
$script:ToggleButton.Text = 'Toggle canvas red / green'
$script:ToggleButton.Location = New-Object System.Drawing.Point(625, 55)
$script:ToggleButton.Size = New-Object System.Drawing.Size(355, 44)
$script:ToggleButton.Anchor = [System.Windows.Forms.AnchorStyles]::Top -bor
    [System.Windows.Forms.AnchorStyles]::Right

$script:Canvas = New-Object System.Windows.Forms.Panel
$script:Canvas.Location = New-Object System.Drawing.Point(625, 112)
$script:Canvas.Size = New-Object System.Drawing.Size(355, 190)
$script:Canvas.Anchor = [System.Windows.Forms.AnchorStyles]::Top -bor
    [System.Windows.Forms.AnchorStyles]::Right
$script:Canvas.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$script:Canvas.BackColor = [System.Drawing.Color]::ForestGreen
$script:Canvas.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 18)

$script:WheelArea = New-Object System.Windows.Forms.ListBox
$script:WheelArea.Location = New-Object System.Drawing.Point(625, 320)
$script:WheelArea.Size = New-Object System.Drawing.Size(355, 300)
$script:WheelArea.Anchor = [System.Windows.Forms.AnchorStyles]::Top -bor
    [System.Windows.Forms.AnchorStyles]::Bottom -bor
    [System.Windows.Forms.AnchorStyles]::Right
$script:WheelArea.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 11)
$script:WheelArea.IntegralHeight = $false
for ($line = 1; $line -le 60; $line++) {
    [void]$script:WheelArea.Items.Add(('Scrollable wheel test line {0:D2}' -f $line))
}

[void]$script:Form.Controls.AddRange([System.Windows.Forms.Control[]]@(
    $script:TextBox, $script:ToggleButton, $script:Canvas, $script:WheelArea
))

function Get-ControlBounds {
    param([System.Windows.Forms.Control]$Control)

    $origin = $Control.PointToScreen([System.Drawing.Point]::Empty)
    return [ordered]@{
        x = [int]$origin.X
        y = [int]$origin.Y
        width = [int]$Control.Width
        height = [int]$Control.Height
    }
}

function Write-InputReport {
    if (-not $script:Form.Visible -or -not $script:Form.IsHandleCreated -or
        $script:Form.WindowState -eq [System.Windows.Forms.FormWindowState]::Minimized) {
        return
    }

    $report = [ordered]@{
        schema_version = 1
        token = $Token
        process_id = $PID
        text = $script:TextBox.Text
        color = $script:ColorName
        button_clicks = $script:ButtonClicks
        counts = $script:Counts
        wheel_delta = $script:WheelDelta
        drag = $script:Drag
        controls = [ordered]@{
            text = Get-ControlBounds $script:TextBox
            button = Get-ControlBounds $script:ToggleButton
            canvas = Get-ControlBounds $script:Canvas
            wheel = Get-ControlBounds $script:WheelArea
        }
    }

    $json = ConvertTo-Json -InputObject $report -Depth 6 -Compress
    [IO.File]::WriteAllText($script:TemporaryPath, $json, [Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($script:FullResultPath)) {
        [IO.File]::Replace($script:TemporaryPath, $script:FullResultPath, [NullString]::Value)
    }
    else {
        try {
            [IO.File]::Move($script:TemporaryPath, $script:FullResultPath)
        }
        catch {
            if ([IO.File]::Exists($script:FullResultPath) -and
                [IO.File]::Exists($script:TemporaryPath)) {
                [IO.File]::Replace($script:TemporaryPath, $script:FullResultPath, [NullString]::Value)
            }
            else {
                throw
            }
        }
    }
}

$script:WheelDelta = 0
$script:MouseDownHandler = {
    param($sender, $eventArgs)
    $script:Counts.mouse_down++
    if ([object]::ReferenceEquals($sender, $script:Canvas) -and
        $eventArgs.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        $point = $sender.PointToScreen($eventArgs.Location)
        $script:Dragging = $true
        $script:LastDragPoint = $point
        $script:Drag = [ordered]@{
            start = [ordered]@{ x = [int]$point.X; y = [int]$point.Y }
            end = $null
        }
        $sender.Capture = $true
    }
    Write-InputReport
}
$script:MouseUpHandler = {
    param($sender, $eventArgs)
    $script:Counts.mouse_up++
    if ([object]::ReferenceEquals($sender, $script:Canvas) -and $script:Dragging) {
        $point = $sender.PointToScreen($eventArgs.Location)
        $script:Drag['end'] = [ordered]@{ x = [int]$point.X; y = [int]$point.Y }
        $script:Dragging = $false
        $script:LastDragPoint = $null
        $sender.Capture = $false
    }
    Write-InputReport
}
$script:MouseMoveHandler = {
    param($sender, $eventArgs)
    $script:Counts.mouse_move++
    if ([object]::ReferenceEquals($sender, $script:Canvas) -and $script:Dragging) {
        $point = $sender.PointToScreen($eventArgs.Location)
        $deltaX = $point.X - $script:LastDragPoint.X
        $deltaY = $point.Y - $script:LastDragPoint.Y
        $script:Canvas.Location = [System.Drawing.Point]::new(
            $script:Canvas.Left + $deltaX, $script:Canvas.Top + $deltaY
        )
        $script:LastDragPoint = $point
    }
    Write-InputReport
}

foreach ($control in @($script:Form, $script:TextBox, $script:ToggleButton,
        $script:Canvas, $script:WheelArea)) {
    $control.Add_MouseDown($script:MouseDownHandler)
    $control.Add_MouseUp($script:MouseUpHandler)
    $control.Add_MouseMove($script:MouseMoveHandler)
}

$script:WheelArea.Add_MouseEnter({ $script:WheelArea.Focus() })
$script:WheelArea.Add_MouseWheel({
    param($sender, $eventArgs)
    $script:Counts.wheel++
    $script:WheelDelta += [int]$eventArgs.Delta
    Write-InputReport
})

$script:TextBox.Add_TextChanged({ Write-InputReport })
$script:Form.Add_KeyDown({
    $script:Counts.key_down++
    Write-InputReport
})
$script:Form.Add_KeyUp({
    $script:Counts.key_up++
    Write-InputReport
})
$script:TextBox.Add_KeyDown({
    param($sender, $eventArgs)
    if ($eventArgs.Control -and $eventArgs.KeyCode -eq [System.Windows.Forms.Keys]::A) {
        $sender.SelectAll()
        $eventArgs.SuppressKeyPress = $true
    }
})

$script:ToggleButton.Add_Click({
    $script:ButtonClicks++
    if ($script:ColorName -eq 'green') {
        $script:ColorName = 'red'
        $script:Canvas.BackColor = [System.Drawing.Color]::Firebrick
    }
    else {
        $script:ColorName = 'green'
        $script:Canvas.BackColor = [System.Drawing.Color]::ForestGreen
    }
    $script:Canvas.Invalidate()
    Write-InputReport
})
$script:Canvas.Add_Paint({
    param($sender, $eventArgs)
    $eventArgs.Graphics.DrawString(
        'DRAG THIS CANVAS', $sender.Font, [System.Drawing.Brushes]::White, 24, 22
    )
})
$script:Form.Add_LocationChanged({ Write-InputReport })
$script:Form.Add_Resize({ Write-InputReport })
$script:Canvas.Add_LocationChanged({ Write-InputReport })
$script:Form.Add_Shown({
    $script:Form.WindowState = [System.Windows.Forms.FormWindowState]::Normal
    $script:Form.ClientSize = New-Object System.Drawing.Size(1000, 650)
    $script:Form.Activate()
    $script:TextBox.Focus()
    Write-InputReport
})
$script:Form.Add_FormClosing({ Write-InputReport })

[System.Windows.Forms.Application]::Run($script:Form)
