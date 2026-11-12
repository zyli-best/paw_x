@echo off
chcp 65001>nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
call :ResolvePowerShell
if errorlevel 1 exit /b 1
call :NowMs SCRIPT_START_MS
set "PORT=8088"
set "INIT_ONLY=0"
set "LOG_LEVEL="
set "NO_ARGS=0"
if "%~1"=="" set "NO_ARGS=1"
if not defined COPY_DEVCO_TOOLS set "COPY_DEVCO_TOOLS=0"

:ParseArgs
if "%~1"=="" goto ArgsDone
if /I "%~1"=="--init-only" (
    set "INIT_ONLY=1"
    shift
    goto ParseArgs
)
if /I "%~1"=="init" (
    set "INIT_ONLY=1"
    shift
    goto ParseArgs
)
if /I "%~1"=="--copy-devco-tools" (
    set "COPY_DEVCO_TOOLS=1"
    shift
    goto ParseArgs
)
if /I "%~1"=="copy-devco-tools" (
    set "COPY_DEVCO_TOOLS=1"
    shift
    goto ParseArgs
)
if /I "%~1"=="--log-level" (
    if "%~2"=="" (
        echo Missing value for --log-level.
        exit /b 1
    )
    call :SetLogLevel "%~2"
    if errorlevel 1 exit /b 1
    shift
    shift
    goto ParseArgs
)
if /I "%~1"=="log-level" (
    if "%~2"=="" (
        echo Missing value for log-level.
        exit /b 1
    )
    call :SetLogLevel "%~2"
    if errorlevel 1 exit /b 1
    shift
    shift
    goto ParseArgs
)
set "PORT=%~1"
shift
goto ParseArgs

:ArgsDone

if "%NO_ARGS%"=="1" if "%LOG_LEVEL%"=="" set "LOG_LEVEL=trace"

set "WORKDIR=%CD%"
if "%WORKDIR:~-1%"=="\" set "WORKDIR=%WORKDIR:~0,-1%"
set "RUNTIME_DIR=%WORKDIR%\paw-runtime"
set "RUNTIME_ZIP=%WORKDIR%\paw-runtime.zip"
set "PAW_EXE=%RUNTIME_DIR%\paw.exe"
set "PAW_DATA_DIR=%RUNTIME_DIR%\.devco_paw"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

call :Log start-paw begin, port=%PORT%, init_only=%INIT_ONLY%, log_level=%LOG_LEVEL%, copy_devco_tools=%COPY_DEVCO_TOOLS%
if "%NO_ARGS%"=="1" (
    call :Log no-argument startup mode enabled
    echo No-argument startup command:
    echo   "%RUNTIME_DIR%\paw.exe" --host 127.0.0.1 --port %PORT% --log-level %LOG_LEVEL%
    echo Working directory:
    echo   %RUNTIME_DIR%
)
if "%INIT_ONLY%"=="1" (
    call :StopRunningPaw
    if errorlevel 1 (
        echo Failed to stop existing paw.exe processes before runtime refresh.
        call :ExitWithElapsed 1
        exit /b 1
    )
)
if "%INIT_ONLY%"=="1" (
    call :EnsurePawRuntime "%WORKDIR%" "%RUNTIME_DIR%" "%RUNTIME_ZIP%" "%INIT_ONLY%"
    if errorlevel 1 (
        echo Failed to prepare Paw runtime.
        call :ExitWithElapsed 1
        exit /b 1
    )
    call :Log runtime ready
) else (
    call :Log skip runtime prepare for normal start
)

set "PAW_EXE=%RUNTIME_DIR%\paw.exe"
if not exist "%PAW_EXE%" (
    if "%NO_ARGS%"=="1" (
        echo paw.exe not found.
        echo No-argument start requires the current path runtime executable.
        echo Expected: %RUNTIME_DIR%\paw.exe
        goto ExitFailure
    )
    if exist "%WORKDIR%\paw.exe" (
        set "PAW_EXE=%WORKDIR%\paw.exe"
        call :Log legacy runtime ready
        echo Using legacy paw.exe: %PAW_EXE%
    ) else (
        echo paw.exe not found.
        echo Normal start does not extract paw-runtime.zip.
        echo Expected: %RUNTIME_DIR%\paw.exe or %WORKDIR%\paw.exe
        call :ExitWithElapsed 1
        exit /b 1
    )
)
for %%I in ("%PAW_EXE%") do set "PAW_EXE_DIR=%%~dpI"
if "%PAW_EXE_DIR:~-1%"=="\" set "PAW_EXE_DIR=%PAW_EXE_DIR:~0,-1%"
set "LOGDIR=%PAW_EXE_DIR%\log"
set "PAW_STDOUT=%LOGDIR%\paw-%PORT%.out.log"
set "PAW_STDERR=%LOGDIR%\paw-%PORT%.err.log"
echo Paw process logs:
echo   %PAW_STDOUT%
echo   %PAW_STDERR%

if "%INIT_ONLY%"=="1" (
    call :RunInitOnlyFlow "%WORKDIR%" "%PAW_EXE%" "%PAW_EXE_DIR%" "%RUNTIME_DIR%"
    if errorlevel 1 (
        call :ExitWithElapsed 1
        exit /b 1
    )
    call :ExitWithElapsed 0
    exit /b 0
)

call :StopRunningPaw
if errorlevel 1 (
    echo Failed to stop existing paw.exe processes.
    call :ExitWithElapsed 1
    exit /b 1
)

set "QWENPAW_INIT_DEFAULT_ONLY="

call :NowMs PAW_START_MS
call :StartPaw "%RUNTIME_DIR%" "%PAW_EXE%" "%PORT%" "%PAW_STDOUT%" "%PAW_STDERR%" "%LOG_LEVEL%"
if errorlevel 1 (
    echo Failed to start Paw.
    call :ExitWithElapsed 1
    exit /b 1
)
call :Log paw process launched

call :WaitForPawReady "%PORT%"
if errorlevel 1 (
    echo Paw did not start successfully on port %PORT%; skip DevCoTools copy.
    echo Check logs:
    echo   %PAW_STDOUT%
    echo   %PAW_STDERR%
    call :ExitWithElapsed 1
    exit /b 1
)
call :NowMs PAW_READY_MS
call :PrintElapsedSeconds "%PAW_START_MS%" "%PAW_READY_MS%" "paw startup elapsed"

if "%COPY_DEVCO_TOOLS%"=="1" (
    call :Log copy DevCoTools begin
    call :EnsureSkillScriptsDirs "%RUNTIME_DIR%"
    "%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $pairs=@(@('D:\DevCoTools\ExcelConvert', (Join-Path $env:RUNTIME_DIR '.devco_paw\workspaces\code_generator\skills\code-gen-executor\scripts')), @('D:\DevCoTools\SvnUpdate', (Join-Path $env:RUNTIME_DIR '.devco_paw\workspaces\svn_guard\skills\svn-updater\scripts')), @('D:\DevCoTools\VulnScanTool', (Join-Path $env:RUNTIME_DIR '.devco_paw\workspaces\vuln_scan\skills\vuln-scan\scripts\vuln_scan_tool'))); foreach ($pair in $pairs) { $src=$pair[0]; $dst=$pair[1]; if (-not (Test-Path -LiteralPath $src)) { Write-Host ('Skip DevCoTools source not found: ' + $src); continue }; if (-not (Test-Path -LiteralPath $dst)) { New-Item -ItemType Directory -Force -Path $dst | Out-Null }; $hasContent = Get-ChildItem -LiteralPath $dst -Force -ErrorAction SilentlyContinue | Select-Object -First 1; if ($hasContent) { Write-Host ('Destination already has content, skip copy: ' + $dst); continue }; robocopy $src $dst /E /R:2 /W:1 | Out-Null; if ($LASTEXITCODE -ge 8) { throw ('Failed to copy ' + $src + ' to ' + $dst) }; Write-Host ('Copied ' + $src + ' to ' + $dst) }"
    if errorlevel 1 (
        call :ExitWithElapsed 1
        exit /b 1
    )
    call :Log copy DevCoTools done
) else (
    echo DevCoTools copy disabled. Use --copy-devco-tools or set COPY_DEVCO_TOOLS=1 to enable.
)

call :Log start-paw done
call :ExitWithElapsed 0
exit /b 0

:ResolvePowerShell
set "PS_EXE="
for %%P in (powershell.exe pwsh.exe) do (
    if not defined PS_EXE (
        for /f "delims=" %%I in ('where %%P 2^>nul') do (
            if not defined PS_EXE set "PS_EXE=%%I"
        )
    )
)
if not defined PS_EXE (
    if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
)
if not defined PS_EXE (
    if exist "%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe" set "PS_EXE=%WINDIR%\System32\WindowsPowerShell\v1.0\powershell.exe"
)
if not defined PS_EXE (
    echo PowerShell executable was not found.
    echo Please install PowerShell or add powershell.exe/pwsh.exe to PATH.
    exit /b 1
)
exit /b 0

:Log
call :GetTimestamp
echo [!LOG_TS!] %*
exit /b 0

