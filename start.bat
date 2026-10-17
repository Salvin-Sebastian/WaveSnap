@echo off
echo.
echo  =============================================
echo   SoundDrop — Music Downloader
echo  =============================================
echo   Starting server at http://localhost:5000
echo   Press Ctrl+C to stop
echo  =============================================
echo.

REM Check for ffmpeg (needed for MP3 conversion)
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo [WARNING] ffmpeg not found in PATH.
    echo          MP3 conversion may not work.
    echo          Install ffmpeg: https://ffmpeg.org/download.html
    echo          Or via winget:  winget install ffmpeg
    echo.
)

python app.py
pause
