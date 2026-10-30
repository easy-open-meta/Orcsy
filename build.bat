@echo off
rem One-click packaging: version -> frontend build -> PyInstaller -> Inno Setup installer
rem Only edit version in task_arranger/__init__.py (never touch build/version.ini)
rem Output: build\Setup\task-arranger-setup-<version>.exe
setlocal
cd /d "%~dp0"
title Task Arranger Build

rem Locate uv: PATH first, then the standard per-user install locations
rem (double-click may run without the user PATH that terminals have)
set "UVEXE="
where uv >nul 2>nul && set "UVEXE=uv"
if not defined UVEXE (
    for %%P in ("%USERPROFILE%\.local\bin\uv.exe" "%USERPROFILE%\.cargo\bin\uv.exe" "%USERPROFILE%\scoop\shims\uv.exe") do (
        if exist %%P set "UVEXE=%%~P"
    )
)
if not defined UVEXE (
    echo [FAIL] uv not found. Install it first: https://docs.astral.sh/uv/
    pause
    exit /b 1
)
echo Using uv: %UVEXE%
echo ===== Task Arranger Build =====
echo.

"%UVEXE%" run python build\build.py
set RC=%errorlevel%

echo.
if "%RC%"=="0" (
    echo [OK] Installer written to build\Setup\
) else (
    echo [FAIL] Build aborted, exit code %RC%. Check the error above.
)
pause
exit /b %RC%
