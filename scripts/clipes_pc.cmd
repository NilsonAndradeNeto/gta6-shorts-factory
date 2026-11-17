@echo off
rem Tarefa agendada do Windows: baixa as lives no PC e dispara a clipagem na nuvem.
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
%PY% scripts\baixar_fontes.py >> .cache\baixar_fontes_tarefa.log 2>&1
