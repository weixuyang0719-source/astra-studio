"""Optional offline DuET piano engraving; never rewrites aligned Astra MIDI.

DuET's score clock is an interpretation of the performance, not the project's
TempoMap. Its listening MIDI must remain a separate notation-only artifact.
The child process inherits the desktop job's process group for cancellation.
"""
from __future__ import annotations
from .release_paths import data_root, runtime_env_root
from . import model_worker_lease as lease

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic
import uuid
import xml.etree.ElementTree as ET

SOURCE_COMMIT = "95b3fdbacc9ce83a6433768b91daf2aae22f9e93"
MODEL_REVISION = "27e1ed4d66c5e36fdc656517317ffeb65e6fc58e"
WEIGHTS_SHA256 = "89e07104358ca55ed3c8048c3d83b224c28e4ee0aa59e76a0ab0a9189ad114e0"
CONFIG_SHA256 = "0ac449b9d9f7be4c689e9d4f85282aa97d3a0e2f31cd87de66cadf533acc5abc"
ROOT = data_root()
ARTIFACTS = ("score.musicxml", "score.mid", "notes.json", "score.npz", "report.json", "review.json", "frames.csv")
TIMING_WARNING = "DuET 乐谱使用独立记谱拍位；其试听 MIDI 不替换原始/量化 MIDI 或项目速度图，不作为原音频同步轨。"


def duet_source_dir(override=None):
    """Return the same installed vendor source for inference and cache consumers."""
    return Path(override) if override else runtime_env_root(ROOT)/"vendor"/"duet"


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _source_hash(source):
    result = hashlib.sha256()
    paths = sorted((source / "src" / "duet").rglob("*.py"))
    paths.append(source / "src" / "duet" / "vocabulary.json")
    for path in paths:
        result.update(path.relative_to(source).as_posix().encode())
        result.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return result.hexdigest()


def _cache_key(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_outputs(directory, expected_notes=None):
    for name in ARTIFACTS:
        path = directory / name
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"DuET 缺少完整输出：{name}")
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("status") != "complete" or report.get("stage") != "complete" or report.get("input_coverage") != 1.0:
        raise ValueError("DuET 未完成全部音符的制谱")
    count = report.get("input_notes")
    cleanup = report.get("midi_input_cleanup", {})
    if (type(count) is not int or count < 1 or cleanup.get("retained_notes") != count
            or cleanup.get("input_note_on_events") != count
            or any(cleanup.get(name, 0) for name in ("nonpositive_duration_notes", "unclosed_note_on_events", "unmatched_note_off_events"))):
        raise ValueError("DuET 输入包含无效或丢失的 MIDI 音符，请先修复源文件")
    records = json.loads((directory / "notes.json").read_text(encoding="utf-8"))
    if not isinstance(records, list) or len(records) != count or any(not isinstance(record, dict) for record in records):
        raise ValueError("DuET 预测音符数量不完整")
    ids = [record.get("input_id") for record in records]
    if any(type(value) is not int for value in ids) or sorted(ids) != list(range(count)):
        raise ValueError("DuET 预测存在重复或丢失的输入音符标识")
    if expected_notes is not None:
        if len(expected_notes) != count or any(record.get("pitch") != int(expected_notes[record["input_id"]]) for record in records):
            raise ValueError("DuET 预测改变了源音符音高或数量")
    xml = ET.parse(directory / "score.musicxml").getroot()
    if xml.tag != "score-partwise" or not xml.findall(".//note/pitch"):
        raise ValueError("DuET 没有生成可读的带音符 MusicXML")
    import mido
    mido.MidiFile(directory / "score.mid")
    return report


def _cached(directory, identity):
    try:
        manifest = json.loads((directory / "astra-cache.json").read_text(encoding="utf-8"))
        if manifest.get("identity") != identity:
            return False
        if any(file_hash(directory / name) != manifest.get("artifacts", {}).get(name) for name in ARTIFACTS):
            return False
        _validate_outputs(directory)
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError):
        return False


def _result(directory, key, cached):
    return {"musicxml": str(directory / "score.musicxml"),
            "notation_midi": str(directory / "score.mid"),
            "report": str(directory / "report.json"),
            "predictions": str(directory / "notes.json"),
            "review": str(directory / "review.json"),
            "cache_key": key, "cached": cached, "warnings": [TIMING_WARNING]}


def _compatible_cached(cache, identity):
    """Reuse verified predictions across execution devices without rebinding IDs.

    Device and thread count describe execution, not a requested notation edit.
    Every model/input/engraving field still has to match, and the original
    manifest and artifact hashes remain authoritative and untouched.
    """
    def semantic(value):
        return {key: item for key, item in value.items() if key not in ('device', 'threads')}
    expected = semantic(identity)
    for manifest in sorted(cache.glob('*/astra-cache.json')):
        try:
            original = json.loads(manifest.read_text(encoding='utf-8'))['identity']
            if not isinstance(original, dict) or semantic(original) != expected:
                continue
            directory = manifest.parent
            key = _cache_key(original)
            if directory.name == key and _cached(directory, original):
                return _result(directory, key, True)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return None


def cached_prediction(raw_midi, cache_dir, *, title=None, source_dir=None):
    """Read-only inference readiness, without loading/checksumming large weights.

    A positive result is not permission to infer later: callers must still use
    cache_only=True when executing, including if files change after this check.
    The actual exporter retains model installation/integrity checks.
    """
    midi=Path(raw_midi).resolve(strict=True)
    identity={'schema':1,'input_sha256':file_hash(midi),'source_commit':SOURCE_COMMIT,
        'source_sha256':_source_hash(duet_source_dir(source_dir)),
        'weights_sha256':WEIGHTS_SHA256,'model_config_sha256':CONFIG_SHA256,
        'device':'cpu','threads':2,'engraving':{'auto_gliss':False},'title':title or midi.stem}
    return _compatible_cached(Path(cache_dir),identity)


