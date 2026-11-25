# Astra editor core — notes and controls

The disk writer remains `editing.py`. This library has no files, JSON parser, Qt, audio, or history stack. It implements indexed event transactions, final-assignment validation, CC64 conflict resolution, immutable prepared results and a fenced accept/discard protocol. It is an independent implementation. The forward/reverse pairing and separation of editing from the save boundary are general editor design ideas; there is no borrowed DAW code.

## Build, test, enable

From the repository root, using the application's existing Python dependencies and a C++17 toolchain:

```sh
cmake -S native/editor_core -B build/editor-core -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON
cmake --build build/editor-core --config Release
ctest --test-dir build/editor-core -C Release --output-on-failure
cmake --install build/editor-core --config Release --prefix build/editor-core/install
```

On Linux, for example:

```sh
export PYTHONPATH="$PWD/src"
export ASTRA_EDITOR_CORE_LIBRARY="$PWD/build/editor-core/install/libastra_editor_core.so"
export ASTRA_TEST_EDITOR_CORE_LIBRARY="$ASTRA_EDITOR_CORE_LIBRARY"
python -m pytest tests/test_native_editor_core.py tests/test_editing.py tests/test_edit_transactions.py
python native/editor_core/bench.py --library "$ASTRA_EDITOR_CORE_LIBRARY"
```

On Windows PowerShell, run the same CMake commands (use a generator/compiler architecture matching Python, e.g. `-A x64` for a Visual Studio generator), then:

```powershell
$env:PYTHONPATH = "$PWD/src"
$env:ASTRA_EDITOR_CORE_LIBRARY = "$PWD/build/editor-core/install/astra_editor_core.dll"
$env:ASTRA_TEST_EDITOR_CORE_LIBRARY = $env:ASTRA_EDITOR_CORE_LIBRARY
python -m pytest tests/test_native_editor_core.py tests/test_editing.py tests/test_edit_transactions.py
python native/editor_core/bench.py --library $env:ASTRA_EDITOR_CORE_LIBRARY
```

The new pytest fixture builds and loads a real library by default if `ASTRA_TEST_EDITOR_CORE_LIBRARY` is not supplied. The native test also runs directly via CTest, without Python. The CMake target can be added as a subdirectory of a parent native build. For deployment put the shared library beside `astra/native_editor.py`; a source checkout also looks in `native/editor_core/`. `ASTRA_EDITOR_CORE_LIBRARY` is an explicit override. Missing or incompatible binaries use the Python path; an explicit load failure is logged. `ASTRA_EDITOR_CORE=0` forces the Python path for matched measurements. These are process environment switches, not project or application settings. Restart after replacing a library.

## Ownership and transaction boundary

- Every edit starts with the existing process `edit_lock` and `_base_index` CAS. Private projections are bounded to two cached projects per process. A hit requires the revision and both head/body file markers to match. This cache never supplies `current()`, preview jobs, or GUI input. It stores no native undo stack and never borrows a GUI document.
- Cold checkout reads and migrates the real body, validates the clock/document, and checks that the ordinary retirement and validation path leaves the projected base stable. Native rows use group-local integer keys translated once from document IDs. They are stable across list compaction within the epoch. Python owns UUID assignment, opaque event payloads, `deleted_ids`, provenance representation, source fields, range, and all structural data. The edit command and its new rows are detached from the caller. No entire row array crosses the FFI on a warm edit.
- Native preparation checks epoch, sequence, revision bytes and a monotonically increasing attempt number. This attempt number is internal; it is not the GUI request ID or a replacement for receipt deduplication. Only one token is pending per session, and it never leaves the writer's lock scope. Undo, redo and legacy publication drop the projection. An observed external head replacement also causes a reload. A reload gives a fresh epoch even if the content revision recurs. Disk format and the scope of disk CAS are unchanged; a native token is not a cross-process version fence.
- The C++ state uses immutable AVL nodes for ID lookup and a second index by `(channel, mapped_tick, order)` for CC64. Prepare checks deletes, sequential assignments and additions in that order, validates final touched rows before doing any displacement, and stages only changed paths. Group membership and winners come from affected tick buckets, not from scanning all controls. A change record contains both sides and stable order; swapping the sides is the native inverse. Displaced rows have a full before side. The Python revision history retains full inverse payloads, including opaque metadata and deletion-log ordering. Redo selects the original immutable revision, not a re-executed command.
- `TempoMap.to_beat` and Python `round(beat * PPQ)` still define mapped ticks. Only the bucket label crosses the boundary; event seconds are never moved to a tick. Python also supplies the existing opening/score-end and duration thresholds. The core enforces the edit rules on typed rows, allows overlapping notes and score-end-crossing tails, and preserves untouched historical CC clusters. Unusual legacy representations, exotic commands, native validation refusals and structural edits use the original Python implementation. In particular, falling back lets Python produce the original failure precedence and repair previously invalid bases. A view whose `rhythm_initial` still depends on a fresh filesystem lookup is not warmed from that body.
- The adapter splices a private candidate from the sparse delta. Equal-looking assignments in a changed batch keep their supplied JSON representation without falsely marking their origin manual. Unknown event fields and root/source fields never need to be reconstructed from native rows. For a no-op it returns a detached view without writing a revision or modifying history. For a change it binds the proposed revision, saves the immutable body, assembles the detached view, then saves the head through the original shared writer. Accept occurs only on the existing replace observation, including replace-then-throw. Precommit errors discard. If cache finalization fails after commit, the cache is dropped and the original disk outcome is not changed or retried.
- `astra_editor_core.h` defines the size/versioned C ABI and ownership contract. The session owns committed and pending index roots. A separate prepared handle owns read-only result buffers; accepting, discarding, or freeing the session does not invalidate them. The caller frees that handle exactly once. The ctypes adapter copies the sparse routing evidence while the result is alive. Calls use `CDLL` and there are no callbacks into Python from C++. Calls on a session, including destruction, must be serialized by its owner.

## Measure the boundary that changed

`bench.py` measures one cold and a configurable number of warm durable note adds on matched synthetic projects, with separate body/head save timings. It asserts actual native acceptance, not just library discovery. Run both orders or repeat on the same machine and disk if comparing small differences. Use the real Qt benchmark separately, against private copies of the same project and with the existing edit tracing, for queue drain, receipt delivery, publication and preview timing. Verify every final note and individual undo/redo. The benchmark is not a proxy for that protocol.

Warm Python work still includes the head read, file markers, the full revision JSON write and flush, the head replace, and assembly of a fresh ordinary Python view. The view entails shallow copies of plain row dictionaries and copies of nested payloads where necessary. Deleting/normalizing rows also compacts the affected Python array. These are not full model validation or native marshalling costs, but they are real. Cold checkout still parses and fully validates a body. None of these changes removes the separate roughly one-second per-note receipt/publication/preview scheduling delay reported on the existing real project.
