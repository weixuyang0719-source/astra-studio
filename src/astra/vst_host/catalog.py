"""VST3 x64 discovery and crash-contained catalog scanning.

A plugin path is not a sample-library path. Scanning executes plugin code only
inside a short-lived worker; the main application never loads it during scan.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time


def standard_roots():
    roots = [Path(os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files")) / "VST3"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs" / "Common" / "VST3")
    for folder in os.environ.get('ASTRA_VST3_PATH', '').split(os.pathsep):
        candidate = Path(folder.strip())
        if candidate.is_absolute() and candidate.parent != candidate:
            roots.append(candidate)
    return list(dict.fromkeys(roots))


def module_binary(path):
    path = Path(path).resolve()
    if path.suffix.lower() != ".vst3":
        raise ValueError("Only VST3 modules are supported")
    if path.is_file():
        return path
    folder = path / "Contents" / "x86_64-win"
    expected = folder / path.name
    if expected.is_file():
        return expected
    modules = list(folder.glob("*.vst3")) if folder.is_dir() else []
    if len(modules) != 1:
        raise ValueError("VST3 bundle has no unambiguous Windows x64 module")
    return modules[0]


def architecture(path):
    """Check PE header without loading a DLL; never treat existence as compatibility."""
    with module_binary(path).open("rb") as f:
        head = f.read(64)
        if len(head) != 64 or head[:2] != b"MZ":
            raise ValueError("Not a Windows PE module")
        offset = struct.unpack_from("<I", head, 60)[0]
        if offset > 16 * 1024 * 1024:
            raise ValueError("Invalid PE header offset")
        f.seek(offset)
        signature = f.read(6)
    if len(signature) != 6 or signature[:4] != b"PE\0\0":
        raise ValueError("Invalid PE signature")
    machine = struct.unpack_from("<H", signature, 4)[0]
    return {0x8664: "x64", 0x14c: "x86", 0xaa64: "arm64"}.get(machine, f"unknown-{machine:04x}")


def discover(roots=None):
    found = {}
    for root in standard_roots() if roots is None else map(Path, roots):
        if not root.is_dir():
            continue
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in list(dirs):
                if name.lower().endswith(".vst3"):
                    path = Path(directory, name).resolve()
                    found[str(path).casefold()] = path
                    dirs.remove(name)  # bundle contents are not separate plugins
            for name in files:
                if name.lower().endswith(".vst3"):
                    path = Path(directory, name).resolve()
                    found[str(path).casefold()] = path
    return sorted(found.values(), key=lambda p: str(p).casefold())


def fingerprint(path):
    binary = module_binary(path)
    stat = binary.stat()
    return {"binary": str(binary), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


SCAN_RESULT_PROTOCOL = 2
_MAX_SCAN_RESULT = 4 * 1024 * 1024


def scan_one(path, native_library, *, timeout=20, runner=subprocess.run):
    path = Path(path).resolve()
    record = {"path": str(path), "status": "failed", "scanned_at": time.time(), "classes": [],
              "scan_result_protocol": SCAN_RESULT_PROTOCOL}
    try:
        record["fingerprint"] = fingerprint(path)
        record["architecture"] = architecture(path)
        if record["architecture"] != "x64":
            raise ValueError("This host supports Windows x64 VST3 only")
        kwargs = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        from .child_command import child_command
        # Some plugins log directly to native stdout. Keep that entirely
        # separate from the structured result, with a private path per scan.
        with tempfile.TemporaryDirectory(prefix="astra-vst-scan-") as temp:
            response = Path(temp) / "result.json"
            result = runner(child_command("astra.vst_host.worker", "scan", str(Path(native_library).resolve()),
                                          str(path), str(response)), **kwargs)
            if result.returncode:
                raise RuntimeError(f"Plugin scanner exited ({result.returncode}): {result.stderr[-1500:]}")
            if not response.is_file():
                raise ValueError("Plugin scanner did not write its result")
            with response.open("rb") as stream:
                raw = stream.read(_MAX_SCAN_RESULT + 1)
            if len(raw) > _MAX_SCAN_RESULT:
                raise ValueError("Plugin scanner response is too large")
            payload = json.loads(raw)
        classes = payload.get("classes") if isinstance(payload, dict) else None
        if not isinstance(classes, list):
            raise ValueError("Plugin scanner returned malformed class metadata")
        record.update(status="ready", classes=classes)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        record["error"] = str(exc)
    return record


class Catalog:
    def __init__(self, path):
        self.path = Path(path)
        self.records = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("records"), dict):
                raise ValueError("Unsupported plugin catalog")
            self.records = data["records"]

    def scan(self, paths, library, *, full=False, timeout=20, runner=subprocess.run):
        for path in paths:
            key = str(Path(path).resolve()).casefold()
            previous = self.records.get(key)
            try:
                unchanged = previous and previous.get("fingerprint") == fingerprint(path)
            except (ValueError, OSError):
                unchanged = False
            # Like Cubase's ordinary rescan: new, changed, and failed modules.
            if not full and unchanged and previous.get("status") == "ready":
                continue
            self.records[key] = scan_one(path, library, timeout=timeout, runner=runner)
            self.save()  # retain previous scan outcomes if a later scanner crashes
        return list(self.records.values())

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "records": self.records}, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