:GetTimestamp
set "LOG_TS="
for /f "tokens=2 delims==" %%I in ('wmic os get LocalDateTime /value 2^>nul ^| findstr /i "LocalDateTime"') do set "DT=%%I"
if defined DT set "LOG_TS=!DT:~0,4!-!DT:~4,2!-!DT:~6,2! !DT:~8,2!:!DT:~10,2!:!DT:~12,2!"
if not defined LOG_TS (
    set "LOG_TS_FILE=%TEMP%\paw-log-ts-%RANDOM%.txt"
    "%PS_EXE%" -NoProfile -Command "(Get-Date).ToString('yyyy-MM-dd HH:mm:ss') | Out-File -LiteralPath '!LOG_TS_FILE!' -Encoding ascii -NoNewline"
    if exist "!LOG_TS_FILE!" (
        set /p LOG_TS=<"!LOG_TS_FILE!"
        del /f /q "!LOG_TS_FILE!" >nul 2>&1
    )
)
exit /b 0

:NowMs
set "NOW_TIME=%TIME: =0%"
set /a "NOW_MS=(1%NOW_TIME:~0,2%-100)*3600000 + (1%NOW_TIME:~3,2%-100)*60000 + (1%NOW_TIME:~6,2%-100)*1000 + (1%NOW_TIME:~9,2%-100)*10"
set "%~1=%NOW_MS%"
exit /b 0

:PrintElapsedSeconds
set "ELAPSED_START_MS=%~1"
set "ELAPSED_END_MS=%~2"
set "ELAPSED_LABEL=%~3"
"%PS_EXE%" -NoProfile -Command "$elapsed=([int64]$env:ELAPSED_END_MS-[int64]$env:ELAPSED_START_MS)/1000; Write-Host ('{0}: {1:N3}s' -f $env:ELAPSED_LABEL, $elapsed)"
exit /b 0

:ExitWithElapsed
set "EXIT_CODE=%~1"
if "%EXIT_CODE%"=="" set "EXIT_CODE=0"
call :NowMs SCRIPT_END_MS
call :PrintElapsedSeconds "%SCRIPT_START_MS%" "%SCRIPT_END_MS%" "script total elapsed"
exit /b %EXIT_CODE%

:ExitFailure
call :ExitWithElapsed 1
exit /b 1

:EnsurePawRuntime
set "RUNTIME_WORKDIR=%~1"
set "RUNTIME_TARGET=%~2"
set "RUNTIME_ARCHIVE=%~3"
set "RUNTIME_INIT_ONLY=%~4"

if exist "%RUNTIME_TARGET%\paw.exe" (
    if "%RUNTIME_INIT_ONLY%"=="1" if exist "%RUNTIME_ARCHIVE%" goto ExtractPawRuntime
    call :Log runtime already exists
    echo Using existing Paw runtime: %RUNTIME_TARGET%
    exit /b 0
)

if exist "%RUNTIME_ARCHIVE%" goto ExtractPawRuntime

if exist "%RUNTIME_WORKDIR%\paw.exe" (
    call :Log legacy runtime ready
    echo Using legacy paw.exe: %RUNTIME_WORKDIR%\paw.exe
    exit /b 0
)

echo Neither "%RUNTIME_TARGET%\paw.exe" nor "%RUNTIME_ARCHIVE%" was found.
exit /b 1

:ExtractPawRuntime
call :Log runtime extract begin
echo Extracting Paw runtime to install directory: %RUNTIME_TARGET%
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; function Remove-TreeForce([string]$p){if(-not(Test-Path -LiteralPath $p)){return};$tempBase=$env:RUNTIME_TARGET;if([string]::IsNullOrWhiteSpace($tempBase)){$tempBase=$env:RESTORE_RUNTIME_DIR};$tempParent=if([string]::IsNullOrWhiteSpace($tempBase)){$null}else{Split-Path -Parent $tempBase};$tempRoot=if([string]::IsNullOrWhiteSpace($tempParent)){$null}else{Join-Path $tempParent '.paw-tmp'};if([string]::IsNullOrWhiteSpace($tempRoot)){$tempRoot=Join-Path $env:SystemRoot 'Temp'};New-Item -ItemType Directory -Force -Path $tempRoot|Out-Null;$e=Join-Path $tempRoot ('paw-empty-'+[guid]::NewGuid().ToString('N'));New-Item -ItemType Directory -Force -Path $e|Out-Null;try{& robocopy $e $p /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS|Out-Null}catch{};Remove-Item -LiteralPath $e -Force -Recurse -EA SilentlyContinue;if(Test-Path -LiteralPath $p){Remove-Item -LiteralPath $p -Force -Recurse -EA SilentlyContinue}}; function Copy-TreeRobocopy([string]$src,[string]$dst){New-Item -ItemType Directory -Force -Path $dst|Out-Null;$rcArgs=@($src,$dst,'/E','/COPY:DAT','/R:2','/W:1','/NFL','/NDL','/NJH','/NJS');$skip=Join-Path $src 'workspaces\code_keeper\skills\bios-compiler\scripts\Tool';if(Test-Path -LiteralPath $skip){$rcArgs+=@('/XD',$skip);Write-Host ('  skip bios-compiler Tool: '+$skip)};& robocopy @rcArgs|Out-Null;if($LASTEXITCODE -ge 8){throw ('robocopy failed '+$src+' -> '+$dst+' ('+$LASTEXITCODE+')')}}; $zip=$env:RUNTIME_ARCHIVE; $dest=$env:RUNTIME_TARGET; $tmp=$dest+'.__extracting'; $backup=$dest+'.__devco_paw_backup'; if(Test-Path -LiteralPath $tmp){Remove-TreeForce $tmp}; if(Test-Path -LiteralPath $backup){Remove-TreeForce $backup}; $devcoSrc=Join-Path $dest '.devco_paw'; if(($env:RUNTIME_INIT_ONLY -eq '1') -and (Test-Path -LiteralPath $devcoSrc)){Copy-TreeRobocopy $devcoSrc $backup}; New-Item -ItemType Directory -Force -Path $tmp|Out-Null; Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force; if(-not(Test-Path -LiteralPath (Join-Path $tmp 'paw.exe'))){throw 'paw.exe not found after extracting runtime archive'}; if(Test-Path -LiteralPath $dest){Remove-TreeForce $dest}; Move-Item -LiteralPath $tmp -Destination $dest"
if errorlevel 1 exit /b 1
if not exist "%RUNTIME_TARGET%\paw.exe" (
    echo paw.exe not found after extracting runtime: %RUNTIME_TARGET%\paw.exe
    exit /b 1
)
call :Log runtime extract done
exit /b 0

:RunInitOnlyFlow
set "INIT_FLOW_WORKDIR=%~1"
set "INIT_FLOW_EXE=%~2"
set "INIT_FLOW_EXE_DIR=%~3"
set "INIT_FLOW_RUNTIME_DIR=%~4"

echo Removing Paw secret directories before init...
call :RemovePawSecretDir "%INIT_FLOW_WORKDIR%\.devco_paw.secret"
if errorlevel 1 (
    echo Failed to remove Paw secret directory under workdir.
    exit /b 1
)
call :RemovePawSecretDir "%INIT_FLOW_EXE_DIR%\.devco_paw.secret"
if errorlevel 1 (
    echo Failed to remove Paw secret directory under paw runtime.
    exit /b 1
)

call :BackupDevcoPawBeforeInit "%INIT_FLOW_RUNTIME_DIR%"
if errorlevel 1 (
    echo Failed to backup .devco_paw before init.
    exit /b 1
)

call :RunPawInitOnly "%INIT_FLOW_RUNTIME_DIR%" "%INIT_FLOW_EXE%"
if errorlevel 1 (
    echo Paw init failed.
    exit /b 1
)

call :MergeNewBuiltinSkillsIntoBackup "%INIT_FLOW_RUNTIME_DIR%"
if errorlevel 1 (
    echo Failed to merge new builtin skills into old .devco_paw.
    exit /b 1
)

call :RestoreDevcoPawBackup "%INIT_FLOW_RUNTIME_DIR%" optional
if errorlevel 1 (
    echo Failed to restore old .devco_paw after init.
    exit /b 1
)

exit /b 0

:MergeNewBuiltinSkillsIntoBackup
set "MERGE_RUNTIME_DIR=%~1"
if "%MERGE_RUNTIME_DIR%"=="" exit /b 0

call :Log merge new builtin skills begin
call :RunEmbeddedPowerShellScript "MERGE_BUILTIN_SKILLS_PS1"
if errorlevel 1 exit /b 1
call :Log merge new builtin skills done
exit /b 0

:BackupDevcoPawBeforeInit
set "BACKUP_RUNTIME_DIR=%~1"
if "%BACKUP_RUNTIME_DIR%"=="" exit /b 0

