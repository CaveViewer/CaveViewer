[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$ArtifactPath,

    [Parameter()]
    [string]$ExpectedCertificateSubject
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$artifact = (Resolve-Path -LiteralPath $ArtifactPath).Path
$signature = Get-AuthenticodeSignature -LiteralPath $artifact
if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Authenticode validation failed for $artifact: $($signature.Status) $($signature.StatusMessage)"
}
if (
    $ExpectedCertificateSubject -and
    $signature.SignerCertificate.Subject -ne $ExpectedCertificateSubject
) {
    throw (
        "Unexpected Authenticode signer for $artifact: " +
        "'$($signature.SignerCertificate.Subject)' (expected '$ExpectedCertificateSubject')."
    )
}

$signTool = Get-Command -Name 'signtool.exe' -ErrorAction SilentlyContinue
if ($null -eq $signTool) {
    $signTool = Get-Command -Name 'signtool' -ErrorAction SilentlyContinue
}
if ($null -eq $signTool) {
    throw 'signtool.exe is required to verify CaveViewer Windows artifacts.'
}

& $signTool.Source verify /pa /tw $artifact
if ($LASTEXITCODE -ne 0) {
    throw "signtool verify failed with exit code $LASTEXITCODE: $artifact"
}
