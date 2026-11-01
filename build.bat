@echo off
rem One-click packaging: version -> frontend build -> PyInstaller -> Inno Setup installer + zip update package
rem Only edit version in orcsy/__init__.py (never touch build/version.ini)
rem Output: build\Setup\orcsy-setup-<version>.exe  (first install)
rem         build\Setup\orcsy-update-<version>.zip (self-update package)
setlocal
cd /d "%~dp0"
title Orcsy Build

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
echo ===== Orcsy Build =====
echo.

"%UVEXE%" run python build\build.py
set RC=%errorlevel%

echo.
if "%RC%"=="0" (
    rem NOTE: never put parens inside echo text here - cmd treats ) as end of block
    echo [OK] Artifacts written to build\Setup\ - setup exe + update zip
) else (
    echo [FAIL] Build aborted, exit code %RC%. Check the error above.
)
pause
exit /b %RC%