call :Log backup .devco_paw before init begin
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; function Copy-TreeRobocopy([string]$src,[string]$dst){if(Test-Path -LiteralPath $dst){Write-Host ('Backup already exists, skip create: '+$dst); return}; New-Item -ItemType Directory -Force -Path $dst | Out-Null; $rcArgs=@($src,$dst,'/E','/COPY:DAT','/R:2','/W:1','/NFL','/NDL','/NJH','/NJS'); $skip=Join-Path $src 'workspaces\code_keeper\skills\bios-compiler\scripts\Tool'; if(Test-Path -LiteralPath $skip){$rcArgs+=@('/XD',$skip)}; & robocopy @rcArgs | Out-Null; if($LASTEXITCODE -ge 8){throw ('robocopy failed '+$src+' -> '+$dst+' ('+$LASTEXITCODE+')')}}; $dest=$env:BACKUP_RUNTIME_DIR; $backup=$dest+'.__devco_paw_backup'; $devcoSrc=Join-Path $dest '.devco_paw'; if(-not(Test-Path -LiteralPath $devcoSrc)){Write-Host ('Source .devco_paw not found, skip backup: '+$devcoSrc); exit 0}; Copy-TreeRobocopy $devcoSrc $backup; Write-Host ('Backed up .devco_paw -> '+$backup)"
if errorlevel 1 exit /b 1
call :Log backup .devco_paw before init done
exit /b 0

:RestoreDevcoPawBackup
set "RESTORE_RUNTIME_DIR=%~1"
set "RESTORE_REQUIRED=%~2"
if "%RESTORE_RUNTIME_DIR%"=="" exit /b 0

call :Log restore old .devco_paw begin
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; function Remove-TreeForce([string]$p){if(-not(Test-Path -LiteralPath $p)){return};$tempBase=$env:RUNTIME_TARGET;if([string]::IsNullOrWhiteSpace($tempBase)){$tempBase=$env:RESTORE_RUNTIME_DIR};$tempParent=if([string]::IsNullOrWhiteSpace($tempBase)){$null}else{Split-Path -Parent $tempBase};$tempRoot=if([string]::IsNullOrWhiteSpace($tempParent)){$null}else{Join-Path $tempParent '.paw-tmp'};if([string]::IsNullOrWhiteSpace($tempRoot)){$tempRoot=Join-Path $env:SystemRoot 'Temp'};New-Item -ItemType Directory -Force -Path $tempRoot|Out-Null;$e=Join-Path $tempRoot ('paw-empty-'+[guid]::NewGuid().ToString('N'));New-Item -ItemType Directory -Force -Path $e|Out-Null;try{& robocopy $e $p /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS|Out-Null}catch{};Remove-Item -LiteralPath $e -Force -Recurse -EA SilentlyContinue;if(Test-Path -LiteralPath $p){Remove-Item -LiteralPath $p -Force -Recurse -EA SilentlyContinue}}; function Copy-TreeRobocopy([string]$src,[string]$dst){New-Item -ItemType Directory -Force -Path $dst|Out-Null;& robocopy $src $dst /E /COPY:DAT /R:2 /W:1 /NFL /NDL /NJH /NJS|Out-Null;if($LASTEXITCODE -ge 8){throw ('robocopy failed '+$src+' -> '+$dst+' ('+$LASTEXITCODE+')')}}; $dest=$env:RESTORE_RUNTIME_DIR; $backup=$dest+'.__devco_paw_backup'; $required=($env:RESTORE_REQUIRED -eq 'required'); if(-not(Test-Path -LiteralPath $backup)){if($required){throw ('Old .devco_paw backup not found: '+$backup)}; Write-Host ('Old .devco_paw backup not found, skip restore: '+$backup); exit 0}; $devcoDst=Join-Path $dest '.devco_paw'; if(Test-Path -LiteralPath $devcoDst){Remove-TreeForce $devcoDst}; Copy-TreeRobocopy $backup $devcoDst; Remove-TreeForce $backup"
if errorlevel 1 exit /b 1
call :Log restore old .devco_paw done
exit /b 0

:RemovePawSecretDir
set "TARGET_SECRET_DIR=%~1"
if "%TARGET_SECRET_DIR%"=="" exit /b 0

call :Log remove secret dir begin: %TARGET_SECRET_DIR%
if not exist "%TARGET_SECRET_DIR%\" if not exist "%TARGET_SECRET_DIR%" (
    echo Secret directory not found, skip remove: %TARGET_SECRET_DIR%
    exit /b 0
)

rd /s /q "%TARGET_SECRET_DIR%" >nul 2>&1
if exist "%TARGET_SECRET_DIR%\" (
    echo Failed to remove secret directory: %TARGET_SECRET_DIR%
    exit /b 1
)
if exist "%TARGET_SECRET_DIR%" (
    echo Failed to remove secret directory: %TARGET_SECRET_DIR%
    exit /b 1
)

call :Log remove secret dir done: %TARGET_SECRET_DIR%
exit /b 0

:RunPawInitOnly
set "INIT_RUNTIME_DIR=%~1"
set "INIT_EXE=%~2"

if not exist "%INIT_EXE%" (
    echo paw.exe not found: %INIT_EXE%
    exit /b 1
)
echo Running Paw init only...
call :Log init-only begin
set "LAUNCH_MODE=init-only"
set "LAUNCH_EXE=%INIT_EXE%"
set "LAUNCH_WORKDIR=%INIT_RUNTIME_DIR%"
call :RunEmbeddedPowerShellScript "LAUNCH_PAW_PS1"
if errorlevel 1 (
    echo Paw init failed.
    exit /b 1
)
call :Log init-only done
echo Paw init completed.
exit /b 0

:StopRunningPaw
set "STOP_PAW_CMD=%WORKDIR%\stop-paw.cmd"
if not exist "%STOP_PAW_CMD%" (
    echo stop-paw.cmd not found, fallback to taskkill.
    taskkill /IM "paw.exe" /F >nul 2>&1
    exit /b 0
)

call :Log stop existing paw begin
call "%STOP_PAW_CMD%"
if errorlevel 1 exit /b 1
call :Log stop existing paw done
exit /b 0

:SyncDevCoTools
set "SYNC_RUNTIME_DIR=%~1"
if "%SYNC_RUNTIME_DIR%"=="" (
    echo SYNC_RUNTIME_DIR is empty.
    exit /b 1
)
call :Log sync DevCo tools begin
call :RunEmbeddedPowerShellScript "SYNC_DEVCO_TOOLS_PS1"
if errorlevel 1 exit /b 1
call :Log sync DevCo tools done
exit /b 0

:EnsureSkillScriptsDirs
set "SKILL_ROOT=%~1"
if "%SKILL_ROOT%"=="" exit /b 0
set "SKILL_WS=%SKILL_ROOT%\.devco_paw\workspaces"
call :Log ensure skill scripts dirs begin
if not exist "%SKILL_WS%\vuln_scan\skills\vuln-scan\scripts\" mkdir "%SKILL_WS%\vuln_scan\skills\vuln-scan\scripts"
if not exist "%SKILL_WS%\vuln_scan\skills\vuln-scan\scripts\vuln_scan_tool\" mkdir "%SKILL_WS%\vuln_scan\skills\vuln-scan\scripts\vuln_scan_tool"
if not exist "%SKILL_WS%\svn_guard\skills\svn-updater\scripts\" mkdir "%SKILL_WS%\svn_guard\skills\svn-updater\scripts"
if not exist "%SKILL_WS%\code_generator\skills\code-gen-executor\scripts\" mkdir "%SKILL_WS%\code_generator\skills\code-gen-executor\scripts"
call :Log ensure skill scripts dirs done
exit /b 0

:ReconcileAgentProfiles
set "RECONCILE_RUNTIME_DIR=%~1"
if "%RECONCILE_RUNTIME_DIR%"=="" exit /b 0

call :Log reconcile agent profiles begin
call :RunEmbeddedPowerShellScript "RECONCILE_AGENT_PROFILES_PS1"
if errorlevel 1 exit /b 1
call :Log reconcile agent profiles done
exit /b 0

:StartPaw
set "START_WORKDIR=%~1"
set "START_EXE=%~2"
set "START_PORT=%~3"
set "START_STDOUT=%~4"
set "START_STDERR=%~5"
set "START_LOG_LEVEL=%~6"

if not exist "%START_EXE%" (
    echo paw.exe not found: %START_EXE%
    exit /b 1
)
if not exist "%LOGDIR%\" mkdir "%LOGDIR%"

call :Log start process begin
echo Start details:
echo   exe: %START_EXE%
echo   workdir: %START_WORKDIR%
if "%START_LOG_LEVEL%"=="" (
    echo   args: --host 127.0.0.1 --port %START_PORT%
) else (
    echo   args: --host 127.0.0.1 --port %START_PORT% --log-level %START_LOG_LEVEL%
)
echo   stdout: %START_STDOUT%
echo   stderr: %START_STDERR%
set "LAUNCH_MODE=start"
set "LAUNCH_EXE=%START_EXE%"
set "LAUNCH_WORKDIR=%START_WORKDIR%"
set "LAUNCH_PORT=%START_PORT%"
set "LAUNCH_STDOUT=%START_STDOUT%"
set "LAUNCH_STDERR=%START_STDERR%"
set "LAUNCH_LOG_LEVEL=%START_LOG_LEVEL%"
call :RunEmbeddedPowerShellScript "LAUNCH_PAW_PS1"
if errorlevel 1 exit /b 1
call :Log start process done
exit /b 0

