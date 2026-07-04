<#
.SYNOPSIS
    CaveViewer Setup -- a single "Install" button that takes a non-technical
    user from "freshly downloaded folder" to "double-click icon on Desktop"
    without ever opening a terminal themselves.

.DESCRIPTION
    One button runs three steps in sequence, stopping and re-enabling
    itself if any step fails (rather than silently continuing as if
    nothing went wrong):
      1. Install Python  -- checks if Python is already present; if not,
         downloads the official installer from python.org and runs it
         silently with PATH registration forced on (this exact setting is
         what caused confusion the first time this project was set up
         manually, so the installer here sets it automatically).
      2. Install Requirements -- runs `pip install -r requirements.txt`
         against this project's requirements file, streaming output into
         the on-screen log box.
      3. Create Desktop Shortcut -- writes a .lnk on the Desktop that runs
         `python caveviewer.py` with the working directory set correctly,
         so double-clicking it from the Desktop launches CaveViewer.

    Once all three steps succeed, the window shows a success message
    briefly, then closes itself automatically.

    This script is plain PowerShell + Windows Forms, which ship built into
    Windows 10/11 -- no separate install needed to RUN this setup tool
    itself, which is the whole point (you can't require Python to install
    Python).

.NOTES
    Must be run on Windows. Installing Python system-wide requires
    administrator privileges -- the script requests elevation automatically
    if it isn't already running elevated (a Windows UAC prompt will appear;
    this is expected and necessary).
#>

param(
    [int]$IoWorkers = 0
)

if ($IoWorkers -lt 0) {
    $IoWorkers = 0
}

# -- Self-elevate if not already running as Administrator -------------------
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    $scriptPath = $MyInvocation.MyCommand.Path
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
    if ($IoWorkers -gt 0) {
        $argList += " -IoWorkers $IoWorkers"
    }
    Start-Process powershell.exe -ArgumentList $argList -Verb RunAs
    exit
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class CaveViewerDpi {
    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();

    [DllImport("shcore.dll")]
    private static extern int SetProcessDpiAwareness(int value);

    public static void Enable() {
        try {
            SetProcessDpiAwareness(1);
        } catch {
            try { SetProcessDPIAware(); } catch {}
        }
    }
}
"@
[CaveViewerDpi]::Enable()
[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Windows.Forms.Application]::SetCompatibleTextRenderingDefault($false)

# -- Paths --------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# When packaged in the release zip, setup.ps1 is copied to the bundle root so
# requirements.txt is a sibling.  In the source tree it lives at
# scripts\windows\ -- two levels below the project root.
if (Test-Path (Join-Path $ScriptDir "requirements.txt")) {
    $ProjectRoot = $ScriptDir
} else {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
}

$RequirementsFile = Join-Path $ProjectRoot "requirements.txt"
$MainScript = Join-Path $ProjectRoot "caveviewer.py"

$PythonInstallerUrl = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
$PythonInstallerPath = Join-Path $env:TEMP "python-installer-caveviewer.exe"

# -- Form setup -----------------------------------------------------------------

$DpiScale = 1.0
try {
    $screenGraphics = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero)
    $DpiScale = [Math]::Max(1.0, $screenGraphics.DpiX / 96.0)
    $screenGraphics.Dispose()
} catch {
    $DpiScale = 1.0
}

function S {
    param([int]$Value)
    return [int][Math]::Round($Value * $DpiScale)
}

function Font-S {
    param(
        [string]$Family,
        [float]$Size,
        [System.Drawing.FontStyle]$Style = [System.Drawing.FontStyle]::Regular
    )
    return New-Object System.Drawing.Font($Family, [single]$Size, $Style)
}

function Point-S {
    param([int]$X, [int]$Y)
    return New-Object System.Drawing.Point((S $X), (S $Y))
}

function Size-S {
    param([int]$Width, [int]$Height)
    return New-Object System.Drawing.Size((S $Width), (S $Height))
}

