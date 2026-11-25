# AstraNativeScore (MuseScore Studio 4.6.5)

The Formal 4 score worker starts from upstream
[MuseScore Studio v4.6.5](https://github.com/musescore/MuseScore/releases/tag/v4.6.5),
Git commit 653f38d8ea80ebd2b084c2b5e5521a32b00983c7.
The archival reference worker SHA-256 is dceff43d4b8fd93c786b598f505675895652b04eddeaae3421b6a64e30a79d19.
Astra's score-worker changes use GPL-3.0-only, with the original notices preserved.

The six precise modified C++ translation units, their headers and relocated
CMake integration are under overlay/. This tree deliberately does not mirror
the entire upstream MuseScore project, its demo scores or packaged binaries.
source-origin.json records how the overlay was matched to the production binary.

## Prepare and configure

1. Obtain the official source tree at the pinned commit above. Point to the
   actual source directory, not a prebuilt installer. The upstream sources are
   not downloaded by scripts in this repository.
2. From this repository, run:

       python native\score-worker\prepare_source.py D:\src\MuseScore-4.6.5 D:\build\astra-score-source

   This builds a separate working copy without editing the upstream directory,
   checking the overlay files and the upstream paths they replace. Add
   --verify-only to check these identities without generating a working copy.
3. In the new working copy, either select compatible upstream Windows
   development dependencies, or obtain the official
   [zlib 1.2.8 source archive](https://zlib.net/fossils/zlib-1.2.8.tar.gz).
   Verify SHA-256
   36658cb768a54c1d4dec43c3116c27ed893e88b02ecfcb44f2166f9c0b7f2a0d
   and save it as third-party/zlib-1.2.8.tar.gz inside the working copy (not
   in the Git repository). Run the included Windows x64 source build:

       .\build-zlib.ps1 -WorkDir D:\build\astra-zlib -Prefix D:\sdk\astra-zlib

4. In an x64 Visual Studio 2026 Developer PowerShell (reference MSVC 19.51,
   Windows SDK 10.0.26100), with CMake, Ninja and Python in PATH, configure
   against the matching Qt 6.9.2 C++ developer kit:

       .\build-worker.ps1 -QtPrefix D:\sdk\Qt\6.9.2\msvc2022_64 -WindowsDependencies D:\sdk\astra-zlib -BuildRoot D:\build\astra-score-build -ConfigureOnly

   To build the worker rather than only generating Ninja files, use a new
   build directory and omit -ConfigureOnly. Keep the worker with runtime
   libraries from that same Qt build. The output is AstraNativeScore.exe.
   A rebuild is not promised to have the original PE timestamps or hash.

HarfBuzz 7.1.0 and its build notices are included; FreeType 2.13.1 and stock
engraving/data assets are part of the pinned upstream tree. Qt is a separate
runtime/build requirement, not redistributed here. See [third-party notices](THIRD-PARTY.md).
