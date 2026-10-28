param([string]$Destination = "")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Destination) { $Destination = Join-Path $projectRoot 'build\tools\inno' }
$compiler = Join-Path $Destination 'ISCC.exe'
if (Test-Path -LiteralPath $compiler -PathType Leaf) { Write-Output $compiler; exit 0 }
$version = '7.1.0'
$expectedSha = '0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f'
$downloadRoot = Join-Path $projectRoot 'build\installer-tools'
New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null
$package = Join-Path $downloadRoot 'innosetup-7.1.0-x64.exe'
if (-not (Test-Path -LiteralPath $package -PathType Leaf)) {
    Invoke-WebRequest -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe' -OutFile $package
}
if ((Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedSha) {
    throw 'Inno Setup compiler download checksum mismatch; refusing execution'
}
$signature = Get-AuthenticodeSignature -LiteralPath $package
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Pyrsys B\.V\.') {
    throw 'Inno Setup compiler publisher signature could not be verified'
}
$installArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/NOICONS', '/TASKS=', ('/DIR="' + $Destination + '"'))
$process = Start-Process -FilePath $package -ArgumentList $installArguments -PassThru -Wait -WindowStyle Hidden
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw 'Could not prepare the isolated Inno Setup compiler'
}
Write-Output $compiler
