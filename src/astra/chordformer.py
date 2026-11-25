"""Offline official ChordFormer ensemble, isolated process and content-bound cache."""
from __future__ import annotations
from .release_paths import data_root, runtime_env_root

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = data_root()
SOURCE_COMMIT = "3da11c078c802b3f5a2ff0f6f8ec184a41537844"
ADAPTER_VERSION = 1


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(audio):
    installed = runtime_env_root(ROOT)
    source, model = installed / "vendor/chordformer", installed / "models/chordformer"
    pin_path = source / "ASTRA-PIN.json"
    if not pin_path.is_file():
        raise FileNotFoundError("ChordFormer 未安装，请运行 scripts/install-chordformer.ps1")
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    if pin.get("commit") != SOURCE_COMMIT or len(pin.get("weights", {})) != 5:
        raise ValueError("ChordFormer 安装版本不完整")
    for folder, items in ((source, pin["source_files"]), (model, pin["weights"])):
        for name, expected in items.items():
            path = (folder / name).resolve()
            if not path.is_relative_to(folder.resolve()) or _sha(path) != expected:
                raise ValueError(f"ChordFormer 文件校验失败：{name}")
    return {"version": ADAPTER_VERSION, "input_sha256": _sha(audio),
            "source_commit": SOURCE_COMMIT, "source_files": pin["source_files"],
            "weights": pin["weights"], "vocabulary": "full",
            "vocabulary_sha256": _sha(source / "data/full_chord_list.txt"),
            "inference": "official_full_sequence_5fold_mean_global_hmm"}


def _key(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate(result):
    duration = result["duration_seconds"]
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid chord analysis duration")
    segments = result["segments"]
    if not isinstance(segments, list) or not segments:
        raise ValueError("Empty chord analysis")
    cursor = 0.0
    for segment in segments:
        start, end, label = segment["start"], segment["end"], segment["label"]
        if (not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end))
                or abs(start - cursor) > 1e-6 or end <= start or end > duration + 1e-6
                or not isinstance(label, str) or not label.strip()):
            raise ValueError("Invalid chord segment timeline")
        cursor = end
    if abs(cursor - duration) > 1e-6:
        raise ValueError("Incomplete chord timeline")
    return result


def _cached(path, identity):
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        payload = envelope["result"]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if envelope["identity"] != identity or envelope["result_sha256"] != digest:
            return None
        return _validate(payload)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def cache_is_available(audio_path, cache_dir):
    """Same complete identity and integrity checks as analyze_audio_chords."""
    try:
        identity = _identity(Path(audio_path).resolve(strict=True))
        return _cached(Path(cache_dir) / (_key(identity) + ".json"), identity) is not None
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _worker_environment(device):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("QT_", "QML", "PYTHONPATH"))}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1",
               PYTHONPATH=str(Path(__file__).resolve().parents[1]), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               ASTRA_CHORDFORMER_DEVICE=device, OMP_NUM_THREADS="4")
    if device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = "-1"
    return env


def _run(request, device, log):
    executable = runtime_env_root(ROOT) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        executable = Path(sys.executable)
    command = [str(executable), "-m", "astra.chordformer_worker", str(request)]
    child = subprocess.Popen(command, env=_worker_environment(device), cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace",
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        for line in child.stdout:
            log("[chordformer] " + line.rstrip())
        code = child.wait()
        if code:
            raise RuntimeError(f"ChordFormer 推理失败（退出码 {code}），请查看处理日志")
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait()
        if child.stdout:
            child.stdout.close()


def analyze_audio_chords(audio_path, cache_dir, device="cuda", log=print, force=False, cache_only=False):
    """Return original-second Harte labels; tempo/display edits never change cache.

    N/X are retained as upstream output, not fabricated chord labels. Device is
    execution metadata; verified results can be reused across CPU/GPU selection.
    force=True explicitly reruns all five models and atomically replaces cache.
    cache_only=True never starts inference, including if a prior cache probe raced.
    """
    device = {"gpu": "cuda", "auto": "cuda"}.get(device, device)
    if device not in ("cpu", "cuda"):
        raise ValueError("ChordFormer device 应为 cpu 或 cuda")
    if force and cache_only:
        raise ValueError("force 与 cache_only 不能同时启用")
    audio = Path(audio_path).resolve(strict=True)
    identity = _identity(audio)
    key = _key(identity)
    cache = Path(cache_dir).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (key + ".json")
    result = None if force else _cached(target, identity)
    if result is not None:
        log("[chordformer] 复用已校验的和弦识别缓存")
        return {**result, "cached": True, "cache_key": key, "cache_path": str(target)}
    if cache_only:
        raise FileNotFoundError("没有有效的 ChordFormer 和弦缓存，请先运行和弦分析")
    with tempfile.TemporaryDirectory(prefix=".chordformer-", dir=cache) as temporary:
        pending = Path(temporary)
        output = pending / "result.json"
        request = pending / "request.json"
        request.write_text(json.dumps({"audio": str(audio), "output": str(output),
                                       "root": str(runtime_env_root(ROOT)), "device": device}), encoding="utf-8")
        _run(request, device, log)
        result = _validate(json.loads(output.read_text(encoding="utf-8")))
        # Refuse publication if an input/model was modified while inference ran.
        if _identity(audio) != identity:
            raise ValueError("ChordFormer 输入或模型在分析过程中发生变化，请重试")
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        staged = pending / "cache.json"
        staged.write_text(json.dumps({"identity": identity, "result": result,
                                      "result_sha256": digest}, ensure_ascii=False), encoding="utf-8")
        os.replace(staged, target)
    return {**result, "cached": False, "cache_key": key, "cache_path": str(target)}
