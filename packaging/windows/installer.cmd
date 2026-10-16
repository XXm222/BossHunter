@echo off
setlocal
cd /d "%~dp0"
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%I"
if not defined ISCC (
    echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php
    echo Or open BossHunter.iss in Inno Setup and click Compile.
    pause
    exit /b 1
)
"%ISCC%" "%~dp0BossHunter.iss"
set "INSTALLER_RESULT=%ERRORLEVEL%"
echo.
if "%INSTALLER_RESULT%"=="0" echo SUCCESS: see dist\installers in the project folder.
pause
exit /b %INSTALLER_RESULT%
