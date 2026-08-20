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
         `python -m caveviewer` with the working directory set correctly,
         so double-clicking it from the Desktop launches CaveViewer.

    Once all three steps succeed, the window shows a success message
    briefly, then closes itself automatically.

    This script is plain PowerShell + Windows Forms, which ship built into
    Windows 10/11 -- no separate install needed to RUN this setup tool
    itself, which is the whole point (you can't require Python to install
    Python).

.NOTES
    Must be run on Windows. Setup deliberately stays in the desktop user's
    context: Python and CaveViewer's virtual environment live below
    %LOCALAPPDATA%, so an elevated installer can never create files the user
    cannot later launch or update.
#>

param(
    [int]$IoWorkers = 0,
    [switch]$AutoInstall,
    [switch]$NonInteractive,
    [string]$PythonExecutable = "",
    [string]$RuntimeRoot = "",
    [string]$ShortcutDirectory = "",
    [string]$LogDirectory = ""
)

if ($IoWorkers -lt 0) {
    $IoWorkers = 0
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

public static class CaveViewerConsole {
    [DllImport("kernel32.dll")]
    private static extern IntPtr GetConsoleWindow();

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_MINIMIZE = 6;

    public static void Minimize() {
        try {
            IntPtr handle = GetConsoleWindow();
            if (handle != IntPtr.Zero) {
                ShowWindow(handle, SW_MINIMIZE);
            }
        } catch {}
    }
}
"@
[CaveViewerDpi]::Enable()
[CaveViewerConsole]::Minimize()
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
$VersionFile = Join-Path $ProjectRoot "src/caveviewer/version.py"
$AppVersion = "dev"
if (Test-Path $VersionFile) {
    try {
        $versionMatch = Select-String -Path $VersionFile -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($versionMatch -and $versionMatch.Matches.Count -gt 0) {
            $AppVersion = $versionMatch.Matches[0].Groups[1].Value
        }
    } catch {
        $AppVersion = "dev"
    }
}
$SafeAppVersion = ($AppVersion -replace '[^A-Za-z0-9_.-]', '_')

$LocalAppDataRoot = [System.Environment]::GetFolderPath(
    [System.Environment+SpecialFolder]::LocalApplicationData
)
if ([string]::IsNullOrWhiteSpace($LocalAppDataRoot)) {
    $LocalAppDataRoot = $env:LOCALAPPDATA
}
if ([string]::IsNullOrWhiteSpace($LocalAppDataRoot)) {
    throw "CaveViewer Setup could not determine the current user's LocalAppData directory."
}

$CaveViewerDataRoot = Join-Path $LocalAppDataRoot "CaveViewer"
$SetupLogDirectory = if ([string]::IsNullOrWhiteSpace($LogDirectory)) {
    Join-Path $CaveViewerDataRoot "logs"
} else {
    [System.IO.Path]::GetFullPath($LogDirectory)
}
try {
    New-Item -ItemType Directory -Path $SetupLogDirectory -Force -ErrorAction Stop | Out-Null
} catch {
    throw "CaveViewer Setup could not create its log directory '$SetupLogDirectory': $($_.Exception.Message)"
}

$setupLogStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$SetupLogPath = Join-Path $SetupLogDirectory "setup-$SafeAppVersion-$setupLogStamp.log"
try {
    [System.IO.File]::WriteAllText(
        $SetupLogPath,
        "CaveViewer Setup log`r`n",
        [System.Text.Encoding]::UTF8
    )
} catch {
    throw "CaveViewer Setup could not create its log file '$SetupLogPath': $($_.Exception.Message)"
}

# Runtime state is intentionally explicit.  No later installation, shortcut,
# or launch step performs another ambient PATH lookup for Python.
$script:SelectedPython = $null
$script:RuntimePython = $null
$script:RuntimePythonw = $null
$script:RuntimeDirectory = $null
$script:LaunchScriptPath = $null

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
$form.Add_Shown({
    $form.TopMost = $true
    $form.Activate()
    $form.BringToFront()
    $form.TopMost = $false
})

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
$subLabel.Text = "Uses a verified Python 3.12 runtime, installs CaveViewer, and creates a Desktop shortcut."
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

$btnOpenLog = New-Object System.Windows.Forms.Button
$btnOpenLog.Text = "Open log"
$btnOpenLog.Location = Point-S 572 176
$btnOpenLog.Size = Size-S 116 24
$btnOpenLog.Font = $FontSmall
$btnOpenLog.FlatStyle = "Flat"
$btnOpenLog.FlatAppearance.BorderSize = 1
$form.Controls.Add($btnOpenLog)

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
$stepNames = @("Python", "CaveViewer runtime", "Python libraries", "Desktop shortcut")
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
$footerLabel.Text = "Licensed under GNU GPLv3. See LICENSE and THIRD_PARTY_NOTICES.md included with this setup folder."
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
    $line = "[$timestamp] $Message"
    try {
        Add-Content -LiteralPath $SetupLogPath -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch {
        # The visible log remains useful even if a storage failure occurs.
        $line = "$line [also could not append setup log: $($_.Exception.Message)]"
    }
    if ($null -ne $logBox) {
        $logBox.AppendText("$line`r`n")
        $logBox.SelectionStart = $logBox.Text.Length
        $logBox.ScrollToCaret()
    }
    [System.Windows.Forms.Application]::DoEvents()
}

$btnOpenLog.Add_Click({
    try {
        Start-Process -FilePath $SetupLogPath -ErrorAction Stop
    } catch {
        Write-Log "ERROR: Could not open setup log '$SetupLogPath': $($_.Exception.Message)"
    }
})

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

function ConvertTo-CaveViewerLogArgument {
    param([AllowEmptyString()][string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

function Format-CaveViewerCommand {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    $rendered = @(ConvertTo-CaveViewerLogArgument $FilePath)
    foreach ($argument in $ArgumentList) {
        $rendered += ConvertTo-CaveViewerLogArgument ([string]$argument)
    }
    return $rendered -join " "
}

function Invoke-CaveViewerCommand {
    <#
        Invoke a native command using a path and argument array, never an
        interpolated command line.  The rendered command is only for the
        retained diagnostic log and is fully quoted there as well.
    #>
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$Description
    )

    Write-Log "Running $Description: $(Format-CaveViewerCommand -FilePath $FilePath -ArgumentList $ArgumentList)"
    try {
        $output = @(& $FilePath @ArgumentList 2>&1)
        $exitCode = $LASTEXITCODE
    } catch {
        Write-Log "ERROR: $Description could not start: $($_.Exception.Message)"
        return [pscustomobject]@{ Succeeded = $false; ExitCode = $null }
    }

    foreach ($line in $output) {
        if ($null -ne $line -and -not [string]::IsNullOrWhiteSpace($line.ToString())) {
            Write-Log "[$Description] $line"
        }
    }
    Write-Log "$Description exited with code $exitCode."
    return [pscustomobject]@{ Succeeded = ($exitCode -eq 0); ExitCode = $exitCode }
}

function Get-PythonRuntimeInfo {
    <#
        Return a validated Python interpreter record, or $null.  A candidate
        must be a real executable, report Python 3.12, and report a 64-bit
        process.  Merely having a `python` command on PATH is not sufficient.
    #>
    param([string]$ExecutablePath)

    if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
        return $null
    }

    try {
        $resolvedPath = (Resolve-Path -LiteralPath $ExecutablePath -ErrorAction Stop).Path
        $fileInfo = Get-Item -LiteralPath $resolvedPath -ErrorAction Stop
    } catch {
        return $null
    }

    if ($fileInfo.PSIsContainer) {
        return $null
    }

    # Windows' Store app-execution alias must not be executed: it can open the
    # Store instead of starting Python.  Reject it before running a probe.
    if ($resolvedPath -match '(?i)\\WindowsApps\\python(?:\.exe)?$' -and $fileInfo.Length -lt 100000) {
        return $null
    }

    $probe = "import struct, sys; print('%d|%d|%d|%s' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8, sys.executable))"
    try {
        $probeOutput = @(& $resolvedPath "-c" $probe 2>&1)
        $probeExitCode = $LASTEXITCODE
    } catch {
        return $null
    }
    if ($probeExitCode -ne 0 -or $probeOutput.Count -eq 0) {
        return $null
    }

    $line = $probeOutput[$probeOutput.Count - 1].ToString().Trim()
    $parts = $line -split '\|', 4
    if ($parts.Count -ne 4) {
        return $null
    }

    try {
        $major = [int]$parts[0]
        $minor = [int]$parts[1]
        $bits = [int]$parts[2]
    } catch {
        return $null
    }

    if ($major -ne 3 -or $minor -ne 12 -or $bits -ne 64) {
        return $null
    }

    return [pscustomobject]@{
        ExecutablePath = $resolvedPath
        Version = "$major.$minor"
        Architecture = "$bits-bit"
    }
}

function Find-SupportedPython {
    $candidatePaths = New-Object System.Collections.Generic.List[string]

    if (-not [string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $candidatePaths.Add($PythonExecutable)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:CAVEVIEWER_PYTHON)) {
        $candidatePaths.Add($env:CAVEVIEWER_PYTHON)
    }

    if (-not [string]::IsNullOrWhiteSpace($LocalAppDataRoot)) {
        $candidatePaths.Add((Join-Path $LocalAppDataRoot "Programs\Python\Python312\python.exe"))
    }
    if (-not [string]::IsNullOrWhiteSpace($env:ProgramFiles)) {
        $candidatePaths.Add((Join-Path $env:ProgramFiles "Python312\python.exe"))
    }
    if (-not [string]::IsNullOrWhiteSpace(${env:ProgramFiles(x86)})) {
        $candidatePaths.Add((Join-Path ${env:ProgramFiles(x86)} "Python312\python.exe"))
    }

    try {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pythonCommand -and $pythonCommand.Source) {
            $candidatePaths.Add($pythonCommand.Source)
        }
    } catch {
        # No ambient Python command is an expected case.
    }

    try {
        $pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($pyCommand -and $pyCommand.Source) {
            $launcherOutput = @(& $pyCommand.Source -3.12 -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $launcherOutput.Count -gt 0) {
                $candidatePaths.Add($launcherOutput[$launcherOutput.Count - 1].ToString().Trim())
            }
        }
    } catch {
        # The Python launcher is optional.
    }

    $seen = @{}
    foreach ($candidatePath in $candidatePaths) {
        if ([string]::IsNullOrWhiteSpace($candidatePath)) {
            continue
        }
        $candidateKey = $candidatePath.ToLowerInvariant()
        if ($seen.ContainsKey($candidateKey)) {
            continue
        }
        $seen[$candidateKey] = $true

        $runtime = Get-PythonRuntimeInfo -ExecutablePath $candidatePath
        if ($null -ne $runtime) {
            return $runtime
        }
        Write-Log "Ignored unsupported Python candidate: $candidatePath"
    }

    return $null
}

function Install-SupportedPython {
    Write-Log "Looking for a real 64-bit Python 3.12 interpreter..."
    $runtime = Find-SupportedPython
    if ($null -ne $runtime) {
        Write-Log "Selected Python $($runtime.Version) $($runtime.Architecture): $($runtime.ExecutablePath)"
        return $runtime
    }

    if ($NonInteractive) {
        Write-Log "ERROR: Noninteractive setup requires a preinstalled, supported Python 3.12 interpreter."
        return $null
    }

    Write-Log "No supported Python 3.12 interpreter was found. Downloading the official per-user installer..."
    $downloadOk = Invoke-CaveViewerDownload -Url $PythonInstallerUrl -DestinationPath $PythonInstallerPath -TimeoutSeconds 120
    if (-not $downloadOk) {
        Write-Log "ERROR: Failed to download the Python installer. Check your internet connection and try again."
        return $null
    }

    $userPythonDirectory = Join-Path $LocalAppDataRoot "Programs\Python\Python312"
    $installArguments = @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=0",
        "Include_test=0",
        "Include_launcher=0",
        "Include_pip=1",
        "TargetDir=$userPythonDirectory"
    )
    $installResult = Invoke-CaveViewerCommand -FilePath $PythonInstallerPath -ArgumentList $installArguments -Description "Python 3.12 installer"
    if (-not $installResult.Succeeded) {
        Write-Log "ERROR: Python installer failed; exit code $($installResult.ExitCode)."
        return $null
    }

    $runtime = Get-PythonRuntimeInfo -ExecutablePath (Join-Path $userPythonDirectory "python.exe")
    if ($null -eq $runtime) {
        Write-Log "ERROR: The Python installer completed, but its 64-bit Python 3.12 executable could not be verified at '$userPythonDirectory'."
        return $null
    }

    Write-Log "Installed and verified Python $($runtime.Version) $($runtime.Architecture): $($runtime.ExecutablePath)"
    return $runtime
}

