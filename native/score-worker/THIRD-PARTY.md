# Third-party notices and inputs

The worker links source in this tree as part of MuseScore 4.6.5. Refer to the actual license notices, including:
- MuseScore musescore/LICENSE.txt, GPL 3; vendored code retains its own notices.
- KDDockWidgets musescore/src/framework/dockwindow/thirdparty/KDDockWidgets/LICENSE.GPL.txt and LICENSE.txt, used under the applicable open-source terms.
- FreeType 2.13.1 musescore/src/framework/draw/thirdparty/freetype/freetype-2.13.1/docs/FTL.TXT and its accompanying notices.
- HarfBuzz 7.1.0 third-party/harfbuzz-7.1.0/harfbuzz/COPYING, and original license references in the source.
- BeatRoot notices remain in musescore/src/importexport/midi/beatroot/ and source headers.
- Music notation and text fonts have author notices in musescore/fonts/; these must not be stripped or treated as licensed by Astra. See musescore/fonts/README.md for the project's font licensing references.
- Bundled upstream UI resources are from the MuseScore source release. Upstream demonstration works, visual-test projects, and large autobot/sample fixtures are not included; no blanket relicense of such works is asserted.

External links for distinct components from the reference build:
- Windows build prerequisites repository: https://github.com/musescore/musescore_prebuild_win_deps (not copied wholesale or redistributed).
- The linked zlib static library was built with zlib 1.2.8 headers. Upstream source: https://zlib.net/fossils/zlib-1.2.8.tar.gz . Use the original external build inputs only to reproduce the archived reference; they are not an endorsement of an older version for new products.
- Qt 6.9.2 C++ kit: https://download.qt.io/official_releases/qt/6.9/6.9.2/single/ . Exact DLL notices/corresponding source for a binary distribution are a separate artifact.
- No ASIO SDK, user VST plug-ins, SoundFont binary, dependency executable/DLL, or app state is bundled in this source archive. This archive is only for the score worker.
The upstream zlib source archive is fetched separately from the official URL above. Follow the README for the reference SHA-256 and local build.

