# SPDX-License-Identifier: GPL-3.0-only
"""Launch the source checkout in a separate development workspace."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("mido", "music21", "numpy", "PySide6", "soundfile")


def args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check prerequisites without launching or writing.")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "Astra Studio Dev" / "workspace")
    parser.add_argument("--score-worker", type=Path, help="Explicit path to a compatible x64 score worker.")
    parser.add_argument("--score-qt", type=Path, help="Explicit path to a compatible Qt 6.9.2 runtime/developer kit.")
    options = parser.parse_args()
    if bool(options.score_worker) != bool(options.score_qt):
        parser.error("--score-worker and --score-qt must be supplied together.")
    options.data_dir = options.data_dir.expanduser().resolve()
    if options.data_dir == ROOT or ROOT in options.data_dir.parents:
        parser.error("Use a data directory outside the source repository.")
    if options.score_worker:
        options.score_worker = options.score_worker.expanduser().resolve()
        options.score_qt = options.score_qt.expanduser().resolve()
        if not options.score_worker.is_file() or not options.score_qt.is_dir():
            parser.error("The score worker must exist as a file and its Qt path as a directory.")
    return options


def main():
    options = args()
    missing = [module for module in REQUIRED if importlib.util.find_spec(module) is None]
    entry = ROOT / "src" / "astra" / "launcher.py"
    if options.check:
        print(json.dumps({"source_entry_found": entry.is_file(), "missing_python_prerequisites": missing,
                          "score_worker_configured": bool(options.score_worker),
                          "writes": False, "audio_device_probe": False}))
        return 0 if entry.is_file() and not missing else 1
    if missing:
        print("Missing dependencies: " + ", ".join(missing), file=sys.stderr)
        return 1
    if sys.platform != "win32":
        print("The Astra Studio desktop application currently targets Windows x64.", file=sys.stderr)
        return 2
    for filename in ("release-4-seal.json", ".session.lock", "portable-manifest.json"):
        if (options.data_dir / filename).exists():
            print("The selected path resembles a release or active session; choose a fresh workspace.", file=sys.stderr)
            return 2
    options.data_dir.mkdir(parents=True, exist_ok=True)
    (options.data_dir / "projects").mkdir(exist_ok=True)
    env = dict(ASTRA_RELEASE_DATA_ROOT=str(options.data_dir),
               ASTRA_RELEASE_PROJECTS_ROOT=str(options.data_dir / "projects"),
               ASTRA_RELEASE_ISOLATED_PROJECTS="1", ASTRA_RELEASE_NUMBER="4",
               ASTRA_RUNTIME_ENV_ROOT=str(ROOT))
    for key in ("ASTRA_NATIVE_SCORE_WORKER_EXE", "ASTRA_NATIVE_SCORE_QT_ROOT"):
        os.environ.pop(key, None)
    if options.score_worker:
        env.update(ASTRA_NATIVE_SCORE_WORKER_EXE=str(options.score_worker), ASTRA_NATIVE_SCORE_QT_ROOT=str(options.score_qt))
    os.environ.update(env)
    sys.path.insert(0, str(ROOT / "src"))
    from astra.launcher import main as launch
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
