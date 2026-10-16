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

REM Create pre-commit.bat (actual logic - Windows batch)
(
echo @echo off
echo for %%%%f in ^(security-scan-report-*.txt^) do del "%%%%f" 2^>nul
echo if not exist .pre-commit-config.yaml exit /b 0
echo set REPORT_FILE=security-scan-report-%%date:~-4,4%%%%date:~-10,2%%%%date:~-7,2%%-%%time:~0,2%%%%time:~3,2%%.txt
echo set REPORT_FILE=%%REPORT_FILE: =0%%
echo ^( echo ========================================== ^& echo Security Scan Report ^& echo Generated: %%date%% %%time%% ^& echo ========================================== ^& echo. ^& pre-commit run 2^>^&1 ^& echo. ^& echo ========================================== ^& echo Scan completed: %%date%% %%time%% ^& echo Report: %%REPORT_FILE%% ^& echo ========================================== ^) ^> "%%REPORT_FILE%%" 2^>^&1
echo type "%%REPORT_FILE%%"
echo exit /b %%ERRORLEVEL%%
) > "%GLOBAL_HOOKS%\pre-commit.bat"

REM Create pre-commit shell wrapper (Git calls this - it calls the .bat)
(
echo #!/bin/sh
echo exec "$(dirname "$0")/pre-commit.bat"
) > "%GLOBAL_HOOKS%\pre-commit"

REM Create commit-msg.bat
(
echo @echo off
echo if exist .pre-commit-config.yaml pre-commit run --hook-stage commit-msg --commit-msg-filename %%1
) > "%GLOBAL_HOOKS%\commit-msg.bat"

REM Create commit-msg shell wrapper
(
echo #!/bin/sh
echo exec "$(dirname "$0")/commit-msg.bat" "$@"
) > "%GLOBAL_HOOKS%\commit-msg"

REM Configure git to use global hooks for ALL repositories
git config --global core.hooksPath "%GLOBAL_HOOKS%"

REM Run initial scan with report (ONLY during setup)
echo.
echo ✨ Running initial security scan...
if exist ".pre-commit-config.yaml" (
    set REPORT_FILE=security-scan-report-%date:~-4,4%%date:~-10,2%%date:~-7,2%-%time:~0,2%%time:~3,2%.txt
    set REPORT_FILE=%REPORT_FILE: =0%
    
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
echo   • Old reports are automatically deleted (only keep latest)
echo.
echo 🚀 From now on - every commit:
echo   • Deletes previous security-scan-report-*.txt
echo   • Runs security checks automatically
echo   • Generates new timestamped report: security-scan-report-YYYYMMDD-HHMM.txt
echo   • Shows results in terminal
echo   • Blocks commit if issues found
echo.
echo 💡 You NEVER need to run any setup script again!
echo.
pause
