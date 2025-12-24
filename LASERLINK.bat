@echo off
setlocal EnableExtensions

REM === Base folder = folder chứa file .bat này ===
set "BASE=%~dp0"

REM === Đổi working directory về BASE (quan trọng cho config/log/relative paths) ===
pushd "%BASE%" || (
  echo [ERROR] Cannot access base folder: "%BASE%"
  exit /b 1
)

set "PY=%BASE%win-py310\python.exe"
set "APP=%BASE%run.py"

if not exist "%PY%" (
  echo [ERROR] Not found: "%PY%"
  popd
  exit /b 2
)

if not exist "%APP%" (
  echo [ERROR] Not found: "%APP%"
  popd
  exit /b 3
)

REM (Optional) Ép UTF-8 cho console/print nếu cần
REM set "PYTHONUTF8=1"

REM === Run app (forward tất cả tham số nếu bạn có truyền thêm) ===
"%PY%" "%APP%" %*

set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