:SetLogLevel
set "REQUESTED_LOG_LEVEL=%~1"
if /I "%REQUESTED_LOG_LEVEL%"=="critical" (
    set "LOG_LEVEL=critical"
    exit /b 0
)
if /I "%REQUESTED_LOG_LEVEL%"=="error" (
    set "LOG_LEVEL=error"
    exit /b 0
)
if /I "%REQUESTED_LOG_LEVEL%"=="warning" (
    set "LOG_LEVEL=warning"
    exit /b 0
)
if /I "%REQUESTED_LOG_LEVEL%"=="info" (
    set "LOG_LEVEL=info"
    exit /b 0
)
if /I "%REQUESTED_LOG_LEVEL%"=="debug" (
    set "LOG_LEVEL=debug"
    exit /b 0
)
if /I "%REQUESTED_LOG_LEVEL%"=="trace" (
    set "LOG_LEVEL=trace"
    exit /b 0
)
echo Invalid log level: %REQUESTED_LOG_LEVEL%
echo Allowed values: critical, error, warning, info, debug, trace
exit /b 1

:WaitForPawReady
set "READY_PORT=%~1"
call :Log wait ready begin
echo Waiting for Paw to start on port %READY_PORT%...
echo Probing 127.0.0.1:%READY_PORT% for up to 120 seconds...
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -Command "$port=[int]$env:READY_PORT; $deadline=[DateTime]::UtcNow.AddSeconds(120); while ([DateTime]::UtcNow -lt $deadline) { $client=$null; try { $client=New-Object Net.Sockets.TcpClient; $async=$client.BeginConnect('127.0.0.1', $port, $null, $null); if ($async.AsyncWaitHandle.WaitOne(200, $false)) { $client.EndConnect($async); exit 0 } } catch { } finally { if ($client) { $client.Close() } }; Start-Sleep -Milliseconds 500 }; exit 1" >nul 2>&1
if not errorlevel 1 (
    call :Log wait ready done
    echo Paw started successfully.
    exit /b 0
)
exit /b 1

:RunEmbeddedPowerShellScript
set "EMBEDDED_SCRIPT_NAME=%~1"
if "%EMBEDDED_SCRIPT_NAME%"=="" (
    echo Embedded PowerShell script name is empty.
    exit /b 1
)
set "EMBEDDED_CMD_PATH=%~f0"
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $path=$env:EMBEDDED_CMD_PATH; $name=$env:EMBEDDED_SCRIPT_NAME; $text=[System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8); $begin='::BEGIN_EMBEDDED_' + $name; $end='::END_EMBEDDED_' + $name; $beginIndex=$text.LastIndexOf($begin); if ($beginIndex -lt 0) { throw ('Embedded script begin marker not found: ' + $begin) }; $contentStart=$text.IndexOf([char]10, $beginIndex); if ($contentStart -lt 0) { throw ('Embedded script content not found: ' + $name) }; $contentStart += 1; $endIndex=$text.IndexOf($end, $contentStart); if ($endIndex -lt 0) { throw ('Embedded script end marker not found: ' + $end) }; $script=$text.Substring($contentStart, $endIndex - $contentStart); $tmp=Join-Path ([System.IO.Path]::GetTempPath()) ('paw-embedded-' + $name + '-' + [System.Guid]::NewGuid().ToString('N') + '.ps1'); $utf8NoBom=New-Object System.Text.UTF8Encoding($false); [System.IO.File]::WriteAllText($tmp, $script, $utf8NoBom); try { & $tmp; exit $LASTEXITCODE } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force } }"
exit /b %ERRORLEVEL%

goto :eof

::BEGIN_EMBEDDED_MERGE_BUILTIN_SKILLS_PS1
$ErrorActionPreference = 'Stop'

$runtime = $env:MERGE_RUNTIME_DIR
if (-not $runtime) {
    Write-Host 'MERGE_RUNTIME_DIR is empty, skip merge.'
    exit 0
}

$oldRoot = $runtime + '.__devco_paw_backup'
$newRoot = Join-Path $runtime '.devco_paw'

Write-Host ('Merge old .devco_paw: ' + $oldRoot)
Write-Host ('Merge new .devco_paw: ' + $newRoot)

if (-not (Test-Path -LiteralPath $oldRoot)) {
    Write-Host ('Old .devco_paw backup not found, skip merge: ' + $oldRoot)
    exit 0
}

$oldWs = Join-Path $oldRoot 'workspaces\default'
$newWs = Join-Path $newRoot 'workspaces\default'
$oldSkills = Join-Path $oldWs 'skills'
$newSkills = Join-Path $newWs 'skills'
$oldManifestPath = Join-Path $oldWs 'skill.json'
$newManifestPath = Join-Path $newWs 'skill.json'

Write-Host ('Old skills dir: ' + $oldSkills)
Write-Host ('New skills dir: ' + $newSkills)
Write-Host ('Old skill.json: ' + $oldManifestPath)
Write-Host ('New skill.json: ' + $newManifestPath)

if (-not (Test-Path -LiteralPath $newSkills)) {
    Write-Host ('New skills directory not found, skip merge: ' + $newSkills)
    exit 0
}

if (-not (Test-Path -LiteralPath $oldSkills)) {
    Write-Host ('Old skills directory not found, create: ' + $oldSkills)
    New-Item -ItemType Directory -Force -Path $oldSkills | Out-Null
}

if (-not (Test-Path -LiteralPath $newManifestPath)) {
    Write-Host ('New skill manifest not found, skip merge: ' + $newManifestPath)
    exit 0
}

function Read-JsonFile($path) {
    $resolvedPath = (Resolve-Path -LiteralPath $path).Path
    $raw = [System.IO.File]::ReadAllText($resolvedPath, [System.Text.Encoding]::UTF8)
    if (-not $raw.Trim()) {
        throw ('JSON file is empty: ' + $path)
    }
    return $raw | ConvertFrom-Json
}

function New-EmptySkillManifest {
    return [pscustomobject]@{
        schema_version = 'workspace-skill-manifest.v1'
        version = 0
        skills = [pscustomobject]@{}
    }
}

function Get-JsonProperty($object, [string]$name) {
    if ($null -eq $object) {
        return $null
    }
    return $object.PSObject.Properties[$name]
}

$newManifest = Read-JsonFile $newManifestPath
if ($null -eq (Get-JsonProperty $newManifest 'skills')) {
    Write-Host ('New skill manifest has no skills object, skip merge: ' + $newManifestPath)
    exit 0
}

if (Test-Path -LiteralPath $oldManifestPath) {
    $oldManifest = Read-JsonFile $oldManifestPath
} else {
    Write-Host ('Old skill manifest not found, create new manifest: ' + $oldManifestPath)
    $oldManifest = New-EmptySkillManifest
}

if ($null -eq (Get-JsonProperty $oldManifest 'schema_version')) {
    $oldManifest | Add-Member -NotePropertyName 'schema_version' -NotePropertyValue 'workspace-skill-manifest.v1'
}
if ($null -eq (Get-JsonProperty $oldManifest 'version')) {
    $oldManifest | Add-Member -NotePropertyName 'version' -NotePropertyValue 0
}
if ($null -eq (Get-JsonProperty $oldManifest 'skills')) {
    $oldManifest | Add-Member -NotePropertyName 'skills' -NotePropertyValue ([pscustomobject]@{})
}

$addedNames = @()
$checked = 0
$oldSkillNames = @{}
$oldManifest.skills.PSObject.Properties | ForEach-Object {
    $oldSkillNames[$_.Name] = $true
}

foreach ($newSkillProperty in $newManifest.skills.PSObject.Properties) {
    $checked++
    $name = $newSkillProperty.Name
    if ($oldSkillNames.ContainsKey($name)) {
        Write-Host ('Skill exists in old skill.json, skip manifest copy: ' + $name)
        continue
    }

    $src = Join-Path $newSkills $name
    $dst = Join-Path $oldSkills $name
    if (Test-Path -LiteralPath $src) {
        if (Test-Path -LiteralPath $dst) {
            Write-Host ('Skill directory already exists in old dir, keep existing files: ' + $name)
        } else {
            Write-Host ('New skill missing in old dir, copy begin: ' + $name)
            Write-Host ('  from: ' + $src)
            Write-Host ('  to:   ' + $dst)
            Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
            if (-not (Test-Path -LiteralPath $dst)) {
                throw ('Copy skill failed, destination missing: ' + $dst)
            }
            Write-Host ('Copy skill done: ' + $name)
        }
    } else {
        Write-Host ('Skill is in new skill.json but directory is missing, manifest entry only: ' + $name)
    }

    $oldManifest.skills | Add-Member -NotePropertyName $name -NotePropertyValue $newSkillProperty.Value
    $oldSkillNames[$name] = $true
    $addedNames += $name
    Write-Host ('Insert old skill.json entry done from new manifest: ' + $name)
}

Write-Host ('Checked new skill.json entries: ' + $checked)

if ($addedNames.Count -eq 0) {
    Write-Host 'No new builtin skills to merge; old skill.json not rewritten'
    exit 0
}

