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

REM Create pre-commit hook that generates reports
echo @echo off > "%GLOBAL_HOOKS%\pre-commit"
echo. >> "%GLOBAL_HOOKS%\pre-commit"
echo REM Delete old security report if exists >> "%GLOBAL_HOOKS%\pre-commit"
echo if exist security-scan-report-*.txt del security-scan-report-*.txt >> "%GLOBAL_HOOKS%\pre-commit"
echo. >> "%GLOBAL_HOOKS%\pre-commit"
echo REM Run pre-commit and save report if config exists >> "%GLOBAL_HOOKS%\pre-commit"
echo if exist .pre-commit-config.yaml ( >> "%GLOBAL_HOOKS%\pre-commit"
echo     for /f "tokens=2-4 delims=/ " %%%%a in ('date /t') do set mydate=%%%%c%%%%a%%%%b >> "%GLOBAL_HOOKS%\pre-commit"
echo     for /f "tokens=1-2 delims=/:" %%%%a in ('time /t') do set mytime=%%%%a%%%%b >> "%GLOBAL_HOOKS%\pre-commit"
echo     set mytime=%%mytime: =0%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     set REPORT_FILE=security-scan-report-%%mydate%%-%%mytime%%.txt >> "%GLOBAL_HOOKS%\pre-commit"
echo. >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo ========================================== ^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo Security Scan Report ^>^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo Generated: %%date%% %%time%% ^>^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo ========================================== ^>^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo. ^>^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo. >> "%GLOBAL_HOOKS%\pre-commit"
echo     pre-commit run --all-files ^>^> %%REPORT_FILE%% 2^>^&1 >> "%GLOBAL_HOOKS%\pre-commit"
echo     set EXIT_CODE=%%ERRORLEVEL%% >> "%GLOBAL_HOOKS%\pre-commit"
echo. >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo. ^>^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo ========================================== ^>^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo Scan completed: %%date%% %%time%% ^>^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo Report saved: %%REPORT_FILE%% ^>^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     echo ========================================== ^>^> %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo. >> "%GLOBAL_HOOKS%\pre-commit"
echo     type %%REPORT_FILE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo     exit /b %%EXIT_CODE%% >> "%GLOBAL_HOOKS%\pre-commit"
echo ) else ( >> "%GLOBAL_HOOKS%\pre-commit"
echo     pre-commit run --all-files >> "%GLOBAL_HOOKS%\pre-commit"
echo ) >> "%GLOBAL_HOOKS%\pre-commit"

REM Run initial scan with report
echo.
echo ✨ Running initial security scan...
if exist ".pre-commit-config.yaml" (
    for /f "tokens=2-4 delims=/ " %%a in ('date /t') do set mydate=%%c%%a%%b
    for /f "tokens=1-2 delims=/:" %%a in ('time /t') do set mytime=%%a%%b
    set mytime=%mytime: =0%
    set REPORT_FILE=security-scan-report-%mydate%-%mytime%.txt
    
    (
        echo ==========================================
        echo Security Scan Report - Initial Setup
        echo Generated: %date% %time%
        echo ==========================================
        echo.
        pre-commit run --all-files 2>&1
        echo.
        echo ==========================================
        echo Scan completed: %date% %time%
        echo Report saved: %REPORT_FILE%
        echo ==========================================
    ) > "%REPORT_FILE%" 2>&1
    
    type "%REPORT_FILE%"
    echo.
    echo 📊 Report saved to: %REPORT_FILE%
)

echo.
echo ✅ SETUP COMPLETE!
echo.
echo 🎯 What this did:
echo   • Installed pre-commit globally
echo   • Set up global git hooks for ALL repositories
echo   • Every git commit will generate a security scan report
echo   • Old reports are automatically deleted
echo.
echo 🚀 From now on - every commit:
echo   • Runs security checks automatically
echo   • Generates timestamped report: security-scan-report-YYYYMMDD-HHMM.txt
echo   • Deletes previous report
echo   • Shows results in terminal
echo.
echo 💡 You NEVER need to run any setup script again!
echo.
pause
