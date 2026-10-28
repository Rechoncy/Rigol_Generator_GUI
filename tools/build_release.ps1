param(
    [string]$Python = "python",
    [switch]$SkipInstall,
    [string]$VaunixDll = ""
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$buildPython = Join-Path $projectRoot "build\release-venv\Scripts\python.exe"
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath $buildPython)) {
        & $Python -c "import pathlib,sys; assert not (pathlib.Path(sys.base_prefix)/'conda-meta').exists(), 'Use official CPython with -Python; Conda DLL search paths can shadow Windows ICU used by Qt'"
        if ($LASTEXITCODE -ne 0) { throw "Select a 64-bit official CPython interpreter with -Python" }
        & $Python -m venv (Join-Path $projectRoot "build\release-venv")
        if ($LASTEXITCODE -ne 0) { throw "Could not create isolated release environment" }
    }
    & $buildPython -c "import pathlib,sys; assert not (pathlib.Path(sys.base_prefix)/'conda-meta').exists(), 'Existing release venv uses Conda; move it aside and rebuild using official CPython'"
    if ($LASTEXITCODE -ne 0) { throw "The release environment must use official CPython" }
    if (-not $SkipInstall) {
        & $buildPython -m pip install --disable-pip-version-check -r "release\requirements-build.txt"
        if ($LASTEXITCODE -ne 0) { throw "Release dependency installation failed" }
        & $buildPython -m pip install --disable-pip-version-check --no-deps --no-build-isolation -e .
        if ($LASTEXITCODE -ne 0) { throw "Project installation failed" }
    }
    & (Join-Path $PSScriptRoot 'ensure_inno_setup.ps1')
    if ($LASTEXITCODE -ne 0) { throw "Installer compiler preparation failed" }
    $buildArguments = @("tools\build_release.py")
    if ($VaunixDll) { $buildArguments += @("--vaunix-dll", $VaunixDll) }
    & $buildPython @buildArguments
    if ($LASTEXITCODE -ne 0) { throw "Release build or packaged application verification failed" }
} finally {
    Pop-Location
}
