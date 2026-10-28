@echo off
REM Build PDFCompressor.exe on Windows. Needs Python 3.10+ installed (python.org).
python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pyinstaller --noconfirm --onefile --windowed --name PDFCompressor app.py
echo.
echo Done. EXE at dist\PDFCompressor.exe
pause
