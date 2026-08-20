[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$InstallerPath,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$MetadataPath,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$UpdateMetadataPath,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedVersion,

    [string]$ExpectedCertificateSubject = "",

    [switch]$AllowUnsigned
)

# Native Windows installer smoke coverage. This script deliberately keeps every
# filesystem location as a PowerShell path and invokes the installer with an
# argument array, so spaces, Unicode, apostrophes, and ampersands are preserved
# rather than interpolated into a command line.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$InstallerProcessWaitMilliseconds = 120000

function Assert-Condition {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Get-ArtifactDigest {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Assert-PackageMetadata {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$PackageMetadataPath,
        [Parameter(Mandatory = $true)][string]$UpdatePath,
        [Parameter(Mandatory = $true)][string]$Version,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$CertificateSubject,
        [Parameter(Mandatory = $true)][bool]$UnsignedAllowed
    )

    $packageMetadata = Get-Content -LiteralPath $PackageMetadataPath -Raw | ConvertFrom-Json
    $updateMetadata = Get-Content -LiteralPath $UpdatePath -Raw | ConvertFrom-Json
    $item = Get-Item -LiteralPath $Path
    $digest = Get-ArtifactDigest -Path $Path

    Assert-Condition ($packageMetadata.artifact_file -eq $item.Name) "Package metadata names a different installer."
    Assert-Condition ($packageMetadata.package_type -eq "windows_signed_installer") "Package metadata has the wrong package type."
    Assert-Condition ([int64]$packageMetadata.size_bytes -eq [int64]$item.Length) "Package metadata has the wrong installer size."
    Assert-Condition ($packageMetadata.sha256 -eq $digest) "Package metadata has the wrong installer SHA-256."
    Assert-Condition ($updateMetadata.latest_version -eq $Version) "Update metadata has the wrong version."
    Assert-Condition ([int64]$updateMetadata.download_size_bytes_windows_exe -eq [int64]$item.Length) "Update metadata has the wrong EXE size."
    Assert-Condition ($updateMetadata.sha256_windows_exe -eq $digest) "Update metadata has the wrong EXE SHA-256."
    Assert-Condition ($updateMetadata.install_channel -eq "windows_installer") "Update metadata has the wrong install channel."

    if ($UnsignedAllowed) {
        Assert-Condition ($packageMetadata.authenticode_status -eq "unsigned-test-only") "Unsigned smoke metadata must be marked unsigned-test-only."
        Assert-Condition ($updateMetadata.authenticode_status -eq "unsigned-test-only") "Unsigned update metadata must be marked unsigned-test-only."
        return
    }

    Assert-Condition ($packageMetadata.authenticode_status -eq "verified") "Release package metadata is not signed and verified."
    Assert-Condition ($updateMetadata.authenticode_status -eq "verified") "Release update metadata is not signed and verified."
    Assert-Condition ($packageMetadata.authenticode_certificate_subject -eq $CertificateSubject) "Package metadata has the wrong Authenticode publisher."
    Assert-Condition ($updateMetadata.authenticode_certificate_subject -eq $CertificateSubject) "Update metadata has the wrong Authenticode publisher."
}

function Assert-InstallerSignature {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$CertificateSubject,
        [Parameter(Mandatory = $true)][bool]$UnsignedAllowed
    )

    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($UnsignedAllowed) {
        Assert-Condition ($signature.Status.ToString() -eq "NotSigned") "Unsigned smoke installer unexpectedly has an Authenticode status of $($signature.Status)."
        return
    }

    Assert-Condition ($signature.Status.ToString() -eq "Valid") "Installer Authenticode status is $($signature.Status), not Valid."
    Assert-Condition ($null -ne $signature.SignerCertificate) "Installer has no Authenticode signer certificate."
    Assert-Condition ($signature.SignerCertificate.Subject -ceq $CertificateSubject) "Installer Authenticode publisher does not match the expected subject."
    Assert-Condition ($null -ne $signature.TimeStamperCertificate) "Installer Authenticode signature has no RFC-3161 timestamp."
}

function ConvertTo-WindowsCommandLineArgument {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Argument
    )

    if ($Argument.Length -eq 0) {
        return '""'
    }
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }

    # Start-Process accepts a single command-line string on Windows PowerShell.
    # Escape quotes and trailing backslashes according to Windows argv rules so
    # installer paths containing spaces, apostrophes, or ampersands stay intact.
    $quoted = $Argument -replace '(\\*)"', '$1$1\"'
    $quoted = $quoted -replace '(\\*)$', '$1$1'
    return '"' + $quoted + '"'
}

function Invoke-CaveViewerInstaller {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $commandLine = (@($Arguments | ForEach-Object {
        ConvertTo-WindowsCommandLineArgument -Argument $_
    }) -join " ")
    $process = Start-Process -FilePath $Path -ArgumentList $commandLine -PassThru
    if (-not $process.WaitForExit($InstallerProcessWaitMilliseconds)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "$Description did not finish within two minutes."
    }
    if ($process.ExitCode -ne 0) {
        throw "$Description failed with installer exit code $($process.ExitCode)."
    }
}

