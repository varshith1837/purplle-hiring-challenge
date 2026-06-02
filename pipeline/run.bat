@echo off
REM ---------------------------------------------------------------------------
REM pipeline\run.bat
REM One command to process all CCTV clips through the detection pipeline.
REM
REM Usage:
REM   pipeline\run.bat [INPUT_DIR] [OUTPUT_DIR] [API_URL]
REM
REM Defaults:
REM   INPUT_DIR  = ..\dataset\CCTV Footage
REM   OUTPUT_DIR = ..\output
REM   API_URL    = http://localhost:8000
REM ---------------------------------------------------------------------------
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.."
set "PROJECT_ROOT=%CD%"
popd

if "%~1"=="" (
    set "INPUT_DIR=%PROJECT_ROOT%\..\dataset\CCTV Footage"
) else (
    set "INPUT_DIR=%~1"
)

if "%~2"=="" (
    set "OUTPUT_DIR=%PROJECT_ROOT%\output"
) else (
    set "OUTPUT_DIR=%~2"
)

if "%~3"=="" (
    set "API_URL=http://localhost:8000"
) else (
    set "API_URL=%~3"
)

set "EVENTS_FILE=%OUTPUT_DIR%\events.jsonl"

echo ==================================================================
echo  Store Intelligence - CCTV Detection Pipeline
echo ==================================================================
echo  Project root : %PROJECT_ROOT%
echo  Input dir    : %INPUT_DIR%
echo  Output dir   : %OUTPUT_DIR%
echo  Events file  : %EVENTS_FILE%
echo  API URL      : %API_URL%
echo ==================================================================

REM Create output directory
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"

REM Clear previous events file
type nul > "%EVENTS_FILE%"

REM Count video files
set /a VIDEO_COUNT=0
for %%f in ("%INPUT_DIR%\*.mp4") do set /a VIDEO_COUNT+=1

if %VIDEO_COUNT% equ 0 (
    echo ERROR: No .mp4 files found in %INPUT_DIR% 1>&2
    exit /b 1
)
echo Found %VIDEO_COUNT% video file(s)
echo.

REM Run pipeline
cd /d "%PROJECT_ROOT%"
python -m pipeline.detect ^
    --input "%INPUT_DIR%" ^
    --output "%EVENTS_FILE%" ^
    --store-id STORE_BLR_001 ^
    --api-url "%API_URL%" ^
    --skip-frames 3 ^
    --conf-threshold 0.35

echo.
echo ==================================================================
echo  Pipeline complete!
echo  Events written to: %EVENTS_FILE%

REM Count events
set /a EVENT_COUNT=0
for /f %%a in ('find /c /v "" ^< "%EVENTS_FILE%" 2^>nul') do set EVENT_COUNT=%%a
echo  Total events: %EVENT_COUNT%
echo ==================================================================

endlocal
