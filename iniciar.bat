@echo off
echo Instalando dependencias...
pip install -r requirements.txt -q
echo.
echo Iniciando o app...
start http://localhost:5000
python app.py
pause
