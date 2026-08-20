[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$ArtifactPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CertificateSubject,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$TimestampUrl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$artifact = (Resolve-Path -LiteralPath $ArtifactPath).Path
$supportedExtensions = @('.exe', '.dll', '.pyd')
if ([IO.Path]::GetExtension($artifact).ToLowerInvariant() -notin $supportedExtensions) {
    throw "Refusing to sign an unsupported artifact type: $artifact"
}

$matchingCertificates = @(
    Get-ChildItem -Path 'Cert:\CurrentUser\My' |
        Where-Object {
            $_.HasPrivateKey -and $_.Subject -eq $CertificateSubject
        }
)
if ($matchingCertificates.Count -ne 1) {
    throw (
        "Expected exactly one current-user code-signing certificate with subject " +
        "'$CertificateSubject'; found $($matchingCertificates.Count)."
    )
}

$signTool = Get-Command -Name 'signtool.exe' -ErrorAction SilentlyContinue
if ($null -eq $signTool) {
    $signTool = Get-Command -Name 'signtool' -ErrorAction SilentlyContinue
}
if ($null -eq $signTool) {
    throw 'signtool.exe is required to sign CaveViewer Windows artifacts.'
}

$certificate = $matchingCertificates[0]
$signArguments = @(
    'sign',
    '/fd', 'SHA256',
    '/sha1', $certificate.Thumbprint,
    '/tr', $TimestampUrl,
    '/td', 'SHA256',
    '/d', 'CaveViewer',
    '/du', 'https://github.com/CaveViewer/CaveViewer',
    $artifact
)
& $signTool.Source @signArguments
if ($LASTEXITCODE -ne 0) {
    throw "signtool sign failed with exit code $LASTEXITCODE: $artifact"
}

& $signTool.Source verify /pa /tw $artifact
if ($LASTEXITCODE -ne 0) {
    throw "signtool verify failed with exit code $LASTEXITCODE: $artifact"
}