$ColorWindow = [System.Drawing.Color]::FromArgb(248, 249, 252)
$ColorHeader = [System.Drawing.Color]::FromArgb(17, 24, 39)
$ColorAccent = [System.Drawing.Color]::FromArgb(202, 162, 62)
$ColorText = [System.Drawing.Color]::FromArgb(31, 41, 55)
$ColorMuted = [System.Drawing.Color]::FromArgb(107, 114, 128)
$ColorPanel = [System.Drawing.Color]::White
$ColorLogBack = [System.Drawing.Color]::FromArgb(245, 247, 250)
$ColorLogText = [System.Drawing.Color]::FromArgb(45, 55, 72)
$FontBody = Font-S "Segoe UI" 9
$FontSmall = Font-S "Segoe UI" 8.5
$FontTitle = Font-S "Segoe UI" 18 ([System.Drawing.FontStyle]::Bold)
$FontSection = Font-S "Segoe UI" 10.5 ([System.Drawing.FontStyle]::Bold)
$FontButton = Font-S "Segoe UI" 10 ([System.Drawing.FontStyle]::Bold)
$FontLog = Font-S "Consolas" 8.5

$form = New-Object System.Windows.Forms.Form
$form.Text = "CaveViewer Setup"
$form.ClientSize = Size-S 720 540
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.BackColor = $ColorWindow
$form.Font = $FontBody
$form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::None

$headerPanel = New-Object System.Windows.Forms.Panel
$headerPanel.BackColor = $ColorHeader
$headerPanel.Location = Point-S 0 0
$headerPanel.Size = Size-S 720 108
$form.Controls.Add($headerPanel)

$titleLabel = New-Object System.Windows.Forms.Label
$titleLabel.Text = "CaveViewer Setup"
$titleLabel.Font = $FontTitle
$titleLabel.ForeColor = [System.Drawing.Color]::White
$titleLabel.BackColor = $ColorHeader
$titleLabel.Location = Point-S 28 20
$titleLabel.Size = Size-S 660 34
$headerPanel.Controls.Add($titleLabel)

$subLabel = New-Object System.Windows.Forms.Label
$subLabel.Text = "Installs Python if needed, prepares CaveViewer's libraries, and creates a Desktop shortcut."
$subLabel.Font = $FontBody
$subLabel.ForeColor = [System.Drawing.Color]::FromArgb(221, 226, 235)
$subLabel.BackColor = $ColorHeader
$subLabel.Location = Point-S 30 60
$subLabel.Size = Size-S 660 24
$headerPanel.Controls.Add($subLabel)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "Ready to install"
$statusLabel.Font = $FontSection
$statusLabel.ForeColor = $ColorText
$statusLabel.Location = Point-S 28 126
$statusLabel.Size = Size-S 520 26
$form.Controls.Add($statusLabel)

$progressBar = New-Object System.Windows.Forms.ProgressBar
$progressBar.Location = Point-S 28 139
$progressBar.Size = Size-S 520 24
$progressBar.Minimum = 0
$progressBar.Maximum = 100
$progressBar.Value = 0
$progressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Continuous
$form.Controls.Add($progressBar)

# Single Install button -- runs all three steps in sequence.
$btnInstall = New-Object System.Windows.Forms.Button
$btnInstall.Text = "Install"
$btnInstall.Location = Point-S 572 136
$btnInstall.Size = Size-S 116 36
$btnInstall.Font = $FontButton
$btnInstall.BackColor = $ColorAccent
$btnInstall.ForeColor = [System.Drawing.Color]::FromArgb(24, 19, 8)
$btnInstall.FlatStyle = "Flat"
$btnInstall.FlatAppearance.BorderSize = 0
$form.Controls.Add($btnInstall)

$stepsPanel = New-Object System.Windows.Forms.Panel
$stepsPanel.BackColor = $ColorPanel
$stepsPanel.Location = Point-S 28 194
$stepsPanel.Size = Size-S 214 286
$stepsPanel.BorderStyle = "FixedSingle"
$form.Controls.Add($stepsPanel)

