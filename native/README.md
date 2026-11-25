# Astra Studio 4 native source archive

Original Astra code is GPL-3.0-only; see LICENSE and LICENSE-SCOPE.md.
Third-party source retains the licenses in its directories. The SHA-256
matrix is for the **shipped original binaries**, not the source archive.
All 16 rows match an original historical build artifact. Relocated recipes
were not used to rebuild all binaries. Timestamps, PE metadata and toolchain
versions can make a new build differ from an original artifact.

## Build matrix
Use Windows x64, Visual Studio 18 (2026) x64 Native Tools command prompt,
Windows SDK 10.0.26100 and CMake 3.25+. Run Build-Native.cmd to build the
first-party native DLLs into out/; no installed copy is modified.
Historical machine-specific recipes have been excluded; use the relative-path script and
NATIVE-BUILD-PROVENANCE.json.

| Directory | Build entry |
|---|---|
| editor-core | CMake astra_editor_core; bundled MIT nlohmann/json 3.12.0 and the test source needed at configure-time |
| audio-core | core.cpp, with this exact midi_scheduler.h; cl /utf-8 |
| stream-cache | stream_cache.cpp; cl /utf-8 |
| audio-components | cl standalone MIDI scheduler, WinMM, PCM loader, tempo cache and WASAPI sources; bundled Signalsmith/stretch MIT |
| kept-endpoint | kept_endpoint.retire-only.cpp; cl /MT; original recipe also builds the stock baseline |
| transport-panel | native_transport_panel.cpp; cl /W4 /WX, User32, GDI, GDI+ |
| recovery-recycle | CMake astra_recovery_recycle |
| instrument-preflight | CMake astra_instrument_start |
| vst-host | CMake astra_vst_host using bundled SDK |
| vst-isolated-fx | C++ compilation plus SDK objects and libraries freshly built by vst-host; cl + link |
| vst-rt-bridge | bridge/live/pcm_bus/kernel; cl /MD /DASTRA_RT_BRIDGE_BUILD |
| musescore-basic | CMake AstraMuseScoreBasic; NMake, see below |

Do not interchange similarly named headers between these build trees. The
standalone MIDI scheduler is the silent-route version (C++ and header SHA
prefixes 7f99ad3d/4e6304b5), not the old base hashes in the secondary runtime
audio manifest. VST3 SDK pinned root commit 3cdf9ca5d1f5b1b21e0a86832aa4abe55607bd96;
submodule commits: base fcf9da0bd27a16f7f03773a3a39822f28f5c8477; cmake
054c9143cbb8d47fc4694e473f2ee3b4d951a8f5; pluginterfaces
4f547e8e102b47de4a8b8aaf343c73b700786372; public.sdk
586dc5e6c8012c3e4b01c79389375cbe96bdb1da. The SDK license is MIT.

## Built-in MuseScore Basic VST3
The Astra VST wrapper, its file-loader shim and the patched FluidSynth 2.3.3
sources from MuseScore Studio 4.6.5 are included. The shipped soundfont is a
separate MIT data resource, MS Basic.sf3, SHA-256
5ea2375e8bd7d8e71def1036978c1621e85b66934169b6a2744b27b9b3c2d99c;
provide the actual bundled copy as MS_BASIC_SF3. Its license and notices are
in this archive. From the x64 Native Tools shell:
    cmake -S musescore-basic -B out/musescore-basic -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release "-DMS_BASIC_SF3=X:/path/MS Basic.sf3" "-DSNDFILE_DLL=X:/path/libsndfile_x64.dll"
    cmake --build out/musescore-basic --target AstraMuseScoreBasic
SNDFILE_DLL is optional for the build, but deployment needs a compatible
dynamically loaded libsndfile to decode SF3.

## Separately tracked, not covered here
- Astra PortAudio host / PortAudio and ASIO SDK, and the full MuseScore score
  worker: separate source deliverables under their owners' audit.
- Built-in piano's libsndfile_x64.dll, VersionInfo 1.2.2.0, from the
  soundfile 0.13.1 Windows x64 wheel's _soundfile_data, SHA-256
  22518c16f9d13eda5ae5adf999c4740d7d16cc2d6b178b36dd4ac2f759a38559.
  The LGPL COPYING notice is retained. Its PE debug path identifies vcpkg's
  x64-windows-custom-rel build. Exact source/codec revisions and that build
  recipe are not provided or verified by this archive. Do not represent this
  archive as complete corresponding source for that DLL or the full release.

No user projects, profiles, recordings, credentials, tool histories, DLLs,
EXEs, object files, PDBs or soundfont binaries are included.

## B2 playback state correction

The isolated VST FX/Instrument host now treats a parameter-titles-only restart as metadata. Combined parameter value changes and performEdit still dirty the project. The source change is already applied; see the patch for audit. The separately built B2 binary used additional packaging-time debug-path redaction; this source checkout is not its packager.