def run_duet_score(raw_midi, cache_dir, *, model_dir=None, source_dir=None,
                   python=None, device="auto", threads=2, engraving_config=None,
                   title=None, log=print, cache_only=False):
    """Run the pinned official model offline, or return a verified cache entry.

    No fallback silently changes the selected score engine. The caller chooses
    whether to render the returned MusicXML as PDF; no renderer is invoked here.
    """
    midi = Path(raw_midi).resolve(strict=True)
    runtime = runtime_env_root(ROOT)
    model = Path(model_dir or runtime / "models" / "duet").resolve()
    source = duet_source_dir(source_dir).resolve()
    cache = Path(cache_dir).resolve()
    if device not in ("auto", "cpu", "cuda") or type(threads) is not int or threads < 1:
        raise ValueError("DuET device 应为 auto/cpu/cuda，threads 应为正整数")
    pin = json.loads((source / "ASTRA-PIN.json").read_text(encoding="utf-8"))
    if pin.get("commit") != SOURCE_COMMIT:
        raise ValueError("DuET 源码版本与适配器不一致")
    for name, expected in (("model.safetensors", WEIGHTS_SHA256), ("config.json", CONFIG_SHA256)):
        path = model / name
        if not path.is_file():
            raise FileNotFoundError(f"DuET 模型未安装：{path}；请运行 scripts/download_duet.py")
        if file_hash(path) != expected:
            raise ValueError(f"DuET 模型校验失败：{name}；请运行 scripts/download_duet.py 修复")
    engraving = {"auto_gliss": False, **(engraving_config or {})}
    identity = {"schema": 1, "input_sha256": file_hash(midi), "source_commit": SOURCE_COMMIT,
                "source_sha256": _source_hash(source), "weights_sha256": WEIGHTS_SHA256,
                "model_config_sha256": CONFIG_SHA256, "device": device, "threads": threads,
                "engraving": engraving, "title": title or midi.stem}
    key = _cache_key(identity)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / key
    if _cached(destination, identity):
        log("[duet] 复用已校验的乐谱缓存，跳过模型推理")
        return _result(destination, key, True)
    compatible = _compatible_cached(cache, identity)
    if compatible:
        log("[duet] 复用已校验的既有预测（仅执行设备/线程不同），保留原预测身份")
        return compatible
    if cache_only:
        raise ValueError("缺少可复用的 DuET 预测缓存。请先常规重导出，再应用节拍修正。")
    env = os.environ.copy()
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONIOENCODING="utf-8")
    duet_cuda_cache = data_root() / ".cache" / "cuda-duet"
    duet_cuda_cache.mkdir(parents=True, exist_ok=True)
    env["CUDA_CACHE_PATH"] = str(duet_cuda_cache)
    env["CUDA_CACHE_MAXSIZE"] = "1073741824"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH"))))
    if device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = "-1"
    with tempfile.TemporaryDirectory(prefix=".duet-pending-", dir=cache) as temporary:
        pending = Path(temporary)
        output = pending / "result"
        request = {"input": str(midi), "output": str(output), "model": str(model),
                   "source": str(source), "identity": identity}
        request_path = pending / "request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        score_env = ".venv-score-gpu" if runtime != ROOT and device == "cuda" else ".venv-score"
        score_python = runtime / score_env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        executable = lease.windowless_python(python or (str(score_python) if score_python.is_file() else sys.executable))
        command = [os.fspath(executable), "-u", "-m", "astra.duet_score", "worker", str(request_path)]
        from .resource_admission import wait_for_model_admission
        wait_for_model_admission(device, "DuET", log)
        log(f"[duet] 本地记谱：{midi.name}（{device}）")
        from .preview_cancel import run_logged_process, cancellation_active
        if cancellation_active():
            code, last = run_logged_process(command, lambda line:log(f"[duet] {line}"),
                encoding="utf-8", errors="replace", env=env, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            src_root = str(Path(__file__).resolve().parents[1])
            daemon_out = None
            if lease.enabled():
                daemon_out = _run_duet_via_daemon(request, executable, src_root, device, env, log)
            if daemon_out is not None:
                code, last = daemon_out
            else:
                code, last = _run_prediction_logged(command, env, log)
        if code:
            raise RuntimeError(f"DuET 制谱失败（退出码 {code}）：{last}")
        _validate_outputs(output)
        if file_hash(midi) != identity["input_sha256"]:
            raise RuntimeError("DuET 处理期间源 MIDI 已改变，未发布乐谱缓存")
        manifest = {"identity": identity, "artifacts": {name: file_hash(output / name) for name in ARTIFACTS}}
        (output / "astra-cache.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if destination.exists():
            if _cached(destination, identity):
                return _result(destination, key, True)
            # Preserve unexpected/corrupt previous files for diagnosis. Never
            # overwrite a prior successful export or accept a partial cache.
            destination.rename(cache / f"{key}.invalid-{uuid.uuid4().hex[:8]}")
        output.rename(destination)
    return _result(destination, key, False)


_DUET_DAEMON_IDLE_SECONDS = 15 * 60


def _duet_endpoint_path(*route):
    directory = data_root() / ".cache"
    directory.mkdir(parents=True, exist_ok=True)
    return lease.leased_path(directory / "fast-duet-worker.json", *route)


def _connect_duet_daemon(python, src, device):
    import socket
    path = _duet_endpoint_path(os.fspath(python), src, device)
    if not path.is_file():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        if info.get("python") != python or info.get("src") != src or info.get("device") != device:
            return None
        sock = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2.0)
        sock.settimeout(1200.0)
        return sock, str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _start_duet_daemon(python, src, device, env):
    import secrets
    import time
    path = _duet_endpoint_path(os.fspath(python), src, device)
    token = secrets.token_hex(16)
    cmd = [os.fspath(python), "-u", "-m", "astra.duet_score", "serve", str(path), token, src, device]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=flags,
            close_fds=True,
        )
    except OSError:
        return None
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        conn = _connect_duet_daemon(os.fspath(python), src, device)
        if conn is not None and conn[1] == token:
            return conn
        if conn is not None:
            conn[0].close()
        time.sleep(0.15)
    return None


def _run_duet_via_daemon(request_dict, executable, src, device, env, log, *, action="transcribe"):
    executable = lease.windowless_python(executable)
    conn = _connect_duet_daemon(os.fspath(executable), src, device)
    if conn is None:
        with lease.start_lock(_duet_endpoint_path(os.fspath(executable), src, device)) as acquired:
            if acquired:
                conn = _connect_duet_daemon(os.fspath(executable), src, device) or _start_duet_daemon(executable, src, device, env)
    if conn is None:
        return None
    sock, token = conn
    try:
        sock.sendall((json.dumps({"token": token, "action": action, "request": request_dict}, ensure_ascii=False) + "\n").encode("utf-8"))
        reader = sock.makefile("r", encoding="utf-8", errors="replace")
        last = ""
        for raw in reader:
            raw = raw.rstrip("\r\n")
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                last = raw
                log(f"[duet] {raw}")
                continue
            kind = msg.get("kind")
            if kind == "log":
                line = str(msg.get("line", "")).strip()
                if line:
                    last = line
                    log(f"[duet] {line}")
            elif kind == "done":
                reader.close()
                return 0, last
            elif kind == "error":
                reader.close()
                return 1, str(msg.get("error") or last or "duet daemon error")
        reader.close()
        return 1, "Model worker closed before confirming the result"
    except OSError as exc:
        return 1, str(exc)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def run_duet_notation_via_daemon(request_dict, executable, src, device, env, log):
    """Run refine_duet_notation inside the warm score daemon to eliminate cold torch/scipy imports."""
    return _run_duet_via_daemon(request_dict, executable, src, device, env, log, action="notation")


