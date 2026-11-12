#Requires -Version 5.1
<#
Build dist\paw\paw.exe, dist\paw-runtime.zip and dist\runtimes.zip using only this checkout.
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-paw.ps1
Use -SkipFrontend only when console\dist already matches the current source.
Use -Clean to clear the PyInstaller analysis cache.
Use -AllowExtraPackages to reuse a venv with additional packages; locked versions are still checked.
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$Clean,
    [switch]$AllowExtraPackages
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$BuildDir = Join-Path $RepoRoot 'build\pyinstaller'
$DistDir = Join-Path $RepoRoot 'dist'
$SavedBuildEnvironment = @{}

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed (exit code $LASTEXITCODE)." }
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows.' }
    $RequiredPythonVersion = (Get-Content -LiteralPath (Join-Path $RepoRoot '.python-version') -Raw).Trim()
    if ($RequiredPythonVersion -notmatch '^\d+\.\d+\.\d+$') { throw '.python-version must specify an exact version, such as 3.11.9.' }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Run scripts\init-windows.ps1 first to create .venv.' }
    foreach ($file in @('paw.spec', 'main.py', 'src\qwenpaw\agents\bundled_workspaces.zip', 'scripts\native-components.py', 'scripts\prepare-bundled-runtimes.py', 'scripts\package-paw-runtime.py', 'scripts\bundled-runtimes.lock.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $file) -PathType Leaf)) { throw "Missing project file: $file" }
    }
    # PyInstaller replaces dist/paw. Never discard data from an app that was
    # previously run directly in that directory. Empty bootstrap locks are safe.
    foreach ($name in @('.devco_paw', '.devco_paw.secret', '.qwenpaw', '.qwenpaw.secret')) {
        $DataDir = Join-Path (Join-Path $DistDir 'paw') $name
        if (Test-Path -LiteralPath $DataDir -PathType Container) {
            $DataFile = Get-ChildItem -LiteralPath $DataDir -Recurse -File -Force |
                Where-Object { $_.Extension -ne '.lock' } | Select-Object -First 1
            if ($DataFile) { throw "Runtime data exists in $DataDir. Back it up outside dist before rebuilding." }
        }
    }
    # Package imports should use disposable build data rather than a user's
    # existing ~/.qwenpaw configuration. Restore the caller's environment later.
    foreach ($name in @('QWENPAW_WORKING_DIR', 'COPAW_WORKING_DIR', 'QWENPAW_SECRET_DIR', 'COPAW_SECRET_DIR')) {
        $SavedBuildEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        $suffix = if ($name.EndsWith('SECRET_DIR')) { 'packaging-data.secret' } else { 'packaging-data' }
        [Environment]::SetEnvironmentVariable($name, (Join-Path $RepoRoot "build\$suffix"), 'Process')
    }
    # Import-time discovery must not scan the builder's personal SSH/config
    # directories. Keep the subprocess user profile disposable as well.
    $BuildProfile = Join-Path $RepoRoot 'build\packaging-home'
    New-Item -ItemType Directory -Force -Path $BuildProfile | Out-Null
    foreach ($name in @('USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA')) {
        $SavedBuildEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        $ProfilePath = if ($name -in @('APPDATA', 'LOCALAPPDATA')) { Join-Path $BuildProfile $name.ToLowerInvariant() } else { $BuildProfile }
        New-Item -ItemType Directory -Force -Path $ProfilePath | Out-Null
        [Environment]::SetEnvironmentVariable($name, $ProfilePath, 'Process')
    }
    Push-Location $RepoRoot
    try {
        Invoke-Checked $Python @('-B', '-c', 'import sys,struct; required=sys.argv[1]; actual=''.''.join(map(str,sys.version_info[:3])); assert sys.platform == ''win32'' and struct.calcsize(''P'') == 8; assert actual == required, f''Python {required} is required; found {actual}. Run scripts/init-windows.ps1 -RecreateVenv.''; import PyInstaller, pkg_resources, reme; print(''Python '' + actual + '', PyInstaller '' + PyInstaller.__version__)', $RequiredPythonVersion)
        $EnvironmentCheckArguments = @('-B', 'scripts\check-windows-env.py')
        if ($AllowExtraPackages) { $EnvironmentCheckArguments += '--allow-extra-packages' }
        Invoke-Checked $Python $EnvironmentCheckArguments
        Invoke-Checked $Python @('-B', 'scripts\native-components.py', '--verify')
        # Refresh editable package metadata without fetching/upgrading dependencies.
        Invoke-Checked $Python @('-m', 'pip', 'install', '--no-deps', '-e', '.')
        if (-not $SkipFrontend) {
            $Npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
            if (-not $Npm) { throw 'npm.cmd is missing. Install Node.js and run scripts\init-windows.ps1.' }
            if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'console\node_modules\typescript\bin\tsc'))) {
                throw 'Frontend dependencies are missing. Run scripts\init-windows.ps1 first.'
            }
            Push-Location (Join-Path $RepoRoot 'console')
            try { Invoke-Checked $Npm.Source @('run', 'build:prod') }
            finally { Pop-Location }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'console\dist\index.html') -PathType Leaf)) {
            throw 'console/dist/index.html is missing; a complete frontend build is required.'
        }
        # Cache and temporary files stay inside this checkout.
        New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
        $PreviousConfigDir = $env:PYINSTALLER_CONFIG_DIR
        $env:PYINSTALLER_CONFIG_DIR = Join-Path $RepoRoot 'build\pyinstaller-cache'
        try {
            $Arguments = @('-m', 'PyInstaller', '--noconfirm', '--workpath', $BuildDir, '--distpath', $DistDir)
            if ($Clean) { $Arguments += '--clean' }
            $Arguments += (Join-Path $RepoRoot 'paw.spec')
            Invoke-Checked $Python $Arguments
        } finally { $env:PYINSTALLER_CONFIG_DIR = $PreviousConfigDir }

        $RuntimeDir = Join-Path $DistDir 'paw'
        $Exe = Join-Path $RuntimeDir 'paw.exe'
        foreach ($relative in @('paw.exe', '_internal\qwenpaw\console\index.html', '_internal\qwenpaw\agents\bundled_workspaces.zip')) {
            if (-not (Test-Path -LiteralPath (Join-Path $RuntimeDir $relative) -PathType Leaf)) { throw "Incomplete build: $relative is missing." }
        }
        Invoke-Checked $Python @('-B', 'scripts\native-components.py', '--audit-frozen', $RuntimeDir)
        # Ship independent portable tools alongside the frozen application.
        # The lock pins official download URLs and SHA256; this also validates
        # venv/pip, npm/npx, Git and Bash with no host runtimes on PATH.
        Invoke-Checked $Python @('-B', 'scripts\prepare-bundled-runtimes.py', '--output', (Join-Path $RuntimeDir 'runtimes'))
        # Exercise the frozen CLI without initializing agents or starting a service.
        Invoke-Checked $Exe @('cli', '--help')
        Copy-Item -LiteralPath (Join-Path $RepoRoot 'start-paw.cmd') -Destination $DistDir -Force
        Copy-Item -LiteralPath (Join-Path $RepoRoot 'stop-paw.cmd') -Destination $DistDir -Force

        # Keep the application and portable tools independently deployable.
        # The app ZIP contains only paw.exe and _internal/. The tools ZIP has
        # manifest.json, python/, node/ and git/ at its root.
        Invoke-Checked $Python @('-B', 'scripts\package-paw-runtime.py', '--app-dir', $RuntimeDir, '--output', (Join-Path $DistDir 'paw-runtime.zip'), '--runtimes-output', (Join-Path $DistDir 'runtimes.zip'))
        Write-Host "Executable: $Exe" -ForegroundColor Green
        Write-Host "Release: $(Join-Path $DistDir 'paw-runtime.zip') + start-paw.cmd + stop-paw.cmd"
        Write-Host "Portable tools: $(Join-Path $DistDir 'runtimes.zip')"
        Write-Host 'Verify runtimes.zip.sha256 and extract runtimes.zip into %USERPROFILE%\.devco\bin\runtime before enabling bundled tools.'
    } finally { Pop-Location }
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
} finally {
    foreach ($name in $SavedBuildEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $SavedBuildEnvironment[$name], 'Process')
    }
}
