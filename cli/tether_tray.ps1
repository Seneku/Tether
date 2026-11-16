# Tether's notification-area icon (`ue dash --tray`, Windows): its colour says whether Tether is busy (green), has
# sessions queued for an editor (amber), has editors open and idle (blue) or is quiet (grey); a hollow ring means
# the dashboard server is not answering (the icon starts it again). Hovering shows what runs; a click opens the
# dashboard; right-click for the menu. It polls the dashboard's /api/tray every 3 s, which keeps that server up.
# ASCII only: Windows PowerShell 5.1 reads a script without a BOM as the ANSI code page.
param(
    [int]$Port = 8765,
    [string]$Python = "python",
    [string]$Ue = "",
    [string]$State = "",
    [string]$Preview = ""  # draw the icons and the hover panel (from the dashboard's state now) into this PNG, and exit
)

$ErrorActionPreference = "Stop"
if (-not $State) { $State = Join-Path $env:LOCALAPPDATA "Tether\tray-$Port.json" }
$LogFile = [IO.Path]::ChangeExtension($State, ".log")
function Write-Log([string]$message) {
    try { Add-Content -LiteralPath $LogFile -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $message) } catch { }
}

try {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    Add-Type -Namespace TetherTray -Name Native -MemberDefinition @"
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")] public static extern bool DestroyIcon(System.IntPtr handle);
"@
    # A panel that never takes the focus from the window the user is typing in.
    Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @"
public class TetherPopup : System.Windows.Forms.Form {
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override System.Windows.Forms.CreateParams CreateParams {
        get {
            System.Windows.Forms.CreateParams p = base.CreateParams;
            p.ExStyle |= 0x08000000 | 0x00000080 | 0x00000008;  // WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
            return p;
        }
    }
}
"@
    [void][TetherTray.Native]::SetProcessDPIAware()
} catch {
    Write-Log "could not load Windows Forms: $_"
    exit 1
}

# One icon per port.
$created = $false
$mutex = $null
if (-not $Preview) {
    $mutex = New-Object System.Threading.Mutex($true, "Local\TetherDashTray_$Port", [ref]$created)
    if (-not $created) { exit 0 }
}

if (-not $Preview) { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $State) | Out-Null
(@{ pid = $PID; port = $Port; started = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() } | ConvertTo-Json -Compress) |
    Set-Content -LiteralPath $State -Encoding Ascii }

$Base = "http://127.0.0.1:$Port"

function Get-Api([string]$path) {
    try {
        $request = [System.Net.HttpWebRequest]::Create("$Base$path")
        $request.Timeout = 2500
        $request.ReadWriteTimeout = 2500
        $request.Proxy = $null
        $response = $request.GetResponse()
        try {
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
            return ($reader.ReadToEnd() | ConvertFrom-Json)
        } finally { $response.Close() }
    } catch { return $null }
}

$script:lastStart = [datetime]::MinValue
function Start-Server([bool]$force = $false) {
    if (-not $Ue) { return }
    if (-not $force -and ((Get-Date) - $script:lastStart).TotalSeconds -lt 20) { return }
    $script:lastStart = Get-Date
    try {
        Start-Process -FilePath $Python -ArgumentList @("`"$Ue`"", "dash", "--no-open", "--listen", "$Port") `
            -WindowStyle Hidden
    } catch { Write-Log "could not start the dashboard: $_" }
}

# ---- icons
$IconSize = [Math]::Max(16, [System.Windows.Forms.SystemInformation]::SmallIconSize.Width)
function New-StateIcon([string]$hex, [bool]$hollow) {
    $bitmap = New-Object System.Drawing.Bitmap $IconSize, $IconSize
    $g = [System.Drawing.Graphics]::FromImage($bitmap)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::Transparent)
    $colour = [System.Drawing.ColorTranslator]::FromHtml($hex)
    $pad = [float][Math]::Max(1, $IconSize / 16)
    $box = New-Object System.Drawing.RectangleF($pad, $pad, ($IconSize - 2 * $pad - 1), ($IconSize - 2 * $pad - 1))
    if ($hollow) {
        $pen = New-Object System.Drawing.Pen($colour, [float][Math]::Max(2, $IconSize / 8))
        $g.DrawEllipse($pen, $box)
        $ink = New-Object System.Drawing.SolidBrush($colour)
    } else {
        $g.FillEllipse((New-Object System.Drawing.SolidBrush($colour)), $box)
        $ink = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    }
    $font = New-Object System.Drawing.Font("Segoe UI", [float]($IconSize * 0.58), [System.Drawing.FontStyle]::Bold,
        [System.Drawing.GraphicsUnit]::Pixel)
    $format = New-Object System.Drawing.StringFormat
    $format.Alignment = [System.Drawing.StringAlignment]::Center
    $format.LineAlignment = [System.Drawing.StringAlignment]::Center
    $g.DrawString("T", $font, $ink, (New-Object System.Drawing.RectangleF(0, [float]($IconSize * 0.03), $IconSize, $IconSize)), $format)
    $g.Dispose()
    $handle = $bitmap.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($handle).Clone()
    [void][TetherTray.Native]::DestroyIcon($handle)
    $bitmap.Dispose()
    return $icon
}
$Icons = @{
    busy    = New-StateIcon "#2f9e5b" $false
    queued  = New-StateIcon "#d38b1c" $false
    idle    = New-StateIcon "#4a7fd6" $false
    quiet   = New-StateIcon "#8a8f98" $false
    offline = New-StateIcon "#8a8f98" $true
}

