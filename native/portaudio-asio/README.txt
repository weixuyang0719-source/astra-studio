Astra PortAudio / ASIO host dependency, corresponding source

This is the corresponding source for the privately staged Windows x64 build of
PortAudio V19.7.0 linked with the Steinberg ASIO SDK 2.3.4. The combined DLL is
distributed under GNU GPL version 3; the upstream component license notices
are retained. No SDK logo, driver samples, or proprietary developer agreement
files are included.

PortAudio v19.7.0 commit 147dd722548358763a8b649b3e4b41dfffbcfbb6
https://github.com/PortAudio/portaudio/tree/147dd722548358763a8b649b3e4b41dfffbcfbb6
Copyright Ross Bencina and Phil Burk and other contributors; MIT terms in
portaudio-v19.7.0/LICENSE.txt.

Steinberg ASIO SDK 2.3.4, archive dated 2025-10-15
https://download.steinberg.net/sdk_downloads/ASIO-SDK_2.3.4_2025-10-15.zip
Original SDK zip SHA256:
d5ebf0c20dd2c5f43771fd0c1418f4b361bf52434ee670097cfa6b3a335e2eca
Copyright (c) 2025 Steinberg Media Technologies GmbH.
For files covered by ASIOSDK/LICENSE.txt, the GPL version 3 alternative is
selected. The Windows host helper files in ASIOSDK/host and ASIOSDK/host/pc
carry their own embedded three-clause permissive terms; their full copyright,
conditions and disclaimer are also reproduced in ASIO-HOST-NOTICES.txt.

Build: install Visual Studio 18 2026 MSVC x64 C++ build tools and Windows SDK
10.0.26100; use CMake 4.3.1 or later with its VS 18 generator, then run
  powershell -File .ebuild.ps1
Results: build/Release/portaudio_x64.dll. In Astra the installed name is
libportaudio64bit-asio.dll. This enables the same five backends as the
comparison upstream binary, not a new driver or system-setting installer.
All 55 exported symbols were checked against the prior packaged DLL. The
binary is not advertised as byte-identical to that previous DLL; no device
was opened during validation.
