# SPDX-License-Identifier: GPL-3.0-only
"""Create a fresh Astra score-worker source tree from the pinned public MuseScore release."""
from __future__ import annotations

import argparse
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import re
import shutil

HERE = Path(__file__).resolve().parent


def git_blob(data):
    return sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def matches(data, expected):
    lf = data.replace(b"\r\n", b"\n")
    return any(git_blob(b) == expected for b in (data, lf, lf.replace(b"\n", b"\r\n")))


def verify(upstream, base):
    for name, expected in base["git_blobs"].items():
        target = upstream / name
        if expected:
            if not target.is_file() or not matches(target.read_bytes(), expected):
                raise ValueError(f"Not the expected MuseScore base file: {name}")
        elif target.exists():
            raise ValueError(f"Fresh upstream copy required; unexpected file: {name}")
    for path, expected in base["astra_sha256"].items():
        if sha256((HERE / "overlay" / path).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Astra overlay hash mismatch: {path}")


def skip_local(_current, names):
    return {name for name in names
            if name in (".git", ".venv", "build.release", "build.debug", "__pycache__")
            or name.startswith(".cache") or name.endswith((".pdb", ".exe", ".dll", ".obj"))}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("upstream",type=Path,help="A clean MuseScore 4.6.5 release source directory.")
    p.add_argument("output",type=Path,help="A new working directory outside the repository.")
    p.add_argument("--verify-only",action="store_true",help="Check overlay and base without copying.")
    o=p.parse_args()
    out=o.output.resolve()
    src=o.upstream.resolve(strict=True)
    if out == src or src in out.parents or HERE == out or HERE in out.parents:
        p.error("Output must be a separate directory.")
    data=json.loads((HERE/"upstream-preimages.json").read_text(encoding="utf-8-sig"))
    verify(src,data)
    if o.verify_only:
        print(f"Verified {len(data['git_blobs'])} upstream source identities and {len(data['astra_sha256'])} overlay files.")
        return
    if out.exists():
        p.error("Output directory already exists; choose an unused path.")
    try:
        shutil.copytree(src,out/"musescore",ignore=skip_local)
        shutil.copytree(HERE/"overlay",out/"musescore",dirs_exist_ok=True)
        shutil.copytree(HERE/"third-party",out/"third-party")
        for n in ("build-worker.ps1","build-zlib.ps1","formal4-config.cmake","LICENSE","THIRD-PARTY.md","source-origin.json"):
            shutil.copyfile(HERE/n,out/n)
    except OSError:
        p.exit(1,"Failed preparing source. The existing upstream directory was not changed.\n")
    print(f"Prepared worker build workspace: {out}")


if __name__ == "__main__":
    main()
