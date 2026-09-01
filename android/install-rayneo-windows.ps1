param(
    [string]$AdbPath = "C:\Android\platform-tools\adb.exe",
    [string]$ApkPath = "",
    [switch]$PreAuthorizeVpn
)

$ErrorActionPreference = "Stop"
$PackageName = "com.rayneo.agent.example.rayneo"
$ActivityName = "com.rayneo.agent.example.RayNeoMainActivity"

if ([string]::IsNullOrWhiteSpace($ApkPath)) {
    $ApkPath = Join-Path `
        $PSScriptRoot `
        "example-app\build\outputs\apk\rayneo\debug\example-app-rayneo-debug.apk"
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

if ($PreAuthorizeVpn) {
    Write-Warning "Pre-authorizing VPN through ADB (internal test / managed-device use only)."
    & $AdbPath shell appops set $PackageName ACTIVATE_VPN allow
    if ($LASTEXITCODE -ne 0) {
        throw "ADB VPN pre-authorization failed."
    }
    & $AdbPath shell appops get $PackageName ACTIVATE_VPN
}

& $AdbPath shell am force-stop $PackageName
& $AdbPath shell am start -n "$PackageName/$ActivityName"
if ($LASTEXITCODE -ne 0) {
    throw "RayNeo Agent A launch failed."
}

if ($PreAuthorizeVpn) {
    Write-Host "App started with VPN pre-authorized; click Enable Agent Network to connect."
} else {
    Write-Host "App started; click Enable Agent Network and approve Android's VPN dialog once."
}
