@echo off
cd /d "%~dp0"
echo Installing and checking requirements...
python -m pip install -q -r requirements.txt
start "" cmd /c "timeout /t 4 >nul & start http://localhost:5000"
echo Eror is running. Keep this window open. Close it to stop.
python server.py
pause