function Initialize-CaveViewerRuntime {
    param([pscustomobject]$PythonRuntime)

    if ($null -eq $PythonRuntime) {
        return $false
    }

    if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
        $runtimeDirectory = Join-Path $CaveViewerDataRoot "runtime\python312"
    } else {
        $runtimeDirectory = [System.IO.Path]::GetFullPath($RuntimeRoot)
    }
    $runtimePython = Join-Path $runtimeDirectory "Scripts\python.exe"
    $runtimePythonw = Join-Path $runtimeDirectory "Scripts\pythonw.exe"

    try {
        New-Item -ItemType Directory -Path $runtimeDirectory -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Log "ERROR: Could not create CaveViewer's runtime directory '$runtimeDirectory': $($_.Exception.Message)"
        return $false
    }

    if (Test-Path -LiteralPath $runtimePython) {
        $runtimeInfo = Get-PythonRuntimeInfo -ExecutablePath $runtimePython
        if ($null -eq $runtimeInfo) {
            Write-Log "ERROR: Existing CaveViewer runtime is not a verified 64-bit Python 3.12 environment: $runtimePython"
            return $false
        }
        Write-Log "Reusing verified CaveViewer runtime: $runtimePython"
    } else {
        $venvArguments = @("-m", "venv")
        if ($NonInteractive) {
            # The native package smoke is offline. It borrows only the runner's
            # already-installed build tooling while still creating a distinct
            # CaveViewer environment; normal user installation stays isolated.
            $venvArguments += "--system-site-packages"
        }
        $venvArguments += $runtimeDirectory
        $venvResult = Invoke-CaveViewerCommand -FilePath $PythonRuntime.ExecutablePath -ArgumentList $venvArguments -Description "CaveViewer virtual-environment creation"
        if (-not $venvResult.Succeeded -or -not (Test-Path -LiteralPath $runtimePython)) {
            Write-Log "ERROR: Could not create and verify CaveViewer's virtual environment at '$runtimeDirectory'."
            return $false
        }
        Write-Log "Created CaveViewer's user-owned virtual environment: $runtimeDirectory"
    }

    $pipResult = Invoke-CaveViewerCommand -FilePath $runtimePython -ArgumentList @("-m", "pip", "--version") -Description "CaveViewer runtime pip verification"
    if (-not $pipResult.Succeeded) {
        Write-Log "ERROR: CaveViewer's virtual environment has no usable pip."
        return $false
    }

    $script:SelectedPython = $PythonRuntime
    $script:RuntimeDirectory = $runtimeDirectory
    $script:RuntimePython = $runtimePython
    $script:RuntimePythonw = if (Test-Path -LiteralPath $runtimePythonw) { $runtimePythonw } else { $runtimePython }
    return $true
}

