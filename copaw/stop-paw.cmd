@echo off
setlocal
cd /d "%~dp0"

fltmc >nul 2>&1
if not errorlevel 1 goto :AdminReady

echo Requesting administrator permission to stop elevated paw.exe processes...
set "STOP_PAW_CMD_PATH=%~f0"
set "STOP_PAW_WORKDIR=%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $scriptPath=$env:STOP_PAW_CMD_PATH; $workDir=$env:STOP_PAW_WORKDIR; $quotedScript='\"' + $scriptPath + '\"'; $process=Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', $quotedScript) -WorkingDirectory $workDir -Verb RunAs -Wait -PassThru; exit $process.ExitCode } catch { Write-Host $_.Exception.Message; exit 1 }"
exit /b %ERRORLEVEL%

:AdminReady

echo Searching for paw.exe processes...

set "FOUND=0"
set "FAILED=0"

for /f "tokens=2" %%P in ('tasklist /FI "IMAGENAME eq paw.exe" 2^>nul ^| findstr /R /I "^paw\.exe[ ][ ]*[0-9][0-9]*"') do (
    set "FOUND=1"
    call :StopPid "%%P"
)

if "%FOUND%"=="0" (
    echo No paw.exe process found.
    exit /b 0
)

if "%FAILED%"=="1" (
    echo Some paw.exe processes failed to stop.
    exit /b 1
)

echo Paw stopped.
exit /b 0

:StopPid
set "PID=%~1"
if "%PID%"=="" exit /b 0

echo Stopping paw.exe PID %PID%...
taskkill /PID %PID% /F >nul 2>&1
if errorlevel 1 (
    echo Failed to stop paw.exe PID %PID%.
    set "FAILED=1"
    exit /b 1
)

echo Stopped paw.exe PID %PID%.
exit /b 0
