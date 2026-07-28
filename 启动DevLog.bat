@echo off
chcp 65001 >nul
title DevLog 开发工作日志
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [错误] 没有找到 Python
  echo   请先到 https://www.python.org/downloads/ 安装
  echo   安装时记得勾选 "Add Python to PATH"
  echo.
  pause
  exit /b 1
)

python -m devlog %*
if errorlevel 1 pause