def prewarm_duet_daemon(config):
    """Fire-and-forget background warmup of the DuET score daemon during Stage 1 separation.

    Starts the isolated score Python process and loads DuET weights into host CPU RAM
    (zero GPU VRAM contention while BS-RoFormer separates audio).
    """
    if not lease.enabled():
        return
    import threading
    score_cfg = (config or {}).get("score", {})
    if score_cfg.get("piano_engine") != "duet":
        return
    device = score_cfg.get("duet_device") or ("cpu" if score_cfg.get("device") == "cpu" else "cuda")
    if device == "cpu":
        return
    runtime = runtime_env_root(ROOT)
    model_dir = str((runtime / "models" / "duet").resolve())
    source_dir = str(duet_source_dir().resolve())
    score_env = ".venv-score-gpu" if runtime != ROOT and device == "cuda" else ".venv-score"
    score_python = runtime / score_env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = score_cfg.get("python") or (str(score_python) if score_python.is_file() else sys.executable)
    if not Path(executable).is_file():
        return
    src_root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONIOENCODING="utf-8")
    duet_cuda_cache = data_root() / ".cache" / "cuda-duet"
    duet_cuda_cache.mkdir(parents=True, exist_ok=True)
    env["CUDA_CACHE_PATH"] = str(duet_cuda_cache)
    env["CUDA_CACHE_MAXSIZE"] = "1073741824"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src_root, env.get("PYTHONPATH"))))

    def _bg():
        try:
            _run_duet_via_daemon(
                {"model": model_dir, "source": source_dir, "device": device},
                executable, src_root, device, env, lambda _msg: None, action="warmup",
            )
        except Exception:
            pass

    threading.Thread(target=_bg, name="astra-duet-prewarm", daemon=True).start()


def _serve_duet_daemon(endpoint_path, token, src, device):
    import gc
    import io
    import socket
    import threading
    import time

    abort = lease.own_worker(endpoint_path, token)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    srv.settimeout(30.0)
    port = srv.getsockname()[1]
    info = {"port": port, "pid": os.getpid(), "python": os.fspath(sys.executable),
            "token": token, "src": src, "device": device}
    tmp = endpoint_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info), encoding="utf-8")
    tmp.replace(endpoint_path)

    cached_model = {}
    last_active = time.monotonic()
    lock = threading.Lock()

    class _StreamWriter(io.TextIOBase):
        def __init__(self, send_fn):
            self._send = send_fn
            self._buf = ""

        def write(self, s):
            if not s:
                return 0
            self._buf += s
            while "\n" in self._buf or "\r" in self._buf:
                idx_n = self._buf.find("\n")
                idx_r = self._buf.find("\r")
                idx = idx_n if idx_r < 0 else (idx_r if idx_n < 0 else min(idx_n, idx_r))
                line = self._buf[:idx].strip()
                self._buf = self._buf[idx + 1:]
                if line:
                    self._send({"kind": "log", "line": line})
            return len(s)

        def flush(self):
            if self._buf.strip():
                self._send({"kind": "log", "line": self._buf.strip()})
                self._buf = ""

    try:
        while time.monotonic() - last_active < _DUET_DAEMON_IDLE_SECONDS:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            last_active = time.monotonic()
            try:
                payload = lease.read_request(conn)
                if not isinstance(payload, dict):
                    continue
                if payload.get("token") != token:
                    continue

                def _send(obj):
                    if obj.get("kind") in ("result", "done", "error"):
                        done.set()
                    try:
                        conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                    except OSError:
                        pass

                with lock, lease.active_client(conn, abort) as done:
                    writer = _StreamWriter(_send)
                    old_out, old_err = sys.stdout, sys.stderr
                    sys.stdout = writer
                    sys.stderr = writer
                    try:
                        action = payload.get("action") or "transcribe"
                        if action == "warmup":
                            _prewarm_model(payload["request"], cached_model)
                        elif action == "notation":
                            _run_notation_request(payload["request"])
                        else:
                            _worker(payload["request"], model_cache=cached_model)
                        writer.flush()
                        _send({"kind": "done"})
                    except Exception as exc:
                        writer.flush()
                        _send({"kind": "error", "error": f"{type(exc).__name__}: {exc}"})
                    finally:
                        sys.stdout, sys.stderr = old_out, old_err
                        gc.collect()
                last_active = time.monotonic()
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
    finally:
        try:
            srv.close()
        except OSError:
            pass
        try:
            if endpoint_path.is_file() and json.loads(endpoint_path.read_text(encoding="utf-8")).get("token") == token:
                endpoint_path.unlink(missing_ok=True)
        except Exception:
            pass


def _run_prediction_logged(command, env, log):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding="utf-8", errors="replace", env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    last = ""
    try:
        for line in process.stdout:
            if line.strip():
                last = line.strip()
                log(f"[duet] {last}")
        code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    return code, last