$stepsHeader = New-Object System.Windows.Forms.Label
$stepsHeader.Text = "Setup steps"
$stepsHeader.Font = $FontSection
$stepsHeader.ForeColor = $ColorText
$stepsHeader.Location = Point-S 16 14
$stepsHeader.Size = Size-S 180 24
$stepsPanel.Controls.Add($stepsHeader)

$stepLabels = @{}
$stepNames = @("Python", "Visual C++ runtime", "Python libraries", "Firewall access", "Desktop shortcut")
for ($i = 0; $i -lt $stepNames.Count; $i++) {
    $stepLabel = New-Object System.Windows.Forms.Label
    $stepLabel.Text = "[ ] $($stepNames[$i])"
    $stepLabel.Font = $FontBody
    $stepLabel.ForeColor = $ColorMuted
    $stepLabel.Location = Point-S 18 (52 + ($i * 38))
    $stepLabel.Size = Size-S 176 26
    $stepsPanel.Controls.Add($stepLabel)
    $stepLabels[$stepNames[$i]] = $stepLabel
}

# Log box
$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Multiline = $true
$logBox.ScrollBars = "Vertical"
$logBox.ReadOnly = $true
$logBox.Font = $FontLog
$logBox.Location = Point-S 262 194
$logBox.Size = Size-S 428 286
$logBox.BackColor = $ColorLogBack
$logBox.ForeColor = $ColorLogText
$logBox.BorderStyle = "FixedSingle"
$form.Controls.Add($logBox)

$footerLabel = New-Object System.Windows.Forms.Label
$footerLabel.Text = "You can leave this window open while setup runs. The log is here if something needs troubleshooting."
$footerLabel.Font = $FontSmall
$footerLabel.ForeColor = $ColorMuted
$footerLabel.Location = Point-S 28 500
$footerLabel.Size = Size-S 662 22
$form.Controls.Add($footerLabel)

function Set-SetupStatus {
    param(
        [string]$Message,
        [int]$Percent
    )
    $statusLabel.Text = $Message
    $progressBar.Value = [Math]::Max($progressBar.Minimum, [Math]::Min($progressBar.Maximum, $Percent))
    [System.Windows.Forms.Application]::DoEvents()
}

function Set-StepState {
    param(
        [string]$Name,
        [ValidateSet("pending", "running", "done", "failed")]
        [string]$State
    )
    if (-not $stepLabels.ContainsKey($Name)) {
        return
    }
    $label = $stepLabels[$Name]
    switch ($State) {
        "pending" {
            $label.Text = "[ ] $Name"
            $label.ForeColor = $ColorMuted
        }
        "running" {
            $label.Text = "[>] $Name"
            $label.ForeColor = $ColorAccent
        }
        "done" {
            $label.Text = "[x] $Name"
            $label.ForeColor = [System.Drawing.Color]::FromArgb(22, 126, 76)
        }
        "failed" {
            $label.Text = "[!] $Name"
            $label.ForeColor = [System.Drawing.Color]::FromArgb(185, 28, 28)
        }
    }
    [System.Windows.Forms.Application]::DoEvents()
}

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "HH:mm:ss"
    $logBox.AppendText("[$timestamp] $Message`r`n")
    $logBox.SelectionStart = $logBox.Text.Length
    $logBox.ScrollToCaret()
    [System.Windows.Forms.Application]::DoEvents()
}

