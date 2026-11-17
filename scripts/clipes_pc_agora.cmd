@echo off
rem Dia de lancamento: baixa lives e publica 1 clipe na hora.
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
%PY% scripts\baixar_fontes.py --agora 1 --fontes 2 >> .cache\baixar_fontes_tarefa.log 2>&1
