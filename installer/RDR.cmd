@echo off
title RDR - Radio Distribution Room
cd /d "%~dp0"
"%~dp0python\python.exe" run.py %*
if errorlevel 1 pause