function Invoke-CaveViewerDownload {
    <#
        Downloads $Url to $DestinationPath using System.Net.WebClient with
        a real timeout, rather than Invoke-WebRequest.

        Why not Invoke-WebRequest: its default progress-bar rendering is
        known to be extremely slow on Windows PowerShell 5.1 specifically
        (a long-standing, widely-reported issue) -- each progress update
        can add massive overhead to a large download, sometimes making a
        download that should take well under a minute appear to hang
        indefinitely with the window showing "Not Responding" and no way
        to recover except force-closing the whole setup tool. WebClient
        avoids that progress-rendering path entirely.

        Returns $true on success, $false on failure/timeout (network
        issue, server error, or exceeding $TimeoutSeconds) -- never
        throws, so callers can just check the return value rather than
        wrapping every call site in their own try/catch.
    #>
    param(
        [string]$Url,
        [string]$DestinationPath,
        [int]$TimeoutSeconds = 180
    )

    try {
        $webClient = New-Object System.Net.WebClient
        $downloadTask = $webClient.DownloadFileTaskAsync($Url, $DestinationPath)

        $elapsed = 0.0
        $lastLoggedSecond = -1
        while (-not $downloadTask.IsCompleted -and $elapsed -lt $TimeoutSeconds) {
            Start-Sleep -Milliseconds 500
            $elapsed += 0.5
            # log progress roughly every 5 seconds rather than every poll,
            # so the log box doesn't fill up with near-duplicate lines
            $currentSecond = [math]::Floor($elapsed)
            if (($currentSecond % 5 -eq 0) -and ($currentSecond -ne $lastLoggedSecond)) {
                $lastLoggedSecond = $currentSecond
                $sizeSoFar = if (Test-Path $DestinationPath) { (Get-Item $DestinationPath).Length } else { 0 }
                Write-Log "  ...downloading, $([math]::Round($sizeSoFar / 1MB, 1)) MB so far"
            }
        }

        if (-not $downloadTask.IsCompleted) {
            Write-Log "WARNING: download timed out after $TimeoutSeconds seconds."
            $webClient.CancelAsync()
            $webClient.Dispose()
            Remove-Item $DestinationPath -Force -ErrorAction SilentlyContinue
            return $false
        }

        if ($downloadTask.IsFaulted) {
            $innerMessage = if ($downloadTask.Exception -and $downloadTask.Exception.InnerException) {
                $downloadTask.Exception.InnerException.Message
            } else {
                "unknown error"
            }
            Write-Log "WARNING: download failed: $innerMessage"
            $webClient.Dispose()
            Remove-Item $DestinationPath -Force -ErrorAction SilentlyContinue
            return $false
        }

        $webClient.Dispose()
        return $true
    } catch {
        Write-Log "WARNING: download failed: $($_.Exception.Message)"
        Remove-Item $DestinationPath -Force -ErrorAction SilentlyContinue
        return $false
    }
}

function Test-PythonInstalled {
    <#
        Checks for a working `python` command on PATH. Returns the version
        string if found, or $null if not found / not runnable.

        Hardened against a known Windows quirk: on a fresh system with no
        Python installed, typing `python` doesn't always fail cleanly --
        Windows sometimes routes it through a built-in "app execution
        alias" stub that opens the Microsoft Store instead of erroring.
        That stub typically lives under WindowsApps and is a few KB in
        size (a real Python install is not), so as a second check beyond
        just "did the command run", we also reject suspiciously tiny
        python.exe files living in that specific stub location.
    #>
    try {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -like "*\WindowsApps\python.exe") {
            $fileInfo = Get-Item $cmd.Source -ErrorAction SilentlyContinue
            if ($fileInfo -and $fileInfo.Length -lt 100000) {
                return $null
            }
        }

        $output = & python --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            return $output.ToString().Trim()
        }
    } catch {
        # python not found on PATH at all -- expected, not an error to surface
    }
    return $null
}

function Resolve-PythonGuiPath {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
    $pythonDir = Split-Path -Parent $pythonPath
    $pythonwPath = Join-Path $pythonDir "pythonw.exe"
    if (Test-Path $pythonwPath) {
        return $pythonwPath
    }
    return $pythonPath
}