$oldManifest.version = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$json = $oldManifest | ConvertTo-Json -Depth 100
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($oldManifestPath, ($json + [Environment]::NewLine), $utf8NoBom)
Write-Host ('Write old skill.json done: ' + $oldManifestPath)
Write-Host ('Merged new builtin skills: ' + $addedNames.Count)
::END_EMBEDDED_MERGE_BUILTIN_SKILLS_PS1

::BEGIN_EMBEDDED_SYNC_DEVCO_TOOLS_PS1
$ErrorActionPreference = 'Stop'

$RuntimeDir = $env:SYNC_RUNTIME_DIR
if (-not $RuntimeDir) {
    throw 'SYNC_RUNTIME_DIR is empty.'
}

$toolsRoot = Join-Path $env:LOCALAPPDATA 'DevCo\Tools'
$devcoPaw = Join-Path $RuntimeDir '.devco_paw\workspaces'

$excludeFiles = @(
    'result.json',
    'report.txt',
    'report.xml'
)

$toolMappings = @(
    @{
        Name     = 'vuln_scan'
        Source   = Join-Path $toolsRoot 'VulnScanTool'
        Dest     = Join-Path $devcoPaw 'vuln_scan\skills\vuln-scan\scripts\vuln_scan_tool'
        Preserve = @('RunCheck.ps1')
    },
    @{
        Name     = 'svn_guard'
        Source   = Join-Path $toolsRoot 'SvnUpdate'
        Dest     = Join-Path $devcoPaw 'svn_guard\skills\svn-updater\scripts'
        Preserve = @('svn-update.ps1', 'start.cmd')
    },
    @{
        Name     = 'code_generator'
        Source   = Join-Path $toolsRoot 'ExcelConvert'
        Dest     = Join-Path $devcoPaw 'code_generator\skills\code-gen-executor\scripts'
        Preserve = @('start.cmd', 'ec-input-path.ps1')
    }
)

function Backup-PreservedFiles {
    param(
        [string] $DestDir,
        [string[]] $Files
    )

    $backup = @{}
    foreach ($file in $Files) {
        $path = Join-Path $DestDir $file
        if (Test-Path -LiteralPath $path) {
            $backup[$file] = Get-Content -LiteralPath $path -Raw -Encoding UTF8
        }
    }
    return $backup
}

function Restore-PreservedFiles {
    param(
        [string] $DestDir,
        [hashtable] $Backup
    )

    foreach ($entry in $Backup.GetEnumerator()) {
        $path = Join-Path $DestDir $entry.Key
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($path, $entry.Value, $utf8NoBom)
        Write-Host ('  preserved workspace file: ' + $entry.Key)
    }
}

function Remove-RuntimeArtifacts {
    param(
        [string] $DestDir
    )

    foreach ($file in $excludeFiles) {
        $path = Join-Path $DestDir $file
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
            Write-Host ('  removed artifact: ' + $file)
        }
    }
}

Write-Host ('DevCo tools root: ' + $toolsRoot)
Write-Host ('Runtime dir: ' + $RuntimeDir)

if (-not (Test-Path -LiteralPath $toolsRoot)) {
    Write-Host 'DevCo tools directory not found; skip sync.'
    exit 0
}

foreach ($map in $toolMappings) {
    Write-Host ('--- sync ' + $map.Name + ' ---')

    if (-not (Test-Path -LiteralPath $map.Source)) {
        Write-Host ('  skip, source missing: ' + $map.Source)
        continue
    }

    New-Item -ItemType Directory -Force -Path $map.Dest | Out-Null
    Write-Host ('  ensured destination: ' + $map.Dest)

    $versionFile = Join-Path $map.Source '.tool_version'
    if (Test-Path -LiteralPath $versionFile) {
        $version = (Get-Content -LiteralPath $versionFile -Raw).Trim()
        Write-Host ('  source version: ' + $version)
    }

    $backup = Backup-PreservedFiles -DestDir $map.Dest -Files $map.Preserve

    $xfArgs = @()
    foreach ($file in $excludeFiles) {
        $xfArgs += '/XF'
        $xfArgs += $file
    }
    $robocopyArgs = @(
        $map.Source,
        $map.Dest,
        '/E', '/R:2', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS'
    ) + $xfArgs
    & robocopy @robocopyArgs | Out-Null
    $rc = $LASTEXITCODE
    if ($rc -ge 8) {
        throw ('robocopy failed for ' + $map.Name + ' (exit ' + $rc + ')')
    }

    Restore-PreservedFiles -DestDir $map.Dest -Backup $backup
    Remove-RuntimeArtifacts -DestDir $map.Dest

    Write-Host ('  synced to: ' + $map.Dest)
}

Write-Host 'DevCo tools sync completed.'
exit 0
::END_EMBEDDED_SYNC_DEVCO_TOOLS_PS1

::BEGIN_EMBEDDED_LAUNCH_PAW_PS1
$ErrorActionPreference = 'Stop'

function ConvertTo-ProcessArgumentString {
    param([string[]]$ArgumentList)

    function Escape-ProcessArgument([string]$Value) {
        if ($null -eq $Value) { return '""' }
        if ($Value -notmatch '[\s"]') { return $Value }
        return '"' + ($Value -replace '(\\*)("|$)', '$1$1\"$2') + '"'
    }

    return (($ArgumentList | ForEach-Object { Escape-ProcessArgument $_ }) -join ' ')
}

function ConvertTo-PowerShellSingleQuotedString {
    param([string]$Value)

    return "'" + ($Value -replace "'", "''") + "'"
}

function Copy-PawProcessEnvironment {
    param(
        [System.Diagnostics.ProcessStartInfo]$StartInfo,
        [hashtable]$ExtraEnvironment = @{}
    )

    foreach ($entry in [Environment]::GetEnvironmentVariables([EnvironmentVariableTarget]::Process).GetEnumerator()) {
        if ($entry.Key -ieq 'QWENPAW_INIT_DEFAULT_ONLY') {
            continue
        }
        $StartInfo.Environment[$entry.Key] = [string]$entry.Value
    }

    foreach ($key in $ExtraEnvironment.Keys) {
        $StartInfo.Environment[$key] = [string]$ExtraEnvironment[$key]
    }
}