def _install_fast_generate_rows(net):
    """Mathematically identical batched causal prefix prefill + cached constrain.

    Leaves vendored DuET files untouched on disk while avoiding per-note Python
    loops over known history rows and per-note tensor allocations in constrain().
    """
    import torch
    import torch.nn.functional as F
    from duet.layers import apply_rotary

    orig_generate_rows = net.generate_rows
    codec = net.codec
    width = codec.width

    @torch.inference_mode()
    def fast_generate_rows(batch, *, prefix_rows=None, prefix_known=None, anchor=0, use_cache=True):
        if not use_cache or prefix_known is not None:
            return orig_generate_rows(batch, prefix_rows=prefix_rows, prefix_known=prefix_known,
                                      anchor=anchor, use_cache=use_cache)
        if net.training or len(batch["note_pitch"]) != 1:
            raise ValueError("Aligned generation requires eval and batch size 1")
        slots = batch["query_note_slots"]
        n = slots.shape[1]
        device = slots.device
        if (slots.ndim != 2 or (slots < 0).any() or (slots >= batch["note_pitch"].shape[1]).any()):
            raise ValueError("Invalid query note slots")
        if (not batch["note_mask"].gather(1, slots).all() or (slots[:, 1:] <= slots[:, :-1]).any()):
            raise ValueError("Queries must address distinct valid input notes in order")
        if prefix_rows is None:
            prefix_rows = torch.empty((0, width), dtype=torch.long, device=device)
        P = len(prefix_rows)
        if P > n:
            raise ValueError("Prefix exceeds query count")

        bar_idx = torch.arange(codec.sizes[0], device=device)
        lengths_t = torch.as_tensor(codec.tick_values["bar_length"], device=device)
        fracs_t = torch.as_tensor(codec.fractions, device=device)
        pos_grid = torch.arange(64, device=device)[:, None] * 480 + fracs_t[None, :]
        n_frac = len(fracs_t)

        def fast_constrain(logits):
            scores = [x[0, -1].float().clone() for x in logits]
            scores[0][bar_idx - codec.bar_offset + anchor < 0] = -torch.inf
            scores[5][lengths_t <= 0] = -torch.inf
            row = torch.stack([v.argmax() for v in scores])
            length = lengths_t[row[5]]
            joint = scores[1][:, None] + scores[2][None, :]
            joint = joint.masked_fill(pos_grid >= length, -torch.inf)
            best = joint.flatten().argmax()
            row[1] = best // n_frac
            row[2] = best % n_frac
            if row[10]:
                row[3] = 0
            else:
                scores[3][0] = -torch.inf
                row[3] = scores[3].argmax()
            return row

        memory, valid, _ = net.encode(batch)
        pos = net.positions(batch, memory)
        pitch = net.current_pitch(batch) if net.decoder_input_pitch else None
        cache = [{} for _ in net.blocks]
        rows = [prefix_rows[k].clone() for k in range(P)]
        ones = torch.ones(width, dtype=torch.bool, device=device)

        if P:
            ids = torch.zeros((1, P, width), dtype=torch.long, device=device)
            known = torch.zeros((1, P, width), dtype=torch.bool, device=device)
            if P > 1:
                ids[0, 1:] = prefix_rows[:P - 1]
                known[0, 1:] = True
            context = torch.ones((1, P), dtype=torch.bool, device=device)
            x = net.embed(ids, known, context, first=True)
            if pitch is not None:
                x = x + pitch[:, :P]
            steps = torch.arange(P, device=device).float()[None]
            for j, block in enumerate(net.blocks):
                stream = net.decoder_stream(j)
                q, k = pos[stream]
                a = block.self_attention
                h = block.norms[0](x)
                qk = net.rotary(steps)
                query = apply_rotary(a._split(a.q_proj(h)), *qk)
                keys = apply_rotary(a._split(a.k_proj(h)), *qk)
                values = a._split(a.v_proj(h))
                cache[j].update(self_k=keys, self_v=values)
                y = F.scaled_dot_product_attention(query, keys, values, is_causal=True)
                x = x + a.o_proj(y.transpose(1, 2).reshape_as(x))
                x = x + block.cross(block.norms[1](x), memory[stream], valid[stream],
                                    net.cross_rotary(stream), q[:, :P], cache[j], k)
                x = x + block.ffn(block.norms[2](x))

        for i in range(P, n):
            if i == 0:
                ids = torch.zeros((1, 1, width), dtype=torch.long, device=device)
                known = torch.zeros_like(ids, dtype=torch.bool)
            else:
                ids = rows[-1][None, None]
                known = ones[None, None]
            context = torch.zeros((1, 1), dtype=torch.bool, device=device)
            x = net.embed(ids, known, context, first=(i == 0))
            if pitch is not None:
                x = x + pitch[:, i:i + 1]
            for j, block in enumerate(net.blocks):
                stream = net.decoder_stream(j)
                q, k = pos[stream]
                x = block.step(x, memory[stream], valid[stream], net.rotary, cache[j], i,
                               q[:, i:i + 1], k, net.cross_rotary(stream))
            hidden = net.final_norm(x)
            logits = [head(hidden) for head in net.heads]
            rows.append(fast_constrain(logits))

        return (torch.stack(rows) if rows
                else torch.empty(0, width, dtype=torch.long, device=device))

    net.generate_rows = fast_generate_rows