function Start-CaveViewerApp {
    $pythonGuiPath = Resolve-PythonGuiPath

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $pythonGuiPath
    $psi.Arguments = "`"$MainScript`""
    $psi.WorkingDirectory = $ProjectRoot
    $psi.UseShellExecute = $false
    if ($IoWorkers -gt 0) {
        $psi.EnvironmentVariables["CAVEVIEWER_IO_WORKERS"] = [string]$IoWorkers
    }

    return [System.Diagnostics.Process]::Start($psi)
}

# -- Step functions -----------------------------------------------------------
# Each returns $true on success, $false on failure. The single Install
# button below runs them in order and stops at the first failure, rather
# than barreling ahead and pretending a failed step succeeded.

function Install-Python {
    Write-Log "Checking for an existing Python installation..."

    $existing = Test-PythonInstalled
    if ($existing) {
        Write-Log "Found existing Python: $existing -- skipping install."
        return $true
    }

    Write-Log "Python not found. Downloading the official installer from python.org..."
    Write-Log "(This may take a minute depending on your internet connection.)"

    $downloadOk = Invoke-CaveViewerDownload -Url $PythonInstallerUrl -DestinationPath $PythonInstallerPath -TimeoutSeconds 120
    if (-not $downloadOk) {
        Write-Log "ERROR: Failed to download the Python installer. Check your internet connection and try again."
        return $false
    }

    Write-Log "Download complete. Running the installer silently..."
    Write-Log "(InstallAllUsers + PrependPath are set automatically -- this is the"
    Write-Log " 'Add Python to PATH' step that has to be checked manually otherwise.)"

    $installArgs = "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0"
    $proc = Start-Process -FilePath $PythonInstallerPath -ArgumentList $installArgs -Wait -PassThru

    if ($proc.ExitCode -ne 0) {
        Write-Log "ERROR: Python installer exited with code $($proc.ExitCode)."
        return $false
    }

    Write-Log "Python installed successfully."

    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"

    Start-Sleep -Seconds 1
    $verify = Test-PythonInstalled
    if ($verify) {
        Write-Log "Verified: $verify"
        return $true
    }

    Write-Log "WARNING: Python installed, but couldn't be verified in this window."
    Write-Log "This can happen if Windows hasn't fully refreshed PATH yet -- continuing anyway."
    return $true
}

function Install-VcRedist {
    <#
        Silently installs the Microsoft Visual C++ 2015-2022 Redistributable
        (x64) if it is not already present.

        Why this is needed: some Python extension packages (including pyglm,
        which moderngl-window depends on) ship compiled .pyd files that link
        against vcruntime140.dll / vcruntime140_1.dll. These DLLs are part of
        the MSVC Redistributable. Python's own installer includes them inside
        its own directory, but some .pyd files resolve them via the system
        PATH/SxS manifest instead -- and if the Redistributable was never
        installed system-wide, the DLL is not found and Python throws
        "ImportError: DLL load failed" on the very first import.

        The Redistributable installer is idempotent: if a compatible version
        is already present it exits immediately with code 0 and does nothing.
        If it needs to install, it runs silently with no visible UI.

        Non-fatal: if the download or install fails for any reason, setup
        continues -- the packages may still work if vcruntime140.dll was
        already present from a previous install of Python, Visual Studio,
        or another application.
    #>
    $vcRedistUrl  = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    $vcRedistPath = Join-Path $env:TEMP "vc_redist_caveviewer.exe"

    Write-Log "Ensuring Visual C++ Redistributable is installed..."

    $downloadOk = Invoke-CaveViewerDownload -Url $vcRedistUrl -DestinationPath $vcRedistPath -TimeoutSeconds 60
    if (-not $downloadOk) {
        Write-Log "Note: could not download the Visual C++ Redistributable -- skipping (may already be installed)."
        return
    }

    try {
        $proc = Start-Process -FilePath $vcRedistPath -ArgumentList "/quiet /norestart" -Wait -PassThru
        if ($proc.ExitCode -eq 0 -or $proc.ExitCode -eq 1638) {
            # 0 = installed successfully, 1638 = a newer version is already installed
            Write-Log "Visual C++ Redistributable is ready."
        } else {
            Write-Log "Note: Visual C++ Redistributable installer returned code $($proc.ExitCode) -- continuing anyway."
        }
    } catch {
        Write-Log "Note: could not run the Visual C++ Redistributable installer -- continuing anyway."
    } finally {
        Remove-Item $vcRedistPath -Force -ErrorAction SilentlyContinue
    }
}

function Install-Requirements {
    Write-Log "Installing required packages from requirements.txt..."
    Write-Log "(moderngl, moderngl-window, numpy, Pillow, truststore -- this may take a minute.)"

    if (-not (Test-Path $RequirementsFile)) {
        Write-Log "ERROR: Could not find requirements.txt at:"
        Write-Log "  $RequirementsFile"
        Write-Log "Make sure this setup folder is still inside the CaveViewer project folder."
        return $false
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "python"
    $psi.Arguments = "-m pip install -r `"$RequirementsFile`""
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    $outputHandler = {
        if (-not [string]::IsNullOrEmpty($EventArgs.Data)) {
            Write-Log $EventArgs.Data
        }
    }
    Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action $outputHandler | Out-Null
    Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $outputHandler | Out-Null

    $proc.Start() | Out-Null
    $proc.BeginOutputReadLine()
    $proc.BeginErrorReadLine()
    $proc.WaitForExit()

    Get-EventSubscriber | Where-Object { $_.SourceObject -eq $proc } | Unregister-Event

    if ($proc.ExitCode -ne 0) {
        Write-Log "ERROR: pip install failed (exit code $($proc.ExitCode))."
        return $false
    }

    Write-Log "All requirements installed successfully."
    return $true
}

