@echo off
setlocal
cd /d "%~dp0"
where cl.exe >nul || exit /b 1
where cmake.exe >nul || exit /b 1
set "R=%CD%"
set "O=%R%\out"
if not exist "%O%" mkdir "%O%"
cmake -S "%R%\editor-core\native\editor_core" -B "%O%\editor" -G "Visual Studio 18 2026" -A x64 "-DASTRA_JSON_INCLUDE_DIR=%R%\editor-core\third_party\nlohmann-json-3.12.0\include" || exit /b 1
cmake --build "%O%\editor" --config Release --target astra_editor_core || exit /b 1
cmake -S "%R%\recovery-recycle" -B "%O%\recovery" -G "Visual Studio 18 2026" -A x64 || exit /b 1
cmake --build "%O%\recovery" --config Release --target astra_recovery_recycle || exit /b 1
cmake -S "%R%\instrument-preflight" -B "%O%\preflight" -G "Visual Studio 18 2026" -A x64 || exit /b 1
cmake --build "%O%\preflight" --config Release --target astra_instrument_start || exit /b 1
cmake -S "%R%\vst-host" -B "%O%\vst-host" -G "Visual Studio 18 2026" -A x64 -DASTRA_VST_BUILD_FIXTURE=OFF -DBUILD_TESTING=OFF || exit /b 1
cmake --build "%O%\vst-host" --config Release --target astra_vst_host || exit /b 1
pushd "%O%"
cl /nologo /std:c++17 /utf-8 /O2 /EHsc /LD "%R%\audio-core\core.cpp" /Fe:astra_audio_core.dll || exit /b 1
cl /nologo /std:c++17 /utf-8 /O2 /EHsc /LD "%R%\stream-cache\stream_cache.cpp" /Fe:astra_stream_cache.dll || exit /b 1
cl /nologo /std:c++17 /utf-8 /O2 /MT /EHsc /LD "%R%\kept-endpoint\kept_endpoint.retire-only.cpp" /Fe:astra_kept_endpoint.dll || exit /b 1
cl /nologo /std:c++17 /utf-8 /O2 /EHsc /W4 /WX /LD "%R%\transport-panel\native_transport_panel.cpp" user32.lib gdi32.lib gdiplus.lib /Fe:native_transport_panel.dll || exit /b 1
cl /nologo /std:c++17 /O2 /EHsc /LD "%R%\audio-components\midi_scheduler.cpp" /Fe:astra_midi_scheduler.dll || exit /b 1
cl /nologo /std:c++17 /O2 /EHsc /LD "%R%\audio-components\midi_winmm.cpp" /Fe:astra_midi_winmm.dll || exit /b 1
cl /nologo /std:c++17 /O2 /EHsc /LD "%R%\audio-components\pcm_loader.cpp" /Fe:astra_pcm_loader.dll || exit /b 1
cl /nologo /std:c++17 /O2 /EHsc /LD "%R%\audio-components\tempo_cache.cpp" /Fe:astra_tempo_cache.dll || exit /b 1
cl /nologo /std:c++17 /O2 /EHsc /LD "%R%\audio-components\native_wasapi.cpp" ole32.lib uuid.lib avrt.lib propsys.lib /Fe:astra_wasapi_host.dll || exit /b 1
set "RT=%R%\vst-rt-bridge\vst_host\rt"
cl /nologo /std:c++17 /O2 /MD /EHsc /utf-8 /DNDEBUG /DNOMINMAX /DASTRA_RT_BRIDGE_BUILD /LD "%RT%\bridge.cpp" "%RT%\live.cpp" "%RT%\pcm_bus.cpp" "%RT%\kernel.cpp" /link /out:astra_vst_rt_bridge.dll || exit /b 1
set "SDK=%R%\vendor\vst3sdk"
set "HOST=%O%\vst-host"
set "FX=%R%\vst-isolated-fx\host"
cl /nologo /std:c++17 /O2 /MD /EHsc /utf-8 /DNOMINMAX /DUNICODE /D_UNICODE /DNDEBUG /I"%SDK%" /I"%SDK%\public.sdk\source\vst\hosting" /c "%FX%\astra_vst_host.cpp" "%FX%\plugprovider_relay.cpp" || exit /b 1
link /nologo /dll /out:astra_vst_host.dll astra_vst_host.obj "%HOST%\astra_vst_host.dir\Release\module_win32.obj" plugprovider_relay.obj "%HOST%\astra_vst_host.dir\Release\memorystream.obj" /libpath:"%HOST%\lib\Release" sdk_hosting.lib sdk_common.lib base.lib pluginterfaces.lib user32.lib kernel32.lib ole32.lib shell32.lib comctl32.lib gdi32.lib winspool.lib oleaut32.lib uuid.lib comdlg32.lib advapi32.lib || exit /b 1
move /y astra_vst_host.dll astra_vst_isolated_fx_host.dll >nul || exit /b 1
popd
