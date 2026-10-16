@echo off
REM One-Time Setup for Windows - Never run again after this
REM This enables pre-commit hooks for ALL projects automatically

echo ========================================
echo ONE-TIME SETUP FOR ALL PROJECTS
echo Never need to run this again!
echo ========================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Install Python first from python.org
    pause
    exit /b 1
)

REM Install pre-commit globally
echo 📦 Installing pre-commit...
pip install pre-commit

REM Create the global hook script
set GLOBAL_HOOKS=%USERPROFILE%\.githooks
if not exist "%GLOBAL_HOOKS%" mkdir "%GLOBAL_HOOKS%"

REM Create pre-commit hook that works everywhere
echo #!/bin/sh > "%GLOBAL_HOOKS%\pre-commit"
echo if [ -f .pre-commit-config.yaml ]; then >> "%GLOBAL_HOOKS%\pre-commit"
echo   pre-commit run --all-files >> "%GLOBAL_HOOKS%\pre-commit"
echo fi >> "%GLOBAL_HOOKS%\pre-commit"

REM Create commit-msg hook
echo #!/bin/sh > "%GLOBAL_HOOKS%\commit-msg"
echo if [ -f .pre-commit-config.yaml ]; then >> "%GLOBAL_HOOKS%\commit-msg"
echo   pre-commit run --hook-stage commit-msg --commit-msg-filename $1 >> "%GLOBAL_HOOKS%\commit-msg"
echo fi >> "%GLOBAL_HOOKS%\commit-msg"

REM Configure git to use global hooks for ALL repositories
git config --global core.hooksPath "%GLOBAL_HOOKS%"

echo.
echo ✅ SETUP COMPLETE!
echo.
echo 🎯 What this did:
echo   • Installed pre-commit globally
echo   • Set up global git hooks for ALL repositories
echo   • Every git commit in any project will now run security checks
echo.
echo 🚀 From now on:
echo   • Clone any repo with .pre-commit-config.yaml
echo   • Hooks work automatically - no setup needed
echo   • Every commit runs security checks
echo.
echo 💡 You NEVER need to run any setup script again!
echo    All projects work automatically.
echo.
pause