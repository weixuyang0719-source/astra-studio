param(
    [string]$BuildDir = (Join-Path $PSScriptRoot "build"),
    [string]$CMake = "cmake.exe"
)
$ErrorActionPreference = "Stop"
$pa = Join-Path $PSScriptRoot "portaudio-v19.7.0"
$sdk = Join-Path $PSScriptRoot "ASIOSDK"
if (!(Test-Path $pa) -or !(Test-Path $sdk)) { throw "Extract the complete source archive first." }
& $CMake -S $pa -B $BuildDir -G "Visual Studio 18 2026" -A x64 "-DASIOSDK_ROOT_DIR=$sdk" -DPA_USE_ASIO=ON -DPA_USE_DS=ON -DPA_USE_WMME=ON -DPA_USE_WASAPI=ON -DPA_USE_WDMKS=ON -DPA_BUILD_SHARED=ON -DPA_BUILD_STATIC=OFF -DPA_BUILD_EXAMPLES=OFF -DPA_BUILD_TESTS=OFF -DPA_DISABLE_INSTALL=ON -DFETCHCONTENT_FULLY_DISCONNECTED=ON "-DCMAKE_POLICY_VERSION_MINIMUM=3.5"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& $CMake --build $BuildDir --config Release --target portaudio --parallel 2
exit $LASTEXITCODE
