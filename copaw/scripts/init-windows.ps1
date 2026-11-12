#Requires -Version 5.1
<#
Create a local Python virtual environment and install the backend/frontend.
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init-windows.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\init-windows.ps1 -PythonExe C:\Python311\python.exe
Use -InstallBrowser to install Playwright Chromium for browser-related tools.
Use -RecreateVenv to back up an existing environment and recreate it.
The exact Python version is read from .python-version (3.11.9).
#>
[CmdletBinding()]
param(
    [string]$PythonExe,
    [switch]$InstallBrowser,
    [switch]$RecreateVenv
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed (exit code $LASTEXITCODE)."
    }
}

function Assert-Python {
    param([string]$Executable)
    Invoke-Checked $Executable @('-B', '-c', 'import sys,struct; required=sys.argv[1]; actual=''.''.join(map(str,sys.version_info[:3])); assert sys.platform == ''win32'', ''Windows is required''; assert actual == required, f''Python {required} is required; found {actual}. Recreate .venv using scripts/init-windows.ps1 -RecreateVenv.''; assert struct.calcsize(''P'') == 8, ''64-bit Python is required''; print(sys.version)', $RequiredPythonVersion)
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows.' }
    foreach ($file in @('.python-version', 'pyproject.toml', 'requirements-build.txt', 'requirements-windows.lock.txt', 'scripts\check-windows-env.py', 'console\package-lock.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $file) -PathType Leaf)) {
            throw "Missing project file: $file"
        }
    }
    $RequiredPythonVersion = (Get-Content -LiteralPath (Join-Path $RepoRoot '.python-version') -Raw).Trim()
    if ($RequiredPythonVersion -notmatch '^\d+\.\d+\.\d+$') { throw '.python-version must specify an exact version, such as 3.11.9.' }
    $VersionParts = $RequiredPythonVersion.Split('.')
    $RequiredPythonSeries = "$($VersionParts[0]).$($VersionParts[1])"
    $PythonDirectory = "Python$($VersionParts[0])$($VersionParts[1])"
    $Node = Get-Command node.exe -ErrorAction SilentlyContinue
    $Npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $Node -or -not $Npm) { throw 'Install Node.js 22.12+ (64-bit) with npm, then run this script again.' }
    Invoke-Checked $Node.Source @('-e', 'const [major,minor]=process.versions.node.split(''.'').map(Number); if(major<22 || (major===22 && minor<12)){console.error(''Node.js 22.12+ is required''); process.exit(1)} console.log(''Node.js ''+process.versions.node)')

    $BasePython = $null
    if ($PythonExe) {
        $BasePython = (Get-Command $PythonExe -ErrorAction Stop).Source
        Assert-Python $BasePython
    }
    if ($RecreateVenv -or -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        if (-not $BasePython) {
            $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
            if ($Launcher) {
                $previousPreference = $ErrorActionPreference
                try {
                    $ErrorActionPreference = 'Continue'
                    $candidate = & $Launcher.Source "-$RequiredPythonSeries" -c 'import sys; print(sys.executable)' 2>$null
                } finally { $ErrorActionPreference = $previousPreference }
                if ($LASTEXITCODE -eq 0 -and $candidate) { $BasePython = [string]$candidate }
            }
            if (-not $BasePython) {
                $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\$PythonDirectory\python.exe"
                if (Test-Path -LiteralPath $candidate -PathType Leaf) { $BasePython = $candidate }
            }
            if (-not $BasePython) {
                $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
                if ($PythonCommand) { $BasePython = $PythonCommand.Source }
            }
            if (-not $BasePython) { throw "Install 64-bit Python $RequiredPythonVersion or pass -PythonExe with its path." }
        }
        Assert-Python $BasePython
        if (Test-Path -LiteralPath $VenvDir) {
            if (-not $RecreateVenv) { throw 'An incomplete .venv exists. Run this script with -RecreateVenv.' }
            # Validate both absolute paths before relocating the old environment.
            $ResolvedVenv = (Resolve-Path -LiteralPath $VenvDir).ProviderPath
            $WorkspacePrefix = [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\') + '\'
            $BackupRoot = [IO.Path]::GetFullPath((Join-Path $RepoRoot 'build\venv-backups'))
            $BackupDir = Join-Path $BackupRoot ("venv-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
            if (-not $ResolvedVenv.StartsWith($WorkspacePrefix, [StringComparison]::OrdinalIgnoreCase) -or
                -not $BackupDir.StartsWith($WorkspacePrefix, [StringComparison]::OrdinalIgnoreCase) -or
                ((Get-Item -LiteralPath $VenvDir -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Refusing to relocate a virtual environment outside this workspace or through a link.'
            }
            New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
            Move-Item -LiteralPath $ResolvedVenv -Destination $BackupDir
            Write-Host "Previous .venv backed up to $BackupDir"
        }
        Write-Host "Creating .venv with $BasePython"
        Invoke-Checked $BasePython @('-m', 'venv', (Join-Path $RepoRoot '.venv'))
    }
    Assert-Python $VenvPython
    Push-Location $RepoRoot
    try {
        # Also repair a virtual environment left without pip by an interrupted bootstrap.
        Invoke-Checked $VenvPython @('-m', 'ensurepip', '--upgrade')
        Invoke-Checked $VenvPython @('-m', 'pip', 'install', '-r', 'requirements-build.txt')
        # Restore every installed distribution version from the original venv.
        # The lock is complete, so dependency resolution must not upgrade it.
        Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--no-deps', '-r', 'requirements-windows.lock.txt')
        Invoke-Checked $VenvPython @('-c', 'import pkg_resources, PyInstaller')
        # Isolated editable builds keep temporary build tools out of this venv.
        Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--no-deps', '-e', '.')
        Invoke-Checked $VenvPython @('-m', 'pip', 'check')
        Invoke-Checked $VenvPython @('-B', 'scripts\check-windows-env.py')
        if ($InstallBrowser) {
            Invoke-Checked $VenvPython @('-m', 'playwright', 'install', 'chromium')
        }
        Push-Location (Join-Path $RepoRoot 'console')
        try { Invoke-Checked $Npm.Source @('ci', '--no-audit', '--no-fund') }
        finally { Pop-Location }
    } finally { Pop-Location }
    Write-Host 'Environment ready. Run scripts\build-paw.ps1 to build the console and paw.exe.' -ForegroundColor Green
    Write-Host 'Optional activation: .\.venv\Scripts\Activate.ps1'
} catch {
    Write-Error $_ -ErrorAction Continue
    exit 1
}