function New-PawProcessStartInfo {
    param(
        [string]$FilePath,
        [string]$WorkingDirectory,
        [string[]]$ArgumentList,
        [hashtable]$ExtraEnvironment = @{}
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.Arguments = ConvertTo-ProcessArgumentString -ArgumentList $ArgumentList
    Copy-PawProcessEnvironment -StartInfo $startInfo -ExtraEnvironment $ExtraEnvironment
    return $startInfo
}

function Start-PawDetachedProcess {
    param(
        [string]$FilePath,
        [string]$WorkingDirectory,
        [string[]]$ArgumentList,
        [string]$StdoutPath,
        [string]$StderrPath
    )

    foreach ($path in @($StdoutPath, $StderrPath)) {
        $dir = Split-Path -Parent $path
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
    }

    $argumentString = ConvertTo-ProcessArgumentString -ArgumentList $ArgumentList
    $combinedPath = [System.IO.Path]::ChangeExtension($StdoutPath, '.console.log')
    $header = @(
        '==== Paw launch ====',
        'time: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),
        'exe: ' + $FilePath,
        'workdir: ' + $WorkingDirectory,
        'args: ' + $argumentString,
        ''
    ) -join [Environment]::NewLine
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($StdoutPath, $header, $utf8NoBom)
    [System.IO.File]::WriteAllText($StderrPath, $header + 'stderr is merged into stdout/console log by the launcher.' + [Environment]::NewLine, $utf8NoBom)
    [System.IO.File]::WriteAllText($combinedPath, $header, $utf8NoBom)

    $helperScript = @"
`$ErrorActionPreference = 'Continue'
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue) {
    `$global:PSNativeCommandUseErrorActionPreference = `$false
}
`$filePath = $(ConvertTo-PowerShellSingleQuotedString $FilePath)
`$workingDirectory = $(ConvertTo-PowerShellSingleQuotedString $WorkingDirectory)
`$stdoutPath = $(ConvertTo-PowerShellSingleQuotedString $StdoutPath)
`$combinedPath = $(ConvertTo-PowerShellSingleQuotedString $combinedPath)
`$argumentList = @(
$(
    ($ArgumentList | ForEach-Object { '    ' + (ConvertTo-PowerShellSingleQuotedString $_) }) -join ",`r`n"
)
)
`$utf8NoBom = New-Object System.Text.UTF8Encoding(`$false)
try {
    [Console]::OutputEncoding = `$utf8NoBom
    [Console]::InputEncoding = `$utf8NoBom
    `$OutputEncoding = `$utf8NoBom
    & chcp.com 65001 | Out-Null
} catch {
}
function Append-Line([string]`$path, [string]`$line) {
    [System.IO.File]::AppendAllText(`$path, `$line + [Environment]::NewLine, `$utf8NoBom)
}
`$env:PYTHONUNBUFFERED = '1'
`$env:PYTHONUTF8 = '1'
`$env:PYTHONIOENCODING = 'utf-8'
Push-Location `$workingDirectory
try {
    Append-Line `$stdoutPath ('paw.exe command: ' + `$filePath + ' ' + $(ConvertTo-PowerShellSingleQuotedString $argumentString))
    Append-Line `$combinedPath ('paw.exe command: ' + `$filePath + ' ' + $(ConvertTo-PowerShellSingleQuotedString $argumentString))
    & `$filePath @argumentList 2>&1 | ForEach-Object {
        if (`$_ -is [System.Management.Automation.ErrorRecord]) {
            `$line = `$_.Exception.Message
        } else {
            `$line = [string]`$_
        }
        Append-Line `$stdoutPath `$line
        Append-Line `$combinedPath `$line
    }
    `$exitCode = `$LASTEXITCODE
    Append-Line `$stdoutPath ('paw.exe exited with code: ' + `$exitCode)
    Append-Line `$combinedPath ('paw.exe exited with code: ' + `$exitCode)
    exit `$exitCode
} finally {
    Pop-Location
}
"@

    $helperPath = Join-Path ([System.IO.Path]::GetTempPath()) ('paw-log-forwarder-' + [System.Guid]::NewGuid().ToString('N') + '.ps1')
    [System.IO.File]::WriteAllText($helperPath, $helperScript, $utf8NoBom)

    $psExe = $env:PS_EXE
    if (-not $psExe) {
        $psExe = 'powershell.exe'
    }
    $helperStartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $helperStartInfo.FileName = $psExe
    $helperStartInfo.WorkingDirectory = $WorkingDirectory
    $helperStartInfo.UseShellExecute = $true
    $helperStartInfo.Verb = 'runas'
    $helperStartInfo.CreateNoWindow = $true
    $helperStartInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $helperStartInfo.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ' + (ConvertTo-ProcessArgumentString -ArgumentList @($helperPath))

    $process = [System.Diagnostics.Process]::Start($helperStartInfo)
    Write-Host ('Started log forwarder PID: ' + $process.Id)
    Write-Host ('Combined console log: ' + $combinedPath)
}

function Invoke-PawInitOnlyProcess {
    param(
        [string]$FilePath,
        [string]$WorkingDirectory
    )

    $startInfo = New-PawProcessStartInfo `
        -FilePath $FilePath `
        -WorkingDirectory $WorkingDirectory `
        -ArgumentList @('--init-only') `
        -ExtraEnvironment @{ QWENPAW_INIT_DEFAULT_ONLY = '1' }

    $process = [System.Diagnostics.Process]::Start($startInfo)
    $process.WaitForExit()
    return $process.ExitCode
}

$launchMode = $env:LAUNCH_MODE
$launchExe = $env:LAUNCH_EXE
$launchWorkDir = $env:LAUNCH_WORKDIR

if (-not $launchMode) { throw 'LAUNCH_MODE is empty.' }
if (-not $launchExe) { throw 'LAUNCH_EXE is empty.' }
if (-not $launchWorkDir) { throw 'LAUNCH_WORKDIR is empty.' }
if (-not (Test-Path -LiteralPath $launchExe)) { throw "paw.exe not found: $launchExe" }

if ($launchMode -eq 'init-only') {
    $exitCode = Invoke-PawInitOnlyProcess -FilePath $launchExe -WorkingDirectory $launchWorkDir
    if ($exitCode -ne 0) {
        throw 'Paw init failed.'
    }
    exit 0
}

if ($launchMode -ne 'start') {
    throw "Unsupported LAUNCH_MODE: $launchMode"
}

$pawArgs = @('--host', '127.0.0.1', '--port', $env:LAUNCH_PORT)
if ($env:LAUNCH_LOG_LEVEL) {
    $pawArgs += @('--log-level', $env:LAUNCH_LOG_LEVEL)
}

Start-PawDetachedProcess `
    -FilePath $launchExe `
    -WorkingDirectory $launchWorkDir `
    -ArgumentList $pawArgs `
    -StdoutPath $env:LAUNCH_STDOUT `
    -StderrPath $env:LAUNCH_STDERR

exit 0
::END_EMBEDDED_LAUNCH_PAW_PS1

::BEGIN_EMBEDDED_START_PAW_PS1
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'

function ConvertTo-ProcessArgumentString {
    param([string[]]$ArgumentList)

    function Escape-ProcessArgument([string]$Value) {
        if ($null -eq $Value) { return '""' }
        if ($Value -notmatch '[\s"]') { return $Value }
        return '"' + ($Value -replace '(\\*)("|$)', '$1$1\"$2') + '"'
    }

    return (($ArgumentList | ForEach-Object { Escape-ProcessArgument $_ }) -join ' ')
}

function ConvertTo-PowerShellSingleQuotedString {
    param([string]$Value)

    return "'" + ($Value -replace "'", "''") + "'"
}

function Copy-PawProcessEnvironment {
    param(
        [System.Diagnostics.ProcessStartInfo]$StartInfo,
        [hashtable]$ExtraEnvironment = @{}
    )

    foreach ($entry in [Environment]::GetEnvironmentVariables([EnvironmentVariableTarget]::Process).GetEnumerator()) {
        if ($entry.Key -ieq 'QWENPAW_INIT_DEFAULT_ONLY') {
            continue
        }
        $StartInfo.Environment[$entry.Key] = [string]$entry.Value
    }

    foreach ($key in $ExtraEnvironment.Keys) {
        $StartInfo.Environment[$key] = [string]$ExtraEnvironment[$key]
    }
}

function New-PawProcessStartInfo {
    param(
        [string]$FilePath,
        [string]$WorkingDirectory,
        [string[]]$ArgumentList,
        [hashtable]$ExtraEnvironment = @{}
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.Arguments = ConvertTo-ProcessArgumentString -ArgumentList $ArgumentList
    Copy-PawProcessEnvironment -StartInfo $startInfo -ExtraEnvironment $ExtraEnvironment
    return $startInfo
}

function Start-PawDetachedProcess {
    param(
        [string]$FilePath,
        [string]$WorkingDirectory,
        [string[]]$ArgumentList,
        [string]$StdoutPath,
        [string]$StderrPath
    )

    foreach ($path in @($StdoutPath, $StderrPath)) {
        $dir = Split-Path -Parent $path
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
    }

    $argumentString = ConvertTo-ProcessArgumentString -ArgumentList $ArgumentList
    $combinedPath = [System.IO.Path]::ChangeExtension($StdoutPath, '.console.log')
    $header = @(
        '==== Paw launch ====',
        'time: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),
        'exe: ' + $FilePath,
        'workdir: ' + $WorkingDirectory,
        'args: ' + $argumentString,
        ''
    ) -join [Environment]::NewLine
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($StdoutPath, $header, $utf8NoBom)
    [System.IO.File]::WriteAllText($StderrPath, $header + 'stderr is merged into stdout/console log by the launcher.' + [Environment]::NewLine, $utf8NoBom)
    [System.IO.File]::WriteAllText($combinedPath, $header, $utf8NoBom)

    $helperScript = @"
`$ErrorActionPreference = 'Continue'
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue) {
    `$global:PSNativeCommandUseErrorActionPreference = `$false
}
`$filePath = $(ConvertTo-PowerShellSingleQuotedString $FilePath)
`$workingDirectory = $(ConvertTo-PowerShellSingleQuotedString $WorkingDirectory)
`$stdoutPath = $(ConvertTo-PowerShellSingleQuotedString $StdoutPath)
`$combinedPath = $(ConvertTo-PowerShellSingleQuotedString $combinedPath)
`$argumentList = @(
$(
    ($ArgumentList | ForEach-Object { '    ' + (ConvertTo-PowerShellSingleQuotedString $_) }) -join ",`r`n"
)
)
`$utf8NoBom = New-Object System.Text.UTF8Encoding(`$false)
try {
    [Console]::OutputEncoding = `$utf8NoBom
    [Console]::InputEncoding = `$utf8NoBom
    `$OutputEncoding = `$utf8NoBom
    & chcp.com 65001 | Out-Null
} catch {
}
function Append-Line([string]`$path, [string]`$line) {
    [System.IO.File]::AppendAllText(`$path, `$line + [Environment]::NewLine, `$utf8NoBom)
}
`$env:PYTHONUNBUFFERED = '1'
`$env:PYTHONUTF8 = '1'
`$env:PYTHONIOENCODING = 'utf-8'
Push-Location `$workingDirectory
try {
    Append-Line `$stdoutPath ('paw.exe command: ' + `$filePath + ' ' + $(ConvertTo-PowerShellSingleQuotedString $argumentString))
    Append-Line `$combinedPath ('paw.exe command: ' + `$filePath + ' ' + $(ConvertTo-PowerShellSingleQuotedString $argumentString))
    & `$filePath @argumentList 2>&1 | ForEach-Object {
        if (`$_ -is [System.Management.Automation.ErrorRecord]) {
            `$line = `$_.Exception.Message
        } else {
            `$line = [string]`$_
        }
        Append-Line `$stdoutPath `$line
        Append-Line `$combinedPath `$line
    }
    `$exitCode = `$LASTEXITCODE
    Append-Line `$stdoutPath ('paw.exe exited with code: ' + `$exitCode)
    Append-Line `$combinedPath ('paw.exe exited with code: ' + `$exitCode)
    exit `$exitCode
} finally {
    Pop-Location
}
"@

    $helperPath = Join-Path ([System.IO.Path]::GetTempPath()) ('paw-log-forwarder-' + [System.Guid]::NewGuid().ToString('N') + '.ps1')
    [System.IO.File]::WriteAllText($helperPath, $helperScript, $utf8NoBom)

    $psExe = $env:PS_EXE
    if (-not $psExe) {
        $psExe = 'powershell.exe'
    }
    $helperStartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $helperStartInfo.FileName = $psExe
    $helperStartInfo.WorkingDirectory = $WorkingDirectory
    $helperStartInfo.UseShellExecute = $true
    $helperStartInfo.Verb = 'runas'
    $helperStartInfo.CreateNoWindow = $true
    $helperStartInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $helperStartInfo.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ' + (ConvertTo-ProcessArgumentString -ArgumentList @($helperPath))

    $process = [System.Diagnostics.Process]::Start($helperStartInfo)
    Write-Host ('Started log forwarder PID: ' + $process.Id)
    Write-Host ('Combined console log: ' + $combinedPath)
}

function Get-NowMs {
    $now = Get-Date
    return [int64]$now.TimeOfDay.TotalMilliseconds
}

function Write-ElapsedSeconds {
    param(
        [int64]$StartMs,
        [int64]$EndMs,
        [string]$Label
    )

    $elapsed = ($EndMs - $StartMs) / 1000
    Write-Host ('{0}: {1:N3}s' -f $Label, $elapsed)
}

function Exit-WithElapsed {
    param(
        [int]$ExitCode,
        [int64]$ScriptStartMs
    )

    Write-ElapsedSeconds -StartMs $ScriptStartMs -EndMs (Get-NowMs) -Label 'script total elapsed'
    exit $ExitCode
}

function Set-LogLevelValue {
    param([string]$RequestedLogLevel)

    $allowed = @('critical', 'error', 'warning', 'info', 'debug', 'trace')
    $normalized = $RequestedLogLevel.ToLowerInvariant()
    if ($allowed -notcontains $normalized) {
        Write-Host "Invalid log level: $RequestedLogLevel"
        Write-Host ('Allowed values: ' + ($allowed -join ', '))
        return $null
    }

    return $normalized
}

function Resolve-StartupOptions {
    param([string[]]$RawArguments)

    $options = [ordered]@{
        Port = '8088'
        ExtractOnly = $false
        InitOnly = $false
        LogLevel = ''
        CopyDevcoTools = ($env:COPY_DEVCO_TOOLS -eq '1')
    }

    for ($i = 0; $i -lt $RawArguments.Count; $i++) {
        $arg = $RawArguments[$i]
        switch -Regex ($arg) {
            '^(?i)--init-only$|^(?i)init$' {
                $options.InitOnly = $true
                continue
            }
            '^(?i)--extract-only$|^(?i)extract$' {
                $options.ExtractOnly = $true
                continue
            }
            '^(?i)--copy-devco-tools$|^(?i)copy-devco-tools$' {
                $options.CopyDevcoTools = $true
                continue
            }
            '^(?i)--log-level$|^(?i)log-level$' {
                if ($i + 1 -ge $RawArguments.Count) {
                    Write-Host "Missing value for $arg."
                    throw 'Missing log level value.'
                }
                $logLevel = Set-LogLevelValue -RequestedLogLevel $RawArguments[$i + 1]
                if (-not $logLevel) {
                    throw 'Invalid log level.'
                }
                $options.LogLevel = $logLevel
                $i++
                continue
            }
            default {
                $options.Port = $arg
            }
        }
    }

    return $options
}

function Ensure-PawRuntime {
    param(
        [string]$WorkDir,
        [string]$RuntimeTarget,
        [string]$RuntimeArchive,
        [switch]$OverwriteRuntimeFiles
    )

    if ((-not $OverwriteRuntimeFiles) -and (Test-Path -LiteralPath (Join-Path $RuntimeTarget 'paw.exe'))) {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime already exists"
        Write-Host "Using existing Paw runtime: $RuntimeTarget"
        return
    }

    if (Test-Path -LiteralPath $RuntimeArchive) {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime extract begin"
        Write-Host "Extracting Paw runtime to install directory: $RuntimeTarget"
        $tmp = $RuntimeTarget + '.__extracting'
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        Expand-Archive -LiteralPath $RuntimeArchive -DestinationPath $tmp -Force
        if (-not (Test-Path -LiteralPath (Join-Path $tmp 'paw.exe'))) {
            throw 'paw.exe not found after extracting runtime archive'
        }

        if ((Test-Path -LiteralPath $RuntimeTarget) -and $OverwriteRuntimeFiles) {
            foreach ($name in @('paw.exe', '_internal')) {
                $src = Join-Path $tmp $name
                $dst = Join-Path $RuntimeTarget $name
                if (-not (Test-Path -LiteralPath $src)) {
                    throw "$name not found after extracting runtime archive"
                }
                if (Test-Path -LiteralPath $dst) {
                    Remove-Item -LiteralPath $dst -Recurse -Force
                }
                Move-Item -LiteralPath $src -Destination $dst
            }
            Remove-Item -LiteralPath $tmp -Recurse -Force
            Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime files overwritten"
            return
        }

        if (Test-Path -LiteralPath $RuntimeTarget) {
            Remove-Item -LiteralPath $RuntimeTarget -Recurse -Force
        }
        Move-Item -LiteralPath $tmp -Destination $RuntimeTarget
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime extract done"
        return
    }

    if (Test-Path -LiteralPath (Join-Path $WorkDir 'paw.exe')) {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] legacy runtime ready"
        Write-Host "Using legacy paw.exe: $(Join-Path $WorkDir 'paw.exe')"
        return
    }

    throw "Neither `"$RuntimeTarget\paw.exe`" nor `"$RuntimeArchive`" was found."
}

function Update-PawRuntimeForInit {
    param(
        [string]$WorkDir,
        [string]$RuntimeTarget,
        [string]$RuntimeArchive
    )

    $devcoPawDir = Join-Path $RuntimeTarget '.devco_paw'
    $runtimeExists = Test-Path -LiteralPath $RuntimeTarget
    $archiveExists = Test-Path -LiteralPath $RuntimeArchive

    if (-not $archiveExists) {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime archive not found, skip update: $RuntimeArchive"
        return
    }

    if ($runtimeExists) {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime refresh begin (preserving .devco_paw)"
        Write-Host "Refreshing Paw runtime, preserving .devco_paw: $RuntimeTarget"

        $devcoBackup = $null
        if (Test-Path -LiteralPath $devcoPawDir) {
            $devcoBackup = Join-Path $WorkDir ('.devco_paw.__backup_' + (Get-Date -Format 'yyyyMMddHHmmss'))
            Write-Host "Preserving .devco_paw -> $devcoBackup"
            Move-Item -LiteralPath $devcoPawDir -Destination $devcoBackup -Force
        }

        Remove-Item -LiteralPath $RuntimeTarget -Recurse -Force

        $tmp = $RuntimeTarget + '.__extracting'
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        try {
            Expand-Archive -LiteralPath $RuntimeArchive -DestinationPath $tmp -Force
            if (-not (Test-Path -LiteralPath (Join-Path $tmp 'paw.exe'))) {
                throw 'paw.exe not found after extracting runtime archive'
            }
        } catch {
            if (Test-Path -LiteralPath $tmp) {
                Remove-Item -LiteralPath $tmp -Recurse -Force
            }
            if ($devcoBackup -and (Test-Path -LiteralPath $devcoBackup)) {
                New-Item -ItemType Directory -Force -Path $RuntimeTarget | Out-Null
                Move-Item -LiteralPath $devcoBackup -Destination $devcoPawDir -Force
            }
            throw
        }
        Move-Item -LiteralPath $tmp -Destination $RuntimeTarget

        if ($devcoBackup -and (Test-Path -LiteralPath $devcoBackup)) {
            Move-Item -LiteralPath $devcoBackup -Destination $devcoPawDir -Force
            Write-Host "Restored .devco_paw into refreshed runtime"
        }

        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime refresh done"
        return
    }

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime extract begin"
    Write-Host "Extracting Paw runtime to install directory: $RuntimeTarget"
    $tmp = $RuntimeTarget + '.__extracting'
    if (Test-Path -LiteralPath $tmp) {
        Remove-Item -LiteralPath $tmp -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Expand-Archive -LiteralPath $RuntimeArchive -DestinationPath $tmp -Force
    if (-not (Test-Path -LiteralPath (Join-Path $tmp 'paw.exe'))) {
        throw 'paw.exe not found after extracting runtime archive'
    }
    Move-Item -LiteralPath $tmp -Destination $RuntimeTarget
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime extract done"
}

function Resolve-ExistingPawExe {
    param(
        [string]$WorkDir,
        [string]$RuntimeTarget
    )

    $runtimePawExe = Join-Path $RuntimeTarget 'paw.exe'
    if (Test-Path -LiteralPath $runtimePawExe) {
        Write-Host "Using existing Paw runtime: $RuntimeTarget"
        return $runtimePawExe
    }

    $legacyPawExe = Join-Path $WorkDir 'paw.exe'
    if (Test-Path -LiteralPath $legacyPawExe) {
        Write-Host "Using legacy paw.exe: $legacyPawExe"
        return $legacyPawExe
    }

    throw "paw.exe not found. Run start-paw.cmd with no arguments first to extract paw-runtime.zip."
}

function Remove-PawSecretDir {
    param([string]$SecretDir)

    if ([string]::IsNullOrWhiteSpace($SecretDir)) {
        return
    }

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] remove secret dir begin: $SecretDir"
    if (-not (Test-Path -LiteralPath $SecretDir)) {
        Write-Host "Secret directory not found, skip remove: $SecretDir"
        return
    }

    Remove-Item -LiteralPath $SecretDir -Recurse -Force
    if (Test-Path -LiteralPath $SecretDir) {
        throw "Failed to remove secret directory: $SecretDir"
    }

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] remove secret dir done: $SecretDir"
}