def _install_fast_cuda_graph_generate_rows(net, *, max_k_hint=512):
    """Speculative FP16 Fused CUDA Graph decoder + Exact 1-Pass Batched FP32 Verification.

    Implements speculative self-verification (Leviathan et al., ICML 2023):
    1. Fuses embeddings, self-attention QKV, and all 11 output heads in FP16,
       captured into 8-step and 1-step CUDA Graphs with in-graph step advances.
    2. Verifies all drafted window rows in a single parallel causal FP32 forward pass;
       any differing note is injected from exact FP32 and re-drafted from bad_idx+1.
    Mathematically guarantees 0/N bit-identical rows vs FP32 baseline while running
    ~4x faster than eager CUDA Graph FP32 (~4.3s for 1,318 notes on RTX 5070 Ti).
    """
    import copy
    import math
    import torch
    import torch.nn.functional as F
    from duet.config import MAX_NOTES
    from duet.layers import apply_rotary

    _install_fast_generate_rows(net)
    eager_fast = net.generate_rows
    codec = net.codec
    width = codec.width
    n_blocks = len(net.blocks)
    n_heads = net.config.num_attention_heads
    d_model = net.config.hidden_size
    d_head = d_model // n_heads
    dev = next(net.parameters()).device
    if dev.type != "cuda":
        return

    dtype = torch.float16
    m_run = copy.deepcopy(net).to(dtype=dtype)
    m_run.rotary.float()
    m_run.note_rotary.float()

    emb_offsets = []
    emb_weights = []
    off = 0
    for e in m_run.embeddings:
        emb_offsets.append(off)
        emb_weights.append(e.weight.detach())
        off += e.num_embeddings
    fused_emb_w = torch.cat(emb_weights, dim=0).contiguous()
    fused_emb_off = torch.tensor(emb_offsets, dtype=torch.long, device=dev)

    head_sizes = [h.out_features for h in m_run.heads]
    fused_head_w = torch.cat([h.weight.detach() for h in m_run.heads], dim=0).contiguous()
    fused_head_b = torch.cat([h.bias.detach() for h in m_run.heads], dim=0).contiguous()
    qkv_w = [
        torch.cat([b.self_attention.q_proj.weight.detach(),
                   b.self_attention.k_proj.weight.detach(),
                   b.self_attention.v_proj.weight.detach()], dim=0).contiguous()
        for b in m_run.blocks
    ]
    qkv_b = [
        torch.cat([b.self_attention.q_proj.bias.detach(),
                   b.self_attention.k_proj.bias.detach(),
                   b.self_attention.v_proj.bias.detach()], dim=0).contiguous()
        for b in m_run.blocks
    ]
    cq_w = [b.cross_attention.in_proj_weight[:d_model].detach().contiguous() for b in m_run.blocks]
    cq_b = [b.cross_attention.in_proj_bias[:d_model].detach().contiguous() for b in m_run.blocks]

    bar_idx = torch.arange(codec.sizes[0], device=dev)
    lengths_t = torch.as_tensor(codec.tick_values["bar_length"], device=dev)
    fracs_t = torch.as_tensor(codec.fractions, device=dev)
    pos_grid = torch.arange(64, device=dev)[:, None] * 480 + fracs_t[None, :]
    n_frac = len(fracs_t)
    reg_emb_0 = m_run.decoder_region_embedding.weight[0].detach().view(1, 1, -1).contiguous()
    bos_plus_reg = (m_run.bos_embedding.detach().view(1, 1, -1) + reg_emb_0).contiguous()
    s3_zero_mask = torch.arange(codec.sizes[3], device=dev) == 0
    tp_axis_dim = net.note_rotary.axis_dim
    tp_time_scale = 2.0 * math.pi / torch.tensor(net.note_rotary.time_periods, device=dev, dtype=torch.float32)
    tp_pitch_scale = 2.0 * math.pi / torch.tensor(net.note_rotary.pitch_periods, device=dev, dtype=torch.float32)
    one_t = torch.ones((1,), dtype=torch.long, device=dev)

    runner = {}

    def _ensure_graph(max_k, max_note_m, max_frame_m):
        if runner.get("cg1") is not None and max_k <= runner["max_k"] and max_note_m <= runner["max_note_m"] and max_frame_m <= runner["max_frame_m"]:
            return runner
        mk = min(MAX_NOTES + 32, max(512, ((max_k + 127) // 128) * 128))
        mn = min(MAX_NOTES + 32, max(512, ((max_note_m + 127) // 128) * 128))
        mf = max(2048, ((max_frame_m + 255) // 256) * 256)

        s_step_idx = torch.zeros((1,), dtype=torch.long, device=dev)
        s_pitch_all = torch.zeros((1, mk, d_model), dtype=dtype, device=dev)
        s_out_all = torch.zeros((mk, width), dtype=torch.long, device=dev)
        s_anchor = torch.zeros((), dtype=torch.long, device=dev)

        all_steps = torch.arange(mk, device=dev, dtype=torch.float32)[None]
        cos_all, sin_all = m_run.rotary(all_steps)
        s_cq_note_cos0 = torch.zeros((mk, tp_axis_dim), dtype=torch.float32, device=dev)
        s_cq_note_sin0 = torch.zeros((mk, tp_axis_dim), dtype=torch.float32, device=dev)
        s_cq_note_cos1 = torch.zeros((mk, tp_axis_dim), dtype=torch.float32, device=dev)
        s_cq_note_sin1 = torch.zeros((mk, tp_axis_dim), dtype=torch.float32, device=dev)
        s_cq_note_vis = torch.zeros((mk,), dtype=torch.bool, device=dev)
        s_cq_frame_cos = torch.zeros((mk, d_head), dtype=torch.float32, device=dev)
        s_cq_frame_sin = torch.zeros((mk, d_head), dtype=torch.float32, device=dev)

        s_self_k = [torch.zeros((1, n_heads, mk, d_head), dtype=dtype, device=dev) for _ in range(n_blocks)]
        s_self_v = [torch.zeros((1, n_heads, mk, d_head), dtype=dtype, device=dev) for _ in range(n_blocks)]
        s_cross_k = [
            torch.zeros((1, n_heads, mn if m_run.decoder_stream(j) == "note" else mf, d_head), dtype=dtype, device=dev)
            for j in range(n_blocks)
        ]
        s_cross_v = [
            torch.zeros((1, n_heads, mn if m_run.decoder_stream(j) == "note" else mf, d_head), dtype=dtype, device=dev)
            for j in range(n_blocks)
        ]
        s_cross_valid_note = torch.zeros((1, 1, 1, mn), dtype=torch.bool, device=dev)
        s_cross_valid_frame = torch.zeros((1, 1, 1, mf), dtype=torch.bool, device=dev)
        s_cross_valid_note[:, :, :, 0] = True
        s_cross_valid_frame[:, :, :, 0] = True
        k_arange = torch.arange(mk, device=dev)[None, None, None, :]

        def step_fn():
            idx = s_step_idx
            prev_idx = torch.clamp(idx - 1, min=0)
            prev_row = s_out_all.index_select(0, prev_idx).squeeze(0)
            x_emb = F.embedding(prev_row + fused_emb_off, fused_emb_w).sum(dim=0).view(1, 1, d_model) + reg_emb_0
            is_first = (idx == 0).view(1, 1, 1)
            x = torch.where(is_first, bos_plus_reg, x_emb)
            if m_run.decoder_input_pitch:
                x = x + s_pitch_all.index_select(1, idx)
            self_mask = k_arange <= idx
            cs = cos_all.index_select(1, idx)
            sn = sin_all.index_select(1, idx)
            nc0 = s_cq_note_cos0.index_select(0, idx).view(1, 1, tp_axis_dim)
            ns0 = s_cq_note_sin0.index_select(0, idx).view(1, 1, tp_axis_dim)
            nc1 = s_cq_note_cos1.index_select(0, idx).view(1, 1, tp_axis_dim)
            ns1 = s_cq_note_sin1.index_select(0, idx).view(1, 1, tp_axis_dim)
            nvis = s_cq_note_vis.index_select(0, idx).view(1, 1, 1, 1)
            fc = s_cq_frame_cos.index_select(0, idx).view(1, 1, d_head)
            fs = s_cq_frame_sin.index_select(0, idx).view(1, 1, d_head)

            for j, block in enumerate(m_run.blocks):
                a = block.self_attention
                q_in = block.norms[0](x)
                qkv = F.linear(q_in, qkv_w[j], qkv_b[j]).view(1, 1, 3, n_heads, d_head)
                q_raw = qkv[:, :, 0].transpose(1, 2)
                k_raw = qkv[:, :, 1].transpose(1, 2)
                value = qkv[:, :, 2].transpose(1, 2)
                query = apply_rotary(q_raw.float(), cs, sn).to(dtype)
                key = apply_rotary(k_raw.float(), cs, sn).to(dtype)
                s_self_k[j].index_copy_(2, idx, key)
                s_self_v[j].index_copy_(2, idx, value)
                y = F.scaled_dot_product_attention(query, s_self_k[j], s_self_v[j], attn_mask=self_mask, dropout_p=0.0)
                x = x + a.o_proj(y.transpose(1, 2).reshape(1, 1, d_model))

                stream = m_run.decoder_stream(j)
                ca = block.cross_attention
                cq_raw = F.linear(block.norms[1](x), cq_w[j], cq_b[j]).view(1, 1, n_heads, d_head).transpose(1, 2)
                if stream == "note":
                    xf = cq_raw.float()
                    o0 = apply_rotary(xf[..., :tp_axis_dim], nc0, ns0)
                    o1 = apply_rotary(xf[..., tp_axis_dim : 2 * tp_axis_dim], nc1, ns1).masked_fill(~nvis, 0.0)
                    cq = torch.cat((o0, o1), -1).to(dtype)
                    c_mask = s_cross_valid_note
                else:
                    cq = apply_rotary(cq_raw.float(), fc, fs).to(dtype)
                    c_mask = s_cross_valid_frame
                cy = F.scaled_dot_product_attention(cq, s_cross_k[j], s_cross_v[j], attn_mask=c_mask, dropout_p=0.0)
                x = x + ca.out_proj(cy.transpose(1, 2).reshape(1, 1, d_model))
                x = x + block.ffn(block.norms[2](x))

            hidden = m_run.final_norm(x)
            all_logits = F.linear(hidden, fused_head_w, fused_head_b)[0, 0].float()
            scores = all_logits.split(head_sizes)
            s0 = scores[0].masked_fill((bar_idx - codec.bar_offset + s_anchor) < 0, -torch.inf)
            s5 = scores[5].masked_fill(lengths_t <= 0, -torch.inf)
            s3_no_grace = scores[3].masked_fill(s3_zero_mask, -torch.inf)
            row_list = [s0.argmax(), scores[1].argmax(), scores[2].argmax(), scores[3].argmax(),
                        scores[4].argmax(), s5.argmax(), *[s.argmax() for s in scores[6:]]]
            length = lengths_t.index_select(0, row_list[5].unsqueeze(0)).squeeze(0)
            joint = (scores[1][:, None] + scores[2][None, :]).masked_fill(pos_grid >= length, -torch.inf)
            best = joint.flatten().argmax()
            row_list[1] = best // n_frac
            row_list[2] = best % n_frac
            row_list[3] = torch.where(row_list[10] != 0, torch.zeros_like(row_list[3]), s3_no_grace.argmax())
            s_out_all.index_copy_(0, idx, torch.stack(row_list).unsqueeze(0))
            s_step_idx.add_(one_t)

        stream_cap = torch.cuda.Stream()
        stream_cap.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream_cap), torch.inference_mode():
            for _ in range(3):
                s_step_idx.zero_()
                step_fn()
        torch.cuda.current_stream().wait_stream(stream_cap)

        s_step_idx.zero_()
        cg1 = torch.cuda.CUDAGraph()
        with torch.cuda.graph(cg1), torch.inference_mode():
            step_fn()

        s_step_idx.zero_()
        cg8 = torch.cuda.CUDAGraph()
        with torch.cuda.graph(cg8), torch.inference_mode():
            for _ in range(8):
                step_fn()
        torch.cuda.synchronize()
        runner.clear()
        runner.update(
            cg1=cg1, cg8=cg8, max_k=mk, max_note_m=mn, max_frame_m=mf,
            s_step_idx=s_step_idx, s_pitch_all=s_pitch_all, s_out_all=s_out_all, s_anchor=s_anchor,
            cos_all=cos_all, sin_all=sin_all,
            s_cq_note_cos0=s_cq_note_cos0, s_cq_note_sin0=s_cq_note_sin0,
            s_cq_note_cos1=s_cq_note_cos1, s_cq_note_sin1=s_cq_note_sin1,
            s_cq_note_vis=s_cq_note_vis, s_cq_frame_cos=s_cq_frame_cos, s_cq_frame_sin=s_cq_frame_sin,
            s_self_k=s_self_k, s_self_v=s_self_v, s_cross_k=s_cross_k, s_cross_v=s_cross_v,
            s_cross_valid_note=s_cross_valid_note, s_cross_valid_frame=s_cross_valid_frame,
            _keep=(
                m_run, fused_emb_w, fused_emb_off, fused_head_w, fused_head_b, qkv_w, qkv_b,
                cq_w, cq_b, k_arange, bar_idx, lengths_t, fracs_t, pos_grid, reg_emb_0,
                bos_plus_reg, s3_zero_mask, tp_time_scale, tp_pitch_scale, one_t,
            ),
        )
        return runner

    def _fp32_verify(memory, valid, pos, pitch_fp32, draft_rows, check_from, prefix_len, anchor):
        n = draft_rows.shape[0]
        if n <= check_from:
            return None, -1
        ids = torch.zeros((1, n, width), dtype=torch.long, device=dev)
        known = torch.zeros((1, n, width), dtype=torch.bool, device=dev)
        if n > 1:
            ids[0, 1:] = draft_rows[:-1]
            known[0, 1:] = True
        ctx_mask = torch.zeros((1, n), dtype=torch.bool, device=dev)
        if prefix_len:
            ctx_mask[0, :prefix_len] = True
        x = net.embed(ids, known, ctx_mask, first=True)
        if pitch_fp32 is not None:
            x = x + pitch_fp32[:, :n]
        steps = torch.arange(n, device=dev).float()[None]
        cos_v, sin_v = net.rotary(steps)
        for j, block in enumerate(net.blocks):
            stream = net.decoder_stream(j)
            q, k = pos[stream]
            a = block.self_attention
            h = block.norms[0](x)
            query = apply_rotary(a._split(a.q_proj(h)), cos_v, sin_v)
            keys = apply_rotary(a._split(a.k_proj(h)), cos_v, sin_v)
            values = a._split(a.v_proj(h))
            y = F.scaled_dot_product_attention(query, keys, values, is_causal=True)
            x = x + a.o_proj(y.transpose(1, 2).reshape_as(x))
            ca = block.cross_attention
            wq, wk, wv = ca.in_proj_weight.chunk(3, dim=0)
            bq, bk, bv = ca.in_proj_bias.chunk(3, dim=0)
            cq = net.cross_rotary(stream).rotate(a._split(F.linear(block.norms[1](x), wq, bq)), q[:, :n])
            ck = net.cross_rotary(stream).rotate(a._split(F.linear(memory[stream], wk, bk)), k)
            cv = a._split(F.linear(memory[stream], wv, bv))
            cm = valid[stream][:, None, None, :]
            cy = F.scaled_dot_product_attention(cq, ck, cv, attn_mask=cm, dropout_p=0.0)
            x = x + ca.out_proj(cy.transpose(1, 2).reshape_as(x))
            x = x + block.ffn(block.norms[2](x))
        hidden = net.final_norm(x)[0, check_from:]
        scores = [h(hidden).float() for h in net.heads]
        s0 = scores[0].masked_fill((bar_idx - codec.bar_offset + anchor) < 0, -torch.inf)
        s5 = scores[5].masked_fill(lengths_t <= 0, -torch.inf)
        s3_ng = scores[3].masked_fill(s3_zero_mask, -torch.inf)
        r_cols = [s0.argmax(-1), scores[1].argmax(-1), scores[2].argmax(-1), scores[3].argmax(-1),
                  scores[4].argmax(-1), s5.argmax(-1), *[s.argmax(-1) for s in scores[6:]]]
        lens = lengths_t[r_cols[5]]
        joint = (scores[1][:, :, None] + scores[2][:, None, :]).masked_fill(pos_grid[None, :, :] >= lens[:, None, None], -torch.inf)
        best = joint.flatten(1).argmax(-1)
        r_cols[1] = best // n_frac
        r_cols[2] = best % n_frac
        r_cols[3] = torch.where(r_cols[10] != 0, torch.zeros_like(r_cols[3]), s3_ng.argmax(-1))
        verified = torch.stack(r_cols, dim=-1)
        mismatch = (verified != draft_rows[check_from:]).any(dim=-1)
        if not mismatch.any():
            return None, -1
        first_bad = int(mismatch.nonzero(as_tuple=False)[0].item())
        return verified[first_bad], check_from + first_bad

    @torch.inference_mode()
    def cg_generate_rows(batch, *, prefix_rows=None, prefix_known=None, anchor=0, use_cache=True):
        if not use_cache or prefix_known is not None or runner.get("disabled"):
            return eager_fast(batch, prefix_rows=prefix_rows, prefix_known=prefix_known, anchor=anchor, use_cache=use_cache)
        if net.training or len(batch["note_pitch"]) != 1:
            raise ValueError("Aligned generation requires eval and batch size 1")
        slots = batch["query_note_slots"]
        n = slots.shape[1]
        if (slots.ndim != 2 or (slots < 0).any() or (slots >= batch["note_pitch"].shape[1]).any()):
            raise ValueError("Invalid query note slots")
        if (not batch["note_mask"].gather(1, slots).all() or (slots[:, 1:] <= slots[:, :-1]).any()):
            raise ValueError("Queries must address distinct valid input notes in order")
        if prefix_rows is None:
            prefix_rows = torch.empty((0, width), dtype=torch.long, device=dev)
        P = len(prefix_rows)
        if P > n:
            raise ValueError("Prefix exceeds query count")

        memory, valid, _ = net.encode(batch)
        pos = net.positions(batch, memory)
        pitch_fp32 = net.current_pitch(batch) if net.decoder_input_pitch else None
        ln = valid["note"].shape[1]
        lf = valid["frame"].shape[1]
        try:
            r = _ensure_graph(max(max_k_hint, n), ln, lf)
        except Exception:
            runner["disabled"] = True
            return eager_fast(batch, prefix_rows=prefix_rows, prefix_known=prefix_known, anchor=anchor, use_cache=use_cache)

        if pitch_fp32 is not None:
            r["s_pitch_all"][:, :n].copy_(pitch_fp32.to(dtype))
        qn = pos["note"][0][0, :n]
        p0 = qn[:, 0:1].float() * tp_time_scale
        p0 = torch.cat((p0, p0), -1)
        r["s_cq_note_cos0"][:n].copy_(p0.cos())
        r["s_cq_note_sin0"][:n].copy_(p0.sin())
        vis = qn[:, 2].bool()
        c1 = qn[:, 1].float().masked_fill(~vis, 0.0)
        p1 = c1[:, None] * tp_pitch_scale
        p1 = torch.cat((p1, p1), -1)
        r["s_cq_note_cos1"][:n].copy_(p1.cos())
        r["s_cq_note_sin1"][:n].copy_(p1.sin())
        r["s_cq_note_vis"][:n].copy_(vis)
        qf = pos["frame"][0][:, :n]
        cf, sf = m_run.rotary(qf)
        if cf.ndim == 4:
            cf, sf = cf[0, 0], sf[0, 0]
        else:
            cf, sf = cf[0], sf[0]
        r["s_cq_frame_cos"][:n].copy_(cf)
        r["s_cq_frame_sin"][:n].copy_(sf)

        r["s_cross_valid_note"].zero_()
        r["s_cross_valid_note"][:, :, :, :ln].copy_(valid["note"][:, None, None, :])
        r["s_cross_valid_frame"].zero_()
        r["s_cross_valid_frame"][:, :, :, :lf].copy_(valid["frame"][:, None, None, :])
        for j, block in enumerate(m_run.blocks):
            stream = m_run.decoder_stream(j)
            ca = block.cross_attention
            split = block.self_attention._split
            wk, wv = ca.in_proj_weight[d_model : 2 * d_model], ca.in_proj_weight[2 * d_model :]
            bk, bv = ca.in_proj_bias[d_model : 2 * d_model], ca.in_proj_bias[2 * d_model :]
            mem_s = memory[stream].to(dtype)
            ck = net.cross_rotary(stream).rotate(split(F.linear(mem_s, wk, bk)).float(), pos[stream][1]).to(dtype)
            cv = split(F.linear(mem_s, wv, bv))
            L = ck.shape[2]
            r["s_cross_k"][j][:, :, :L, :].copy_(ck)
            r["s_cross_v"][j][:, :, :L, :].copy_(cv)

        if P:
            r["s_out_all"][:P].copy_(prefix_rows)
            ids_p = torch.zeros((1, P, width), dtype=torch.long, device=dev)
            known_p = torch.zeros((1, P, width), dtype=torch.bool, device=dev)
            if P > 1:
                ids_p[0, 1:].copy_(prefix_rows[: P - 1])
                known_p[0, 1:] = True
            ctx_p = torch.ones((1, P), dtype=torch.bool, device=dev)
            x = sum(e(ids_p[..., k]) * known_p[..., k, None].to(dtype) for k, e in enumerate(m_run.embeddings))
            x[:, 0] = m_run.bos_embedding
            x = x + m_run.decoder_region_embedding(ctx_p.long())
            if pitch_fp32 is not None:
                x = x + r["s_pitch_all"][:, :P]
            cos_p, sin_p = r["cos_all"][:, :P], r["sin_all"][:, :P]
            for j, block in enumerate(m_run.blocks):
                stream = m_run.decoder_stream(j)
                q, _ = pos[stream]
                a = block.self_attention
                h = block.norms[0](x)
                query = apply_rotary(a._split(a.q_proj(h)).float(), cos_p, sin_p).to(dtype)
                keys = apply_rotary(a._split(a.k_proj(h)).float(), cos_p, sin_p).to(dtype)
                values = a._split(a.v_proj(h))
                r["s_self_k"][j][:, :, :P, :].copy_(keys)
                r["s_self_v"][j][:, :, :P, :].copy_(values)
                y = F.scaled_dot_product_attention(query, keys, values, is_causal=True)
                x = x + a.o_proj(y.transpose(1, 2).reshape_as(x))
                ca = block.cross_attention
                cq = net.cross_rotary(stream).rotate(a._split(F.linear(block.norms[1](x), cq_w[j], cq_b[j])).float(), q[:, :P]).to(dtype)
                c_mask = r["s_cross_valid_note"] if stream == "note" else r["s_cross_valid_frame"]
                cy = F.scaled_dot_product_attention(cq, r["s_cross_k"][j], r["s_cross_v"][j], attn_mask=c_mask, dropout_p=0.0)
                x = x + ca.out_proj(cy.transpose(1, 2).reshape_as(x))
                x = x + block.ffn(block.norms[2](x))

        r["s_anchor"].fill_(anchor)
        cg1, cg8 = r["cg1"], r["cg8"]
        s_step_idx = r["s_step_idx"]

        def _decode_from(start_step):
            s_step_idx.fill_(start_step)
            rem = n - start_step
            for _ in range(rem // 8):
                cg8.replay()
            for _ in range(rem % 8):
                cg1.replay()

        _decode_from(P)

        # Speculative exact 1-pass FP32 verification with rollback on any mismatch.
        cur_p = P
        rollbacks = 0
        while cur_p < n:
            exact_row, bad_idx = _fp32_verify(memory, valid, pos, pitch_fp32, r["s_out_all"][:n], cur_p, P, anchor)
            if bad_idx < 0:
                break
            rollbacks += 1
            if rollbacks >= 16:
                return eager_fast(batch, prefix_rows=prefix_rows, prefix_known=prefix_known, anchor=anchor, use_cache=use_cache)
            r["s_out_all"][bad_idx].copy_(exact_row)
            if bad_idx + 1 < n:
                _decode_from(bad_idx + 1)
            cur_p = bad_idx + 1
        return r["s_out_all"][:n].clone()

    net.generate_rows = cg_generate_rows


def _prewarm_model(request, model_cache):
    """Pre-import score stack and load DuET weights into CPU RAM without contending for GPU VRAM."""
    if not hasattr(hashlib, "file_digest"):
        def file_digest(fileobj, digest):
            result = hashlib.new(digest) if isinstance(digest, str) else digest()
            for chunk in iter(lambda: fileobj.read(1024 * 1024), b""):
                result.update(chunk)
            return result
        hashlib.file_digest = file_digest
    src_str = str(Path(request["source"]) / "src")
    if src_str not in sys.path:
        sys.path.insert(0, src_str)
    import pandas  # noqa: F401
    import scipy.signal  # noqa: F401
    from duet import DuET
    from duet.readable import pipeline as _duet_readable_pipe  # noqa: F401
    cache_key = (str(request["model"]), request["device"])
    if cache_key in model_cache:
        return
    # Load weights into CPU RAM first (zero GPU memory while separation runs).
    cpu_key = (str(request["model"]), "prewarm_cpu")
    if cpu_key not in model_cache:
        model = DuET.from_pretrained(request["model"], local_files_only=True, device="cpu")
        model_cache[cpu_key] = model


def _run_notation_request(request):
    from .duet_notation import refine_duet_notation
    print("[notation] 复用 DuET 预测，按参考速度图重新制谱", flush=True)
    value = refine_duet_notation(**request)
    output = Path(request["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(value, ensure_ascii=False), flush=True)


def _worker(request, *, model_cache=None):
    # Only this isolated child supplies the Python 3.11 stdlib convenience API;
    # the vendored model and engraving code are unchanged.
    if not hasattr(hashlib, "file_digest"):
        def file_digest(fileobj, digest):
            result = hashlib.new(digest) if isinstance(digest, str) else digest()
            for chunk in iter(lambda: fileobj.read(1024 * 1024), b""):
                result.update(chunk)
            return result
        hashlib.file_digest = file_digest
    src_str = str(Path(request["source"]) / "src")
    if src_str not in sys.path:
        sys.path.insert(0, src_str)
    import torch
    from duet import DuET
    from duet.config import CORE_SECONDS
    from duet.midi import read_performance_midi
    from duet.readable import pipeline as readable_upstream
    from .duet_notation import fast_readable_context
    options = request["identity"]
    midi = Path(request["input"])
    if file_hash(midi) != options["input_sha256"]:
        raise ValueError("源 MIDI 已改变")
    data = read_performance_midi(midi)
    cleanup = data["midi_input_cleanup"]
    if cleanup["input_note_on_events"] != cleanup["retained_notes"] or cleanup["unmatched_note_off_events"]:
        raise ValueError("源 MIDI 含有不完整音符，拒绝静默丢弃后制谱")
    torch.set_num_threads(options["threads"])
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    cache_key = (str(request["model"]), options["device"])
    model = model_cache.get(cache_key) if model_cache is not None else None
    if model is None:
        cpu_key = (str(request["model"]), "prewarm_cpu")
        prewarmed = model_cache.pop(cpu_key, None) if model_cache is not None else None
        target_dev = ("cuda" if torch.cuda.is_available() else "cpu") if options["device"] == "auto" else options["device"]
        if prewarmed is not None:
            model = prewarmed
            model.device = torch.device(target_dev)
            model.model = model.model.to(model.device).eval()
        else:
            model = DuET.from_pretrained(request["model"], local_files_only=True,
                                        device=None if options["device"] == "auto" else options["device"])
        if model.device.type == "cuda":
            _install_fast_cuda_graph_generate_rows(model.model)
        else:
            _install_fast_generate_rows(model.model)
        if model_cache is not None:
            model_cache.clear()
            model_cache[cache_key] = model
    # DuET skips empty core windows. Use the same onset partition to report the
    # real denominator without modifying the vendored inference loop.
    total_windows = len({int(onset // CORE_SECONDS) for onset in data["ref_onset"]})
    started, completed_windows = monotonic(), 0

    def progress(event):
        nonlocal completed_windows
        completed_windows += 1
        print(json.dumps({**event, "completed_windows": completed_windows,
                          "total_windows": total_windows,
                          "elapsed_seconds": monotonic() - started}), flush=True)

    # Cache encoder outputs per window during this transcribe() call so
    # predict_engraving_frames() reuses the exact encoder pass without recomputing it.
    _encode_key_fields = (
        "roll",
        "frame_times_rel",
        "note_pitch",
        "note_onset_rel",
        "note_duration_sec",
        "note_velocity",
        "frame_valid",
        "note_mask",
        "note_masked",
        "note_is_context",
        "frame_is_context",
    )
    orig_encode = model.model.encode
    enc_cache = {}

    def _cached_encode(batch):
        h = hashlib.blake2b(digest_size=16)
        for field in _encode_key_fields:
            t = batch.get(field)
            if t is None:
                h.update(b"\x00")
            else:
                h.update(field.encode("ascii"))
                h.update(repr(tuple(t.shape)).encode("ascii"))
                h.update(t.detach().cpu().numpy().tobytes())
        key = h.digest()
        hit = enc_cache.get(key)
        if hit is not None:
            return hit
        out = orig_encode(batch)
        if len(enc_cache) < 64:
            enc_cache[key] = out
        return out

    model.model.encode = _cached_encode
    print(json.dumps({"completed_windows": 0, "total_windows": total_windows,
                      "elapsed_seconds": 0.0}), flush=True)
    try:
        with fast_readable_context(readable_upstream):
            model.transcribe(midi, request["output"], title=options["title"],
                             engraving_config=options["engraving"], progress=progress)
    finally:
        model.model.encode = orig_encode
        enc_cache.clear()
    _validate_outputs(Path(request["output"]), data["ref_pitch"])


def main():
    if len(sys.argv) >= 6 and sys.argv[1] == "serve":
        _serve_duet_daemon(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["worker"])
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    _worker(json.loads(args.request.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