# ---- the hover panel
$popup = New-Object TetherPopup
$popup.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$popup.ShowInTaskbar = $false
$popup.TopMost = $true
$popup.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$popup.BackColor = [System.Drawing.ColorTranslator]::FromHtml("#1d1f23")
$titleLabel = New-Object System.Windows.Forms.Label
$titleLabel.AutoSize = $true
$titleLabel.ForeColor = [System.Drawing.Color]::White
$titleLabel.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
$bodyLabel = New-Object System.Windows.Forms.Label
$bodyLabel.AutoSize = $true
$bodyLabel.ForeColor = [System.Drawing.ColorTranslator]::FromHtml("#d8dadf")
$bodyLabel.Font = New-Object System.Drawing.Font("Consolas", 9)
$bodyLabel.MaximumSize = New-Object System.Drawing.Size(720, 0)  # longer lines wrap
$footLabel = New-Object System.Windows.Forms.Label
$footLabel.AutoSize = $true
$footLabel.ForeColor = [System.Drawing.ColorTranslator]::FromHtml("#9096a0")
$footLabel.Font = New-Object System.Drawing.Font("Segoe UI", 8)
$accent = New-Object System.Windows.Forms.Panel
$popup.Controls.AddRange(@($accent, $titleLabel, $bodyLabel, $footLabel))

$script:data = $null
$script:hoverAt = $null
$Colours = @{ busy = "#2f9e5b"; queued = "#d38b1c"; idle = "#4a7fd6"; quiet = "#8a8f98"; offline = "#8a8f98" }

function Update-Popup {
    $d = $script:data
    if ($d) {
        $titleLabel.Text = $d.title
        $bodyLabel.Text = ($d.lines -join "`r`n")
        $state = $d.state
    } else {
        $titleLabel.Text = "Tether: the dashboard is not answering"
        $bodyLabel.Text = "starting it again (ue dash); click to open it"
        $state = "offline"
    }
    $footLabel.Text = "click: open the dashboard    right-click: menu" + $(if ($d) { "    " + (Get-Date).ToString("HH:mm:ss") } else { "" })
    $accent.BackColor = [System.Drawing.ColorTranslator]::FromHtml($Colours[$state])
    $pad = 12
    $accent.SetBounds(0, 0, 4, 10)
    $titleLabel.Location = New-Object System.Drawing.Point(($pad + 2), $pad)
    $bodyLabel.Location = New-Object System.Drawing.Point(($pad + 2), ($titleLabel.Bottom + 6))
    $footLabel.Location = New-Object System.Drawing.Point(($pad + 2), ($bodyLabel.Bottom + 8))
    $width = [Math]::Max($titleLabel.Right, [Math]::Max($bodyLabel.Right, $footLabel.Right)) + $pad
    $height = $footLabel.Bottom + $pad
    $popup.ClientSize = New-Object System.Drawing.Size($width, $height)
    $accent.SetBounds(0, 0, 4, $height)
}

function Show-Popup {
    Update-Popup
    $p = [System.Windows.Forms.Cursor]::Position
    $area = [System.Windows.Forms.Screen]::FromPoint($p).WorkingArea
    $w, $h = $popup.Width, $popup.Height
    $x = [Math]::Min([Math]::Max($p.X - [int]($w / 2), $area.Left + 6), $area.Right - $w - 6)
    if ($p.Y -ge $area.Bottom) { $y = $area.Bottom - $h - 8 }       # taskbar at the bottom
    elseif ($p.Y -lt $area.Top) { $y = $area.Top + 8 }              # at the top
    else { $y = [Math]::Max($area.Top + 6, [Math]::Min($p.Y - $h - 16, $area.Bottom - $h - 6)) }  # at a side
    $popup.Location = New-Object System.Drawing.Point($x, $y)
    if (-not $popup.Visible) { $popup.Show() }
}

