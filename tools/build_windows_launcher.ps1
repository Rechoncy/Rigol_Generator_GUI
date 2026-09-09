$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $compiler)) { throw "Windows .NET Framework 4 compiler not found" }
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ /reference:System.Windows.Forms.dll `
    "/win32icon:$projectRoot\src\rigol_control\assets\app_icon.ico" `
    "/out:$projectRoot\Lab Control Center.exe" "$PSScriptRoot\windows_launcher.cs"
if ($LASTEXITCODE -ne 0) { throw "Launcher build failed" }
Write-Output (Join-Path $projectRoot "Lab Control Center.exe")
