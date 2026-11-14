@echo off
rem sapper launcher for cmd.exe / PowerShell
rem Loads .env.local (model gateway + key), then runs the CLI.
setlocal
set "ENVFILE=%~dp0..\.env.local"
if exist "%ENVFILE%" (
  for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%ENVFILE%") do (
    if not "%%~a"=="" set "%%~a=%%~b"
  )
)
set "SAPPER_PY=py -3"
where py >nul 2>nul || set "SAPPER_PY=python"
%SAPPER_PY% "%~dp0sapper_win.py" %*