function Add-PythonFirewallRule {
    <#
        Adds a Windows Firewall outbound allow-rule for python.exe so that
        CaveViewer can reach GitHub (update checks, sample-map catalog) the
        very first time it runs.

        Why this matters: setup runs elevated (Administrator), but the
        Desktop shortcut launches python as a normal user.  The first time a
        newly-installed python.exe makes an outbound TCP connection, Windows
        Defender Firewall can show a "allow / block" popup.  If the user
        dismisses or blocks that popup, Python is firewall-blocked for every
        subsequent session -- the app can't check for updates or load the
        sample-map list even though the machine is genuinely online.
        Creating the rule here, while we're still elevated, prevents the
        popup entirely and ensures the app works out of the box.

        This step is intentionally non-fatal: if the rule can't be created
        (e.g. a corporate Group Policy blocks New-NetFirewallRule), setup
        still completes successfully -- the app will run, and the worst-case
        is the user sees the firewall popup on first launch.
    #>
    try {
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        $pythonPath = if ($pythonCmd) { $pythonCmd.Source } else { $null }
        if (-not $pythonPath -or -not (Test-Path $pythonPath)) {
            Write-Log "Firewall rule skipped: python.exe path not found."
            return
        }

        $ruleName = "CaveViewer - Python outbound"

        # Remove any stale rule from a previous install before re-adding.
        Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue

        New-NetFirewallRule `
            -DisplayName $ruleName `
            -Direction Outbound `
            -Program $pythonPath `
            -Action Allow `
            -Profile Any `
            -Enabled True | Out-Null

        Write-Log "Firewall rule created -- Python can reach the internet when launched from the Desktop shortcut."
    } catch {
        # Non-fatal: log the reason but let setup continue.
        Write-Log "Note: could not create firewall rule ($($_.Exception.Message))."
        Write-Log "If CaveViewer can't reach GitHub on first launch, allow Python through Windows Firewall manually."
    }
}

