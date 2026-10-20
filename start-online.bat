@echo off
setlocal
cd /d "%~dp0"
echo.
echo ===== Eror online (trial link) =====
echo.
echo Checking requirements...
python -m pip install -q -r requirements.txt
echo.
set /p EROR_PASSWORD=Choose a password for your online link (letters and numbers only): 
if "%EROR_PASSWORD%"=="" (
  echo A password is required so strangers cannot use your server.
  pause
  exit /b
)
where cloudflared >nul 2>nul
if errorlevel 1 (
  echo Installing the tunnel tool, one time only...
  winget install -e --id Cloudflare.cloudflared --accept-source-agreements --accept-package-agreements
)
set "CF=cloudflared"
where cloudflared >nul 2>nul
if errorlevel 1 set "CF=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if /i "%CF%"=="cloudflared" goto run
if exist "%CF%" goto run
echo.
echo Could not install the tunnel tool automatically.
echo Download cloudflared for Windows from https://github.com/cloudflare/cloudflared/releases
echo Save it in this folder as cloudflared.exe, then run this file again.
pause
exit /b
:run
start "Eror server" cmd /k python server.py
timeout /t 4 >nul
echo.
echo Wait for a link ending in trycloudflare.com below, then open it on any device.
echo When the browser asks to sign in: leave the username empty, password = the one you chose.
echo Keep both black windows open. Close them to stop sharing.
echo.
"%CF%" tunnel --url http://localhost:5000
pause
