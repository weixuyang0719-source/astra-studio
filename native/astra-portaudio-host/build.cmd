@echo off
setlocal
cd /d "%~dp0"
where cl.exe >nul || exit /b 1
if not exist out mkdir out
pushd out
cl /nologo /std:c++17 /O2 /EHsc /LD "%~dp0portaudio_host.cpp" /Fe:astra_portaudio_host.dll /link ole32.lib user32.lib
set "rc=%ERRORLEVEL%"
popd
exit /b %rc%
