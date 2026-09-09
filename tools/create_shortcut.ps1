$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot "Lab Control Center.exe"
if (-not (Test-Path -LiteralPath $launcher)) {
    & (Join-Path $PSScriptRoot "build_windows_launcher.ps1")
}
Add-Type -Path (Join-Path $PSScriptRoot "windows_shortcut.cs")
$shortcutPath = Join-Path $projectRoot "Lab Control Center.lnk"
[LabControlShortcut]::Create($shortcutPath, $launcher)
Write-Output $shortcutPath
