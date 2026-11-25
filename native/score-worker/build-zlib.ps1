# SPDX-License-Identifier: GPL-3.0-only
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$Prefix,
    [string]$PlatformToolset = "v145"
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command msbuild.exe -ErrorAction SilentlyContinue) -or -not (Get-Command ml64.exe -ErrorAction SilentlyContinue)) {
    throw 'Run in a Visual Studio x64 Native Tools command prompt / Developer PowerShell.'
}
$work=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($WorkDir)
$out=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Prefix)
$zOpt=Join-Path $PSScriptRoot 'third-party\zlib-1.2.8.tar.gz'
if ((Get-FileHash -LiteralPath $zOpt -Algorithm SHA256).Hash -ne '36658CB768A54C1D4DEC43C3116C27ED893E88B02ECFCB44F2166F9C0B7F2A0D') { throw 'zlib source archive hash mismatch' }
if (Test-Path -LiteralPath (Join-Path $work 'zlib-1.2.8')) { throw 'Choose a new zlib work folder.' }
if (Test-Path -LiteralPath (Join-Path $out 'libx64\zlibstat.lib')) { throw 'Refusing to replace an existing dependency library.' }
New-Item -ItemType Directory -Path $work -Force | Out-Null
tar -xzf $zOpt -C $work
if($LASTEXITCODE){throw 'zlib source extraction failed.'}
$src=Join-Path $work 'zlib-1.2.8'
$project=Join-Path $src 'contrib\vstudio\vc11\zlibstat.vcxproj'
$asm=Join-Path $src 'contrib\masmx64'
Push-Location -LiteralPath $asm
try {
    ml64 /nologo /c /Zi /Fogvmat64.obj gvmat64.asm
    if ($LASTEXITCODE) { throw 'zlib gvmat64 assembly failed.'}
    ml64 /nologo /c /Zi /Foinffasx64.obj inffasx64.asm
    if ($LASTEXITCODE) { throw 'zlib inffasx64 assembly failed.'}
} finally { Pop-Location }
msbuild $project /nologo /m /t:Build /p:Configuration=Release /p:Platform=x64 "/p:PlatformToolset=$PlatformToolset" /p:WindowsTargetPlatformVersion=10.0
if($LASTEXITCODE){throw 'zlibstatic compilation failed.'}
$lib=Join-Path (Split-Path -Parent $project) 'x64\ZlibStatRelease\zlibstat.lib'
if(-not (Test-Path -LiteralPath $lib)){throw 'Compiler did not produce zlibstat.lib.'}
# The MuseScore prebuilts add the Windows minizip helper to zlibstat.
$winSource=Join-Path $src 'contrib\minizip\iowin32.c'
$winObj=Join-Path $work 'iowin32.obj'
cl /nologo /c /O2 /MD /DWIN64 /DZLIB_WINAPI /D_CRT_SECURE_NO_WARNINGS "/I$src" "/Fo$winObj" $winSource
if($LASTEXITCODE){throw 'minizip Windows file I/O compilation failed.'}
$combined=Join-Path $work 'zlibstat-with-minizip.lib'
lib /nologo "/OUT:$combined" $lib $winObj
if($LASTEXITCODE){throw 'combining zlibstat with the Windows minizip helper failed.'}
$lib=$combined
New-Item -ItemType Directory -Path (Join-Path $out 'libx64'), (Join-Path $out 'include\zlib') -Force | Out-Null
Copy-Item -LiteralPath $lib -Destination (Join-Path $out 'libx64\zlibstat.lib')
$headers=@('zlib.h','zconf.h') | ForEach-Object { Join-Path $src $_ }
$headers+=@(Get-ChildItem -LiteralPath (Join-Path $src 'contrib\minizip') -Filter '*.h' -File | ForEach-Object FullName)
Copy-Item -LiteralPath $headers -Destination (Join-Path $out 'include\zlib')
Get-FileHash -LiteralPath (Join-Path $out 'libx64\zlibstat.lib') -Algorithm SHA256

