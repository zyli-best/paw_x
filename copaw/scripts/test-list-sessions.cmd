@echo off
setlocal DisableDelayedExpansion

set "host=10.159.221.147"
set "port=8088"
set "timeout=30"
set "base_url="
set "agent_id="
set "user_id="
set "channel="
set "filter_user="
set "filter_channel="
set "token=%QWENPAW_API_TOKEN%"

:parse_args
if "%~1"=="" goto build_request
if "%~1"=="--help" goto help
if "%~1"=="-h" goto help
if [%2]==[] (
    >&2 echo FAIL: Expected a value for "%~1". Run "%~nx0 --help" for usage.
    exit /b 2
)

if "%~1"=="--host" (
    set "host=%~2"
) else if "%~1"=="--ip" (
    set "host=%~2"
) else if "%~1"=="--port" (
    set "port=%~2"
) else if "%~1"=="--base-url" (
    set "base_url=%~2"
) else if "%~1"=="--timeout" (
    set "timeout=%~2"
) else if "%~1"=="--agent-id" (
    set "agent_id=%~2"
) else if "%~1"=="--user-id" (
    set "user_id=%~2"
    set "filter_user=1"
) else if "%~1"=="--channel" (
    set "channel=%~2"
    set "filter_channel=1"
) else if "%~1"=="--token" (
    set "token=%~2"
) else (
    >&2 echo FAIL: Unknown option "%~1". Run "%~nx0 --help" for usage.
    exit /b 2
)
shift
shift
goto parse_args

:build_request
setlocal EnableDelayedExpansion
if not defined timeout goto invalid_timeout
for /f "delims=0123456789." %%D in ("!timeout!") do goto invalid_timeout
set "timeout_digits=!timeout:.=!"
if "!timeout_digits:0=!"=="" goto invalid_timeout

if not defined base_url (
    if not defined port goto invalid_port
    for /f "delims=0123456789" %%D in ("!port!") do goto invalid_port
    if "!port:0=!"=="" goto invalid_port
    if not defined host goto invalid_address
    if not "!host:/=!"=="!host!" goto invalid_address
    if not "!host:\=!"=="!host!" goto invalid_address
    if not "!host::=!"=="!host!" if not "!host:~0,1!"=="[" set "host=[!host!]"
    set "base_url=http://!host!:!port!"
)
if /i not "!base_url:~0,7!"=="http://" if /i not "!base_url:~0,8!"=="https://" goto invalid_address
if not "!base_url:@=!"=="!base_url!" goto invalid_address
if not "!base_url:?=!"=="!base_url!" goto invalid_address
if not "!base_url:#=!"=="!base_url!" goto invalid_address

:trim_slash
if "!base_url:~-1!"=="/" (
    set "base_url=!base_url:~0,-1!"
    goto trim_slash
)
if not "!base_url:~-4!"=="/api" set "base_url=!base_url!/api"
set "url=!base_url!/chats"

set "curl_options="
if defined filter_user set "curl_options=!curl_options! --data-urlencode "user_id=!user_id!""
if defined filter_channel set "curl_options=!curl_options! --data-urlencode "channel=!channel!""
if defined agent_id set "curl_options=!curl_options! --header "X-Agent-Id: !agent_id!""
if defined token set "curl_options=!curl_options! --header "Authorization: Bearer !token!""

where.exe curl.exe >nul 2>&1
if errorlevel 1 (
    >&2 echo FAIL: curl.exe was not found. Windows curl.exe is required.
    exit /b 1
)

set "request_dir=!TEMP!\qwenpaw-list-sessions-!RANDOM!-!RANDOM!"
mkdir "!request_dir!" 2>nul
if errorlevel 1 (
    >&2 echo FAIL: Could not create a temporary directory for the response.
    exit /b 1
)

>&2 echo GET !url! ^(timeout !timeout!s^)
curl.exe --disable --silent --show-error --globoff --noproxy "*" --proto "=http,https" --get ^
    --connect-timeout "!timeout!" --max-time "!timeout!" --header "Accept: application/json" ^
    !curl_options! --output "!request_dir!\body.json" --write-out "%%{http_code} %%{time_total}" ^
    --url "!url!" >"!request_dir!\status.txt"
set "curl_exit=!errorlevel!"
if not "!curl_exit!"=="0" (
    >&2 echo FAIL: curl.exe exited with code !curl_exit!.
    set "result=1"
    goto cleanup
)

for /f "usebackq tokens=1,2" %%A in ("!request_dir!\status.txt") do (
    set "status=%%A"
    set "elapsed=%%B"
)
if "!status!"=="200" (
    >&2 echo PASS: HTTP !status!, !elapsed!s
    set "result=0"
) else (
    >&2 echo FAIL: HTTP !status!, !elapsed!s
    set "result=1"
)
if exist "!request_dir!\body.json" (
    type "!request_dir!\body.json"
    echo(
)

:cleanup
if exist "!request_dir!\body.json" del /q "!request_dir!\body.json"
if exist "!request_dir!\status.txt" del /q "!request_dir!\status.txt"
rmdir "!request_dir!" 2>nul
exit /b !result!

:invalid_address
>&2 echo FAIL: Use an HTTP or HTTPS server address without credentials, query or fragment.
exit /b 2

:invalid_port
>&2 echo FAIL: --port must be a positive integer.
exit /b 2

:invalid_timeout
>&2 echo FAIL: --timeout must be a positive number of seconds.
exit /b 2

:help
echo Smoke-test the QwenPaw session list API ^(GET /api/chats^).
echo Usage: %~nx0 [options]
echo   --host, --ip HOST   Server IP or hostname ^(default: 10.159.221.147^)
echo   --port PORT        Server port ^(default: 8088^)
echo   --base-url URL     HTTP^(S^) server URL, overriding host and port
echo   --timeout SECONDS  Connection and total request timeout ^(default: 30^)
echo   --agent-id ID      Agent ID ^(defaults to the server's active agent^)
echo   --user-id ID       Filter by user ID
echo   --channel CHANNEL  Filter by channel
echo   --token TOKEN      Bearer token ^(defaults to QWENPAW_API_TOKEN^)
echo   -h, --help         Show this help
exit /b 0