function New-DesktopShortcut {
    Write-Log "Creating a CaveViewer shortcut on your Desktop..."

    try {
        $desktopPath = [System.Environment]::GetFolderPath("Desktop")
        $shortcutPath = Join-Path $desktopPath "CaveViewer.lnk"

        $pythonGuiPath = Resolve-PythonGuiPath
        $iconPath = Join-Path $ScriptDir "icon\caveviewer.ico"
        $stableIconDir = Join-Path $env:ProgramData "CaveViewer"
        $stableIconPath = Join-Path $stableIconDir "caveviewer.ico"

        $wshShell = New-Object -ComObject WScript.Shell
        $shortcut = $wshShell.CreateShortcut($shortcutPath)

        if ($IoWorkers -gt 0) {
            # Launch through hidden PowerShell so the shortcut can set this
            # runtime-only environment variable without modifying system/user
            # env settings or showing a console window.
            $shortcut.TargetPath = "powershell.exe"
            $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"`$env:CAVEVIEWER_IO_WORKERS='$IoWorkers'; Start-Process -WindowStyle Hidden -FilePath '$pythonGuiPath' -ArgumentList '`"$MainScript`"' -WorkingDirectory '$ProjectRoot'`""
            Write-Log "Shortcut configured with CAVEVIEWER_IO_WORKERS=$IoWorkers."
        } else {
            $shortcut.TargetPath = $pythonGuiPath
            $shortcut.Arguments = "`"$MainScript`""
        }

        $shortcut.WorkingDirectory = $ProjectRoot
        $shortcut.Description = "Launch CaveViewer"

        if (Test-Path $iconPath) {
            New-Item -ItemType Directory -Path $stableIconDir -Force | Out-Null
            Copy-Item $iconPath $stableIconPath -Force
            $shortcut.IconLocation = "$stableIconPath,0"
            Write-Log "Using custom CaveViewer icon."
        } else {
            $shortcut.IconLocation = "$pythonGuiPath,0"
            Write-Log "Custom icon not found at $iconPath -- using default icon instead."
        }

        $shortcut.Save()
        Write-Log "Shortcut created: $shortcutPath"
        return $true
    } catch {
        Write-Log "ERROR: Failed to create the desktop shortcut."
        Write-Log $_.Exception.Message
        return $false
    }
}

# -- Single Install button: runs all three steps in sequence -----------------

$btnInstall.Add_Click({
    $btnInstall.Enabled = $false
    $btnInstall.Text = "Installing..."
    foreach ($stepName in $stepNames) {
        Set-StepState $stepName "pending"
    }
    Set-SetupStatus "Checking Python..." 5
    Set-StepState "Python" "running"

    $ok = Install-Python
    if (-not $ok) {
        Set-StepState "Python" "failed"
        Set-SetupStatus "Python installation failed" 5
        Write-Log ""
        Write-Log "Setup stopped -- Python installation failed. Click Install to try again."
        $btnInstall.Enabled = $true
        $btnInstall.Text = "Install"
        return
    }
    Set-StepState "Python" "done"

    # Non-fatal: ensures vcruntime140.dll is present system-wide so that
    # Python extension packages (pyglm, moderngl, etc.) can load their DLLs.
    Set-SetupStatus "Preparing Visual C++ runtime..." 25
    Set-StepState "Visual C++ runtime" "running"
    Install-VcRedist
    Set-StepState "Visual C++ runtime" "done"

    Set-SetupStatus "Installing Python libraries..." 45
    Set-StepState "Python libraries" "running"
    $ok = Install-Requirements
    if (-not $ok) {
        Set-StepState "Python libraries" "failed"
        Set-SetupStatus "Library installation failed" 45
        Write-Log ""
        Write-Log "Setup stopped -- installing requirements failed. Click Install to try again."
        $btnInstall.Enabled = $true
        $btnInstall.Text = "Install"
        return
    }
    Set-StepState "Python libraries" "done"

    # Non-fatal: add a firewall rule so CaveViewer can reach GitHub on first
    # launch without triggering a "block or allow?" popup.
    Set-SetupStatus "Configuring firewall access..." 75
    Set-StepState "Firewall access" "running"
    Add-PythonFirewallRule
    Set-StepState "Firewall access" "done"

    Set-SetupStatus "Creating Desktop shortcut..." 88
    Set-StepState "Desktop shortcut" "running"
    $ok = New-DesktopShortcut
    if (-not $ok) {
        Set-StepState "Desktop shortcut" "failed"
        Set-SetupStatus "Shortcut creation failed" 88
        Write-Log ""
        Write-Log "Setup stopped -- could not create the desktop shortcut. Click Install to try again."
        $btnInstall.Enabled = $true
        $btnInstall.Text = "Install"
        return
    }
    Set-StepState "Desktop shortcut" "done"

    Write-Log ""
    Write-Log "All done!"
    Set-SetupStatus "CaveViewer is ready" 100
    $btnInstall.Text = "Done"
    $btnInstall.Enabled = $false

    Show-InstallCompleteDialog
})

function Show-InstallCompleteDialog {
    <#
        Shown once setup finishes successfully -- replaces the previous
        behavior of just logging a success message and auto-closing after
        a few seconds. Gives the person an explicit choice: launch
        CaveViewer right now, or just close the setup window and launch
        it later from the Desktop shortcut.
    #>
    $dialog = New-Object System.Windows.Forms.Form
    $dialog.Text = "CaveViewer Setup"
    $dialog.ClientSize = Size-S 460 220
    $dialog.StartPosition = "CenterParent"
    $dialog.FormBorderStyle = "FixedDialog"
    $dialog.MaximizeBox = $false
    $dialog.MinimizeBox = $false
    $dialog.BackColor = $ColorWindow
    $dialog.Font = $FontBody
    $dialog.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::None

    $msgLabel = New-Object System.Windows.Forms.Label
    $msgLabel.Text = "CaveViewer is ready"
    $msgLabel.Font = Font-S "Segoe UI" 16 ([System.Drawing.FontStyle]::Bold)
    $msgLabel.ForeColor = $ColorText
    $msgLabel.Location = Point-S 28 24
    $msgLabel.Size = Size-S 404 38
    $dialog.Controls.Add($msgLabel)

    $subLabel = New-Object System.Windows.Forms.Label
    $subLabel.Text = "Setup finished successfully. You can launch CaveViewer now or use the Desktop shortcut later."
    $subLabel.ForeColor = $ColorMuted
    $subLabel.Location = Point-S 30 70
    $subLabel.Size = Size-S 400 48
    $dialog.Controls.Add($subLabel)

    $btnLaunch = New-Object System.Windows.Forms.Button
    $btnLaunch.Text = "Launch CaveViewer"
    $btnLaunch.Location = Point-S 112 146
    $btnLaunch.Size = Size-S 160 40
    $btnLaunch.Font = $FontButton
    $btnLaunch.BackColor = $ColorAccent
    $btnLaunch.ForeColor = [System.Drawing.Color]::FromArgb(24, 19, 8)
    $btnLaunch.FlatStyle = "Flat"
    $btnLaunch.FlatAppearance.BorderSize = 0
    $dialog.Controls.Add($btnLaunch)

    $btnClose = New-Object System.Windows.Forms.Button
    $btnClose.Text = "Close"
    $btnClose.Location = Point-S 288 146
    $btnClose.Size = Size-S 88 40
    $btnClose.Font = $FontBody
    $dialog.Controls.Add($btnClose)

    $btnLaunch.Add_Click({
        try {
            $proc = Start-CaveViewerApp
            Start-Sleep -Milliseconds 1200
            if ($proc -and $proc.HasExited) {
                Write-Log "WARNING: CaveViewer exited immediately after launch (exit code $($proc.ExitCode))."
                Write-Log "The setup window will stay open so you can review the log or use the Desktop shortcut after fixing the issue."
                return
            }
            $dialog.Close()
        } catch {
            Write-Log "WARNING: Could not launch CaveViewer automatically: $($_.Exception.Message)"
            Write-Log "You can still double-click the CaveViewer icon on your Desktop."
        }
    })

    $btnClose.Add_Click({
        $dialog.Close()
    })

    # Treat every way this dialog can close (Launch, Close, or the
    # window's own [X] button) the same way: close the now-pointless
    # setup window behind it. Handling this once here, rather than
    # calling $form.Close() separately inside each button's own handler,
    # avoids closing $form twice over for the buttons (FormClosed fires
    # for ANY dismissal of $dialog, including a programmatic .Close()
    # call from a button handler, not just the titlebar [X]).
    $dialog.Add_FormClosed({
        $form.Close()
    })

    [void]$dialog.ShowDialog($form)
}

Write-Log "Welcome to CaveViewer Setup."
if ($IoWorkers -gt 0) {
    Write-Log "Runtime worker override enabled: CAVEVIEWER_IO_WORKERS=$IoWorkers"
}
Write-Log "Click Install to set up Python, the required libraries, and a Desktop shortcut."

[void]$form.ShowDialog()
