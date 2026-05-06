@echo off
echo ========================================
echo   Starting HSI Trading Backend (port 6000)
echo ========================================
cd backend
..\backend\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 6000
