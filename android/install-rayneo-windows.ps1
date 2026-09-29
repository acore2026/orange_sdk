param(
    [string]$AdbPath = "C:\Android\platform-tools\adb.exe",
    [string]$ApkPath = "",
    [switch]$RequireVpnConsent
)

$ErrorActionPreference = "Stop"
$PackageName = "com.rayneo.agent.example.rayneo"
$ActivityName = "com.rayneo.agent.example.RayNeoMainActivity"

if ([string]::IsNullOrWhiteSpace($ApkPath)) {
    $ApkPath = Join-Path `
        $PSScriptRoot `
        "example-app\build\outputs\apk\rayneo\release\example-app-rayneo-release.apk"
}

if (-not (Test-Path -LiteralPath $AdbPath)) {
    throw "adb.exe not found: $AdbPath"
}
if (-not (Test-Path -LiteralPath $ApkPath)) {
    throw "RayNeo APK not found: $ApkPath"
}

$deviceLines = & $AdbPath devices |
    Select-Object -Skip 1 |
    Where-Object { $_ -match "\sdevice$" }
if ($deviceLines.Count -ne 1) {
    throw "Expected exactly one authorized ADB device, found $($deviceLines.Count)."
}

Write-Host "Installing $ApkPath"
& $AdbPath install -r $ApkPath
if ($LASTEXITCODE -ne 0) {
    throw "APK installation failed."
}

if (-not $RequireVpnConsent) {
    Write-Host "Pre-authorizing VPN through ADB."
    & $AdbPath shell appops set $PackageName ACTIVATE_VPN allow
    if ($LASTEXITCODE -ne 0) {
        throw "ADB VPN pre-authorization failed."
    }
    $vpnAppOp = & $AdbPath shell appops get $PackageName ACTIVATE_VPN
    if ($LASTEXITCODE -ne 0 -or ($vpnAppOp -join " ") -notmatch "ACTIVATE_VPN:\s*allow\b") {
        throw "ADB VPN pre-authorization was not confirmed: $($vpnAppOp -join ' ')"
    }
    Write-Host ($vpnAppOp -join " ")
}

& $AdbPath shell am force-stop $PackageName
& $AdbPath shell am start -n "$PackageName/$ActivityName"
if ($LASTEXITCODE -ne 0) {
    throw "RayNeo Agent A launch failed."
}

if (-not $RequireVpnConsent) {
    Write-Host "App started with VPN pre-authorized; secure networking starts automatically."
} else {
    Write-Host "App started; approve Android's VPN dialog once if it appears."
}