if ($Preview) {
    $script:data = Get-Api "/api/tray"
    Update-Popup
    $popup.Location = New-Object System.Drawing.Point(-30000, -30000)  # off every screen
    $popup.Show()
    [System.Windows.Forms.Application]::DoEvents()
    Update-Popup
    $panel = New-Object System.Drawing.Bitmap $popup.Width, $popup.Height
    $popup.DrawToBitmap($panel, (New-Object System.Drawing.Rectangle(0, 0, $popup.Width, $popup.Height)))
    $names = @("busy", "queued", "idle", "quiet", "offline")
    $sheet = New-Object System.Drawing.Bitmap ([Math]::Max($popup.Width, $names.Count * ($IconSize * 2 + 8)) + 16), ($popup.Height + $IconSize * 2 + 24)
    $g = [System.Drawing.Graphics]::FromImage($sheet)
    $g.Clear([System.Drawing.ColorTranslator]::FromHtml("#202020"))
    for ($i = 0; $i -lt $names.Count; $i++) {
        $g.DrawIcon($Icons[$names[$i]], (New-Object System.Drawing.Rectangle((8 + $i * ($IconSize * 2 + 8)), 8, ($IconSize * 2), ($IconSize * 2))))
    }
    $g.DrawImage($panel, 8, ($IconSize * 2 + 16))
    $g.Dispose()
    $sheet.Save($Preview, [System.Drawing.Imaging.ImageFormat]::Png)
    exit 0
}

# ---- the icon
$tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon = $Icons.offline
$tray.Text = ""  # the hover panel says it all (a tooltip would cover it)
$tray.Visible = $true

function Open-Dashboard {
    if (-not (Get-Api "/api/ping")) {
        Start-Server $true
        for ($i = 0; $i -lt 40 -and -not (Get-Api "/api/ping"); $i++) { Start-Sleep -Milliseconds 250 }
    }
    try { Start-Process "$Base/" } catch { Write-Log "could not open the browser: $_" }
}

function Update-State {
    $d = Get-Api "/api/tray"
    $script:data = $d
    if ($d) {
        $tray.Icon = $Icons[[string]$d.state]
    } else {
        $tray.Icon = $Icons.offline
        Start-Server
    }
    if ($popup.Visible) { Update-Popup }
}

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$openItem = $menu.Items.Add("Open dashboard")
$openItem.Font = New-Object System.Drawing.Font($openItem.Font, [System.Drawing.FontStyle]::Bold)
$openItem.add_Click({ Open-Dashboard })
$menu.Items.Add("Refresh now").add_Click({ Update-State }) | Out-Null
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
$menu.Items.Add("Quit the Tether icon").add_Click({ [System.Windows.Forms.Application]::Exit() }) | Out-Null
$tray.ContextMenuStrip = $menu
$menu.add_Opening({ $popup.Hide() })

$tray.add_MouseClick({
    param($sender, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) { $popup.Hide(); Open-Dashboard }
})
$tray.add_MouseMove({
    $script:hoverAt = [System.Windows.Forms.Cursor]::Position
    if (-not $menu.Visible) { Show-Popup }
})

# The panel goes once the pointer leaves the icon (the icon only reports moves over itself).
$hideTimer = New-Object System.Windows.Forms.Timer
$hideTimer.Interval = 150
$hideTimer.add_Tick({
    if ($popup.Visible -and $script:hoverAt) {
        $p = [System.Windows.Forms.Cursor]::Position
        if ([Math]::Abs($p.X - $script:hoverAt.X) -gt 24 -or [Math]::Abs($p.Y - $script:hoverAt.Y) -gt 24) { $popup.Hide() }
    }
})
$hideTimer.Start()

$pollTimer = New-Object System.Windows.Forms.Timer
$pollTimer.Interval = 3000
$pollTimer.add_Tick({ Update-State })
$pollTimer.Start()
Update-State

try {
    [System.Windows.Forms.Application]::Run()
} catch {
    Write-Log "stopped: $_"
} finally {
    $tray.Visible = $false
    $tray.Dispose()
    try { Remove-Item -LiteralPath $State -ErrorAction SilentlyContinue } catch { }
    $mutex.ReleaseMutex()
}
