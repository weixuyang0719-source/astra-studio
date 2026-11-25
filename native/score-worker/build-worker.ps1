# SPDX-License-Identifier: GPL-3.0-only
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$QtPrefix,
    [Parameter(Mandatory)][string]$WindowsDependencies,
    [Parameter(Mandatory)][string]$BuildRoot,
    [switch]$ConfigureOnly,
    [int]$Jobs=6
)
$ErrorActionPreference='Stop'
$qt=(Resolve-Path -LiteralPath $QtPrefix).Path
$deps=(Resolve-Path -LiteralPath $WindowsDependencies).Path
if (-not (Test-Path -LiteralPath (Join-Path $qt 'lib\cmake\Qt6\Qt6Config.cmake'))) { throw 'QtPrefix must point to a Qt 6.9.2 C++ developer kit.' }
if (-not (Test-Path -LiteralPath (Join-Path $deps 'libx64\zlibstat.lib'))) { throw 'WindowsDependencies must point to the MuseScore-compatible Windows dependency prefix with libx64\zlibstat.lib.' }
if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) { throw 'Run from a Visual Studio x64 Native Tools/Developer PowerShell (MSVC 19.51 reference).' }
$src=Join-Path $PSScriptRoot 'musescore'
$b=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($BuildRoot)
if ($b -eq $src -or $b.StartsWith($src+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Use a separate build directory.' }
if (Test-Path -LiteralPath (Join-Path $b 'CMakeCache.txt')) { throw 'Choose a new build directory or remove the previous configuration yourself.' }
New-Item -ItemType Directory -Path "$b\_deps\harfbuzz" -Force | Out-Null
Copy-Item -Path (Join-Path $PSScriptRoot 'third-party\harfbuzz-7.1.0\*') -Destination "$b\_deps\harfbuzz" -Recurse -Force
$cfg=Join-Path $PSScriptRoot 'formal4-config.cmake'
cmake -C $cfg -S $src -B $b -G Ninja "-DCMAKE_TRY_COMPILE_CONFIGURATION=Release" "-DCMAKE_PREFIX_PATH=$qt" "-DQt6_DIR=$qt\lib\cmake\Qt6" "-DFETCHCONTENT_SOURCE_DIR_MUSESCORE_PREBUILD_WIN_DEPS=$deps" "-DCMAKE_INSTALL_PREFIX=$b\install"
if($LASTEXITCODE){throw "CMake configuration failed ($LASTEXITCODE)."}
if($ConfigureOnly){return}
cmake --build $b --target MuseScoreStudio --parallel $Jobs
if($LASTEXITCODE){throw "Worker compilation failed ($LASTEXITCODE)."}
$exe=Join-Path $b 'MuseScore4.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "Worker target completed but did not produce $exe." }
$artifact=Join-Path $b 'AstraNativeScore.exe'
Copy-Item -LiteralPath $exe -Destination $artifact
Get-FileHash -LiteralPath $artifact -Algorithm SHA256