function Restore-InstallationMarker {
    param(
        [Parameter(Mandatory = $true)][string]$RegistryPath,
        [Parameter(Mandatory = $true)][bool]$Existed,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )

    $names = @("Channel", "PayloadDirectory", "Version")
    if (-not $Existed) {
        Remove-Item -LiteralPath $RegistryPath -Recurse -Force -ErrorAction SilentlyContinue
        return
    }

    New-Item -Path $RegistryPath -Force | Out-Null
    foreach ($name in $names) {
        if ($Values.ContainsKey($name)) {
            New-ItemProperty -LiteralPath $RegistryPath -Name $name -Value $Values[$name] -PropertyType String -Force | Out-Null
        }
        else {
            Remove-ItemProperty -LiteralPath $RegistryPath -Name $name -ErrorAction SilentlyContinue
        }
    }
}

if (-not $AllowUnsigned -and [string]::IsNullOrWhiteSpace($ExpectedCertificateSubject)) {
    throw "-ExpectedCertificateSubject is required for a signed release smoke test."
}

$installerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
$metadataPath = (Resolve-Path -LiteralPath $MetadataPath).Path
$updateMetadataPath = (Resolve-Path -LiteralPath $UpdateMetadataPath).Path
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("CaveViewer smoke & café O'Brien " + [Guid]::NewGuid().ToString("N"))
$installRoot = Join-Path $temporaryRoot "CaveViewer install & café O'Brien"
$initialLogPath = Join-Path $temporaryRoot "initial install & café O'Brien.log"
$updateLogPath = Join-Path $temporaryRoot "update install & café O'Brien.log"
$markerPath = "HKCU:\Software\CaveViewer\Installation"
$markerExisted = Test-Path -LiteralPath $markerPath
$originalMarkerValues = @{}
$knownCaveViewerProcessIds = @(
    Get-Process -Name "CaveViewer" -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }
)
$waitProcess = $null

if ($markerExisted) {
    foreach ($name in @("Channel", "PayloadDirectory", "Version")) {
        try {
            $originalMarkerValues[$name] = Get-ItemPropertyValue -LiteralPath $markerPath -Name $name -ErrorAction Stop
        }
        catch {
            # The marker can predate this installer contract and omit a value.
        }
    }
}

try {
    New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
    Assert-PackageMetadata -Path $installerPath -PackageMetadataPath $metadataPath -UpdatePath $updateMetadataPath -Version $ExpectedVersion -CertificateSubject $ExpectedCertificateSubject -UnsignedAllowed ([bool]$AllowUnsigned)
    Assert-InstallerSignature -Path $installerPath -CertificateSubject $ExpectedCertificateSubject -UnsignedAllowed ([bool]$AllowUnsigned)

    Invoke-CaveViewerInstaller -Path $installerPath -Description "Initial installer verification" -Arguments @(
        "/SP-",
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DIR=$installRoot",
        "/LOG=$initialLogPath",
        "--verify-install"
    )

    $installedPayload = Join-Path $installRoot "app-$ExpectedVersion"
    $installedExecutable = Join-Path $installedPayload "CaveViewer.exe"
    Assert-Condition (Test-Path -LiteralPath $installedExecutable -PathType Leaf) "Initial installer did not produce the expected frozen executable."
    Assert-Condition (Test-Path -LiteralPath $initialLogPath -PathType Leaf) "Initial installer did not produce its log."

    $waitProcess = Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList @("/d", "/c", "ping -n 3 127.0.0.1 > nul") -WindowStyle Hidden -PassThru
    $updateStarted = Get-Date
    Invoke-CaveViewerInstaller -Path $installerPath -Description "Installer update handoff" -Arguments @(
        "/SP-",
        "/SILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DIR=$installRoot",
        "/LOG=$updateLogPath",
        "--update",
        "--wait-pid",
        "$($waitProcess.Id)",
        "--expected-version",
        $ExpectedVersion
    )
    Wait-Process -Id $waitProcess.Id -ErrorAction SilentlyContinue
    $waitProcess = $null
    Assert-Condition (((Get-Date) - $updateStarted).TotalSeconds -ge 1) "Installer update did not wait for the supplied parent process."
    Assert-Condition (Test-Path -LiteralPath $installedExecutable -PathType Leaf) "Update installer did not retain a complete frozen payload."
    Assert-Condition (Test-Path -LiteralPath $updateLogPath -PathType Leaf) "Update installer did not produce its log."

    $marker = Get-ItemProperty -LiteralPath $markerPath -ErrorAction Stop
    Assert-Condition ($marker.Channel -eq "windows_installer") "Installer did not register the Windows installer channel."
    Assert-Condition ($marker.Version -eq $ExpectedVersion) "Installer registered the wrong update version."
    Assert-Condition ([System.IO.Path]::GetFullPath($marker.PayloadDirectory) -ieq [System.IO.Path]::GetFullPath($installedPayload)) "Installer registered the wrong payload directory."
    Write-Host "Windows installer smoke passed: $installerPath"
}
finally {
    if ($null -ne $waitProcess -and -not $waitProcess.HasExited) {
        Stop-Process -Id $waitProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Get-Process -Name "CaveViewer" -ErrorAction SilentlyContinue |
        Where-Object { $knownCaveViewerProcessIds -notcontains $_.Id } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Restore-InstallationMarker -RegistryPath $markerPath -Existed $markerExisted -Values $originalMarkerValues
    Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}