function Invoke-PawInitOnly {
    param(
        [string]$WorkDir,
        [string]$PawExe
    )

    if (-not (Test-Path -LiteralPath $PawExe)) {
        throw "paw.exe not found: $PawExe"
    }

    Write-Host 'Running Paw init only...'
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] init-only begin"
    $startInfo = New-PawProcessStartInfo `
        -FilePath $PawExe `
        -WorkingDirectory $WorkDir `
        -ArgumentList @('--init-only') `
        -ExtraEnvironment @{ QWENPAW_INIT_DEFAULT_ONLY = '1' }
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        throw 'Paw init failed.'
    }
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] init-only done"
    Write-Host 'Paw init completed.'
}

function Start-PawProcess {
    param(
        [string]$WorkDir,
        [string]$PawExe,
        [string]$Port,
        [string]$StdoutPath,
        [string]$StderrPath,
        [string]$LogLevel,
        [string]$LogDir
    )

    if (-not (Test-Path -LiteralPath $PawExe)) {
        throw "paw.exe not found: $PawExe"
    }
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    }

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] start process begin"
    $pawArgs = @('--host', '127.0.0.1', '--port', $Port)
    if ($LogLevel) {
        $pawArgs += @('--log-level', $LogLevel)
    }

    Start-PawDetachedProcess `
        -FilePath $PawExe `
        -WorkingDirectory $WorkDir `
        -ArgumentList $pawArgs `
        -StdoutPath $StdoutPath `
        -StderrPath $StderrPath
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] start process done"
}

function Wait-ForPawReady {
    param([string]$Port)

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] wait ready begin"
    Write-Host "Waiting for Paw to start on port $Port..."
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while ([DateTime]::UtcNow -lt $deadline) {
        $client = $null
        try {
            $client = New-Object Net.Sockets.TcpClient
            $async = $client.BeginConnect('127.0.0.1', [int]$Port, $null, $null)
            if ($async.AsyncWaitHandle.WaitOne(200, $false)) {
                $client.EndConnect($async)
                Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] wait ready done"
                Write-Host 'Paw started successfully.'
                return $true
            }
        } catch {
        } finally {
            if ($client) {
                $client.Close()
            }
        }
        Start-Sleep -Milliseconds 500
    }

    return $false
}

function Copy-DevCoTools {
    param([string]$WorkDir)

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] copy DevCoTools begin"
    $pairs = @(
        @('D:\DevCoTools\ExcelConvert', (Join-Path $WorkDir '.devco_paw\workspaces\code_generator\skills\code-gen-executor\scripts')),
        @('D:\DevCoTools\SvnUpdate', (Join-Path $WorkDir '.devco_paw\workspaces\svn_guard\skills\svn-updater\scripts')),
        @('D:\DevCoTools\VulnScanTool', (Join-Path $WorkDir '.devco_paw\workspaces\vuln_scan\skills\vuln-scan\scripts\vuln_scan_tool'))
    )

    foreach ($pair in $pairs) {
        $src = $pair[0]
        $dst = $pair[1]
        if (-not (Test-Path -LiteralPath $src)) {
            Write-Host "Skip DevCoTools source not found: $src"
            continue
        }
        if (-not (Test-Path -LiteralPath $dst)) {
            New-Item -ItemType Directory -Force -Path $dst | Out-Null
        }

        $hasContent = Get-ChildItem -LiteralPath $dst -Force -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hasContent) {
            Write-Host "Destination already has content, skip copy: $dst"
            continue
        }

        & robocopy $src $dst /E /R:2 /W:1 | Out-Null
        if ($LASTEXITCODE -ge 8) {
            throw "Failed to copy $src to $dst"
        }
        Write-Host "Copied $src to $dst"
    }
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] copy DevCoTools done"
}

$scriptStartMs = Get-NowMs

try {
    $options = Resolve-StartupOptions -RawArguments $Arguments
    $workDir = (Resolve-Path -LiteralPath $PSScriptRoot).Path.TrimEnd('\')
    $runtimeDir = Join-Path $workDir 'paw-runtime'
    $runtimeZip = Join-Path $workDir 'paw-runtime.zip'
    $pawExe = Join-Path $runtimeDir 'paw.exe'

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    $copyDevcoToolsValue = if ($options.CopyDevcoTools) { '1' } else { '0' }
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] start-paw begin, port=$($options.Port), extract_only=$([int]$options.ExtractOnly), init_only=$([int]$options.InitOnly), log_level=$($options.LogLevel), copy_devco_tools=$copyDevcoToolsValue"

    if ($options.ExtractOnly) {
        Ensure-PawRuntime -WorkDir $workDir -RuntimeTarget $runtimeDir -RuntimeArchive $runtimeZip -OverwriteRuntimeFiles
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime ready"
        Write-Host 'Paw runtime extract completed.'
        Exit-WithElapsed -ExitCode 0 -ScriptStartMs $scriptStartMs
    }

    if ($options.InitOnly) {
        Update-PawRuntimeForInit -WorkDir $workDir -RuntimeTarget $runtimeDir -RuntimeArchive $runtimeZip
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime ready"
        $pawExe = Resolve-ExistingPawExe -WorkDir $workDir -RuntimeTarget $runtimeDir
    } else {
        Ensure-PawRuntime -WorkDir $workDir -RuntimeTarget $runtimeDir -RuntimeArchive $runtimeZip
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runtime ready"
        if (-not (Test-Path -LiteralPath $pawExe)) {
            $pawExe = Join-Path $workDir 'paw.exe'
        }
    }

    $pawExeDir = Split-Path -Parent $pawExe
    $logDir = Join-Path $pawExeDir 'log'
    $pawStdout = Join-Path $logDir "paw-$($options.Port).out.log"
    $pawStderr = Join-Path $logDir "paw-$($options.Port).err.log"
    Write-Host 'Paw process logs:'
    Write-Host "  $pawStdout"
    Write-Host "  $pawStderr"

    if ($options.InitOnly) {
        $secretDirs = @(
            (Join-Path $workDir '.devco_paw.secret'),
            (Join-Path $pawExeDir '.devco_paw.secret')
        )
        Write-Host 'Removing Paw secret directories before init...'
        foreach ($secretDir in $secretDirs) {
            Remove-PawSecretDir -SecretDir $secretDir
        }
        Invoke-PawInitOnly -WorkDir $workDir -PawExe $pawExe
        Exit-WithElapsed -ExitCode 0 -ScriptStartMs $scriptStartMs
    }

    $pawStartMs = Get-NowMs
    Start-PawProcess `
        -WorkDir $workDir `
        -PawExe $pawExe `
        -Port $options.Port `
        -StdoutPath $pawStdout `
        -StderrPath $pawStderr `
        -LogLevel $options.LogLevel `
        -LogDir $logDir
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] paw process launched"

    if (-not (Wait-ForPawReady -Port $options.Port)) {
        Write-Host "Paw did not start successfully on port $($options.Port); skip DevCoTools copy."
        Write-Host 'Check logs:'
        Write-Host "  $pawStdout"
        Write-Host "  $pawStderr"
        Exit-WithElapsed -ExitCode 1 -ScriptStartMs $scriptStartMs
    }

    Write-ElapsedSeconds -StartMs $pawStartMs -EndMs (Get-NowMs) -Label 'paw startup elapsed'

    if ($options.CopyDevcoTools) {
        Copy-DevCoTools -WorkDir $workDir
    } else {
        Write-Host 'DevCoTools copy disabled. Use --copy-devco-tools or set COPY_DEVCO_TOOLS=1 to enable.'
    }

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] start-paw done"
    Exit-WithElapsed -ExitCode 0 -ScriptStartMs $scriptStartMs
} catch {
    Write-Host "Error: $($_.Exception.Message)"
    Exit-WithElapsed -ExitCode 1 -ScriptStartMs $scriptStartMs
}
::END_EMBEDDED_START_PAW_PS1