function Resolve-PythonGuiPath {
    if ([string]::IsNullOrWhiteSpace($script:RuntimePythonw)) {
        throw "CaveViewer's verified runtime has not been initialized."
    }
    return $script:RuntimePythonw
}

function Start-CaveViewerApp {
    if ([string]::IsNullOrWhiteSpace($script:RuntimePython)) {
        throw "CaveViewer's verified runtime has not been initialized."
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:RuntimePython
    $psi.Arguments = "-m caveviewer"
    $psi.WorkingDirectory = $ProjectRoot
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    if ($IoWorkers -gt 0) {
        $psi.EnvironmentVariables["CAVEVIEWER_IO_WORKERS"] = [string]$IoWorkers
    }

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    $outputHandler = {
        if (-not [string]::IsNullOrWhiteSpace($EventArgs.Data)) {
            Write-Log "[CaveViewer launch] $($EventArgs.Data)"
        }
    }
    Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action $outputHandler | Out-Null
    Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $outputHandler | Out-Null
    $proc.Start() | Out-Null
    $proc.BeginOutputReadLine()
    $proc.BeginErrorReadLine()
    return $proc
}

# -- Step functions -----------------------------------------------------------
# Each returns $true on success, $false on failure. The single Install
# button below runs them in order and stops at the first failure, rather
# than barreling ahead and pretending a failed step succeeded.

function Install-Python {
    $runtime = Install-SupportedPython
    if ($null -eq $runtime) {
        return $false
    }
    $script:SelectedPython = $runtime
    return $true
}

function Prepare-CaveViewerRuntime {
    if ($null -eq $script:SelectedPython) {
        Write-Log "ERROR: CaveViewer cannot create its runtime before Python 3.12 is selected."
        return $false
    }
    return Initialize-CaveViewerRuntime -PythonRuntime $script:SelectedPython
}

function Install-Requirements {
    Write-Log "Installing required packages into CaveViewer's verified virtual environment..."
    if ([string]::IsNullOrWhiteSpace($script:RuntimePython)) {
        Write-Log "ERROR: CaveViewer's verified runtime is unavailable."
        return $false
    }
    if (-not (Test-Path -LiteralPath $RequirementsFile)) {
        Write-Log "ERROR: Could not find requirements.txt at:"
        Write-Log "  $RequirementsFile"
        Write-Log "Make sure this setup folder is still inside the CaveViewer project folder."
        return $false
    }

    $pipArguments = @(
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check"
    )
    if ($NonInteractive) {
        # The package smoke must not make network requests. It still proves
        # interpreter selection, venv creation, editable installation, and
        # an installed-module import using the exact production runtime.
        Write-Log "Noninteractive verification uses pip --no-deps; dependency downloads are intentionally disabled."
        $pipArguments += @("--no-deps", "--no-build-isolation")
    } else {
        $pipArguments += @("-r", $RequirementsFile)
    }
    $pipArguments += @("-e", $ProjectRoot)
    $pipResult = Invoke-CaveViewerCommand -FilePath $script:RuntimePython -ArgumentList $pipArguments -Description "CaveViewer dependency installation"
    if (-not $pipResult.Succeeded) {
        Write-Log "ERROR: pip install failed; exit code $($pipResult.ExitCode)."
        return $false
    }

    return Test-CaveViewerInstallation
}

function Test-CaveViewerInstallation {
    if ([string]::IsNullOrWhiteSpace($script:RuntimePython)) {
        Write-Log "ERROR: CaveViewer's verified runtime is unavailable for installation verification."
        return $false
    }

    # This checks the editable installation through the same explicit virtual
    # environment that the shortcut will use.  It imports no GUI or GPU code.
    $verifyCode = "import caveviewer; from caveviewer.version import APP_NAME, APP_VERSION; print(APP_NAME + ' ' + APP_VERSION)"
    $verifyResult = Invoke-CaveViewerCommand -FilePath $script:RuntimePython -ArgumentList @("-c", $verifyCode) -Description "CaveViewer installed-module verification"
    if (-not $verifyResult.Succeeded) {
        Write-Log "ERROR: CaveViewer could not be imported from its verified virtual environment."
        return $false
    }

    Write-Log "Verified CaveViewer module with $script:RuntimePython."
    return $true
}

function ConvertTo-PowerShellSingleQuotedLiteral {
    param([AllowEmptyString()][string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

function ConvertTo-ShortcutFileArgument {
    param([string]$Path)

    if ($Path.Contains('"')) {
        throw "Windows paths cannot contain a double quote."
    }
    return '"' + $Path + '"'
}

function New-CaveViewerLauncher {
    if ([string]::IsNullOrWhiteSpace($script:RuntimePython) -or [string]::IsNullOrWhiteSpace($script:RuntimeDirectory)) {
        throw "CaveViewer's verified runtime has not been initialized."
    }

    $launcherPath = Join-Path $script:RuntimeDirectory "launch-caveviewer.ps1"
    $launcherLines = @(
        '$ErrorActionPreference = ''Stop''',
        ('$python = {0}' -f (ConvertTo-PowerShellSingleQuotedLiteral $script:RuntimePython)),
        ('$projectRoot = {0}' -f (ConvertTo-PowerShellSingleQuotedLiteral $ProjectRoot)),
        ('$launchLogPath = {0}' -f (ConvertTo-PowerShellSingleQuotedLiteral $SetupLogPath)),
        'try {',
        '    New-Item -ItemType Directory -Path (Split-Path -Parent $launchLogPath) -Force | Out-Null',
        '    Set-Location -LiteralPath $projectRoot'
    )
    if ($IoWorkers -gt 0) {
        $launcherLines += ('$env:CAVEVIEWER_IO_WORKERS = {0}' -f (ConvertTo-PowerShellSingleQuotedLiteral ([string]$IoWorkers)))
    }
    $launcherLines += @(
        '    & $python -m caveviewer *>> $launchLogPath',
        '    $exitCode = $LASTEXITCODE',
        '    if ($exitCode -ne 0) {',
        '        Add-Content -LiteralPath $launchLogPath -Value ("[$(Get-Date -Format ''s'')] CaveViewer exited with code $exitCode.") -Encoding UTF8',
        '    }',
        '    exit $exitCode',
        '} catch {',
        '    Add-Content -LiteralPath $launchLogPath -Value ("[$(Get-Date -Format ''s'')] CaveViewer launch failed: $($_.Exception.Message)") -Encoding UTF8',
        '    exit 1',
        '}'
    )
    [System.IO.File]::WriteAllText(
        $launcherPath,
        ($launcherLines -join [System.Environment]::NewLine) + [System.Environment]::NewLine,
        [System.Text.Encoding]::UTF8
    )
    $script:LaunchScriptPath = $launcherPath
    return $launcherPath
}

function New-DesktopShortcut {
    Write-Log "Creating a CaveViewer shortcut with its verified runtime..."

    try {
        $desktopPath = if ([string]::IsNullOrWhiteSpace($ShortcutDirectory)) {
            [System.Environment]::GetFolderPath("Desktop")
        } else {
            [System.IO.Path]::GetFullPath($ShortcutDirectory)
        }
        New-Item -ItemType Directory -Path $desktopPath -Force -ErrorAction Stop | Out-Null
        $shortcutPath = Join-Path $desktopPath "CaveViewer.lnk"
        $launcherPath = New-CaveViewerLauncher

        $powerShellPath = Join-Path $PSHOME "powershell.exe"
        if (-not (Test-Path -LiteralPath $powerShellPath)) {
            throw "Could not find PowerShell at '$powerShellPath'."
        }

        $iconPath = Join-Path $ScriptDir "icon\caveviewer.ico"
        $stableIconDir = Join-Path $CaveViewerDataRoot "icons"
        $stableIconPath = Join-Path $stableIconDir "caveviewer-$SafeAppVersion.ico"

        $wshShell = New-Object -ComObject WScript.Shell
        $shortcut = $wshShell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $powerShellPath
        $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $(ConvertTo-ShortcutFileArgument $launcherPath)"
        $shortcut.WorkingDirectory = $ProjectRoot
        $shortcut.Description = "Launch CaveViewer using its verified Python 3.12 runtime"

        if (Test-Path -LiteralPath $iconPath) {
            New-Item -ItemType Directory -Path $stableIconDir -Force -ErrorAction Stop | Out-Null
            Copy-Item -LiteralPath $iconPath -Destination $stableIconPath -Force
            $shortcut.IconLocation = "$stableIconPath,0"
            Write-Log "Using user-owned CaveViewer icon: $stableIconPath"
        } else {
            $shortcut.IconLocation = "$(Resolve-PythonGuiPath),0"
            Write-Log "Custom icon not found at $iconPath -- using the verified runtime icon instead."
        }

        $shortcut.Save()
        Write-Log "Shortcut created: $shortcutPath"
        Write-Log "Shortcut launcher: $launcherPath"
        return $true
    } catch {
        Write-Log "ERROR: Failed to create the desktop shortcut: $($_.Exception.Message)"
        return $false
    }
}

# -- Install workflow ---------------------------------------------------------

function Invoke-CaveViewerInstall {
    # Shared by the Install button, noninteractive package smoke, and the
    # updater's automatic handoff.  Every path uses the same explicit runtime.
    $btnInstall.Enabled = $false
    $btnInstall.Text = "Installing..."
    foreach ($stepName in $stepNames) {
        Set-StepState $stepName "pending"
    }
    Write-Log "Setup log: $SetupLogPath"
    Write-Log "Project root: $ProjectRoot"
    Write-Log "User-owned runtime root: $(if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) { Join-Path $CaveViewerDataRoot 'runtime\python312' } else { $RuntimeRoot })"

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
        return $false
    }
    Set-StepState "Python" "done"

    Set-SetupStatus "Preparing CaveViewer runtime..." 28
    Set-StepState "CaveViewer runtime" "running"
    $ok = Prepare-CaveViewerRuntime
    if (-not $ok) {
        Set-StepState "CaveViewer runtime" "failed"
        Set-SetupStatus "Runtime preparation failed" 28
        Write-Log ""
        Write-Log "Setup stopped -- CaveViewer's verified runtime could not be prepared. Click Install to try again."
        $btnInstall.Enabled = $true
        $btnInstall.Text = "Install"
        return $false
    }
    Set-StepState "CaveViewer runtime" "done"

    Set-SetupStatus "Installing Python libraries..." 55
    Set-StepState "Python libraries" "running"
    $ok = Install-Requirements
    if (-not $ok) {
        Set-StepState "Python libraries" "failed"
        Set-SetupStatus "Library installation failed" 55
        Write-Log ""
        Write-Log "Setup stopped -- installing requirements failed. Click Install to try again."
        $btnInstall.Enabled = $true
        $btnInstall.Text = "Install"
        return $false
    }
    Set-StepState "Python libraries" "done"

    Set-SetupStatus "Creating Desktop shortcut..." 84
    Set-StepState "Desktop shortcut" "running"
    $ok = New-DesktopShortcut
    if (-not $ok) {
        Set-StepState "Desktop shortcut" "failed"
        Set-SetupStatus "Shortcut creation failed" 84
        Write-Log ""
        Write-Log "Setup stopped -- could not create the desktop shortcut. Click Install to try again."
        $btnInstall.Enabled = $true
        $btnInstall.Text = "Install"
        return $false
    }
    Set-StepState "Desktop shortcut" "done"

    Write-Log ""
    Write-Log "All done!"
    Set-SetupStatus "CaveViewer is ready" 100
    $btnInstall.Text = "Done"
    $btnInstall.Enabled = $false
    return $true
}

$btnInstall.Add_Click({
    if (Invoke-CaveViewerInstall) {
        Show-InstallCompleteDialog
    }
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

function Start-AutomaticUpdateInstall {
    # The verified-update handoff selected this mode only after an explicit
    # in-app click.  It keeps setup visible and leaves it open on any failure.
    if (-not (Invoke-CaveViewerInstall)) {
        return
    }

    try {
        Write-Log "Automatic update installation finished; launching CaveViewer."
        $proc = Start-CaveViewerApp
        Start-Sleep -Milliseconds 1200
        if ($proc -and $proc.HasExited) {
            Write-Log "WARNING: CaveViewer exited immediately after automatic update launch (exit code $($proc.ExitCode))."
            Write-Log "The setup window will stay open so you can review the log or launch from the Desktop shortcut."
            $btnInstall.Enabled = $true
            $btnInstall.Text = "Install"
            return
        }
        $form.Close()
    } catch {
        Write-Log "WARNING: Could not launch CaveViewer after the automatic update: $($_.Exception.Message)"
        Write-Log "The setup window will stay open so you can review the log or launch from the Desktop shortcut."
        $btnInstall.Enabled = $true
        $btnInstall.Text = "Install"
    }
}

Write-Log "Welcome to CaveViewer Setup."
Write-Log "Setup log is retained at: $SetupLogPath"
if ($IoWorkers -gt 0) {
    Write-Log "Runtime worker override enabled: CAVEVIEWER_IO_WORKERS=$IoWorkers"
}
Write-Log "CaveViewer is licensed under the GNU General Public License v3.0."
Write-Log "License files are included with this setup folder: LICENSE and THIRD_PARTY_NOTICES.md."
Write-Log "Setup does not change system Python, PATH, firewall, or administrator-owned locations."

if ($NonInteractive) {
    Write-Log "Noninteractive installer verification requested."
    if (Invoke-CaveViewerInstall) {
        Write-Log "Noninteractive installer verification completed successfully."
        exit 0
    }
    Write-Log "Noninteractive installer verification failed. See the setup log above."
    exit 1
}

if ($AutoInstall) {
    Write-Log "Automatic update installation requested."
    $form.Add_Shown({
        Start-AutomaticUpdateInstall
    })
} else {
    Write-Log "Click Install to set up Python, the required libraries, and a Desktop shortcut."
}

[void]$form.ShowDialog()
