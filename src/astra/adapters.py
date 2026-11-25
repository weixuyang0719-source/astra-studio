"""Run each local inference backend in its own short-lived Python process."""

from __future__ import annotations
from .release_paths import data_root
from . import model_worker_lease as lease

import argparse
from contextlib import contextmanager
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable


STEMS = ("vocals", "drums", "bass", "guitar", "piano", "other")
SEPARATOR_MODEL = "BS-Roformer-SW.ckpt"
SEPARATOR_ASSETS = (SEPARATOR_MODEL, "BS-Roformer-SW.yaml", "download_checks.json")
XLANCE_PIANO_MODEL = "bs_keys_xlancer.ckpt"
XLANCE_PIANO_CONFIG = "bs_keys_xlancer_config.yaml"
Log = Callable[[str], None]


class ModelError(RuntimeError):
    """A local inference worker failed."""


def separate(input_path: Path, output_dir: Path, config: dict, log: Log) -> dict[str, str]:
    """Return absolute paths for the requested WAV stems."""
    return _run_worker("separator", input_path, output_dir, config, log)


def transcribe_transkun(input_path: Path, output_dir: Path, config: dict, log: Log) -> list[dict]:
    """Return piano notes in seconds and MIDI units."""
    return _run_worker("transkun", input_path, output_dir, config, log)


def transcribe_basic_pitch(input_path: Path, output_dir: Path, config: dict, log: Log) -> list[dict]:
    """Return pitched note events in seconds and MIDI units."""
    return _run_worker("basic_pitch", input_path, output_dir, config, log)


def detect_beats(input_path: Path, output_dir: Path, config: dict, log: Log) -> dict[str, list[float]]:
    """Run exactly the explicitly selected pulse provider, without fallback."""
    from .rhythm_provider import selected_provider
    provider = selected_provider(config)
    return _run_worker(provider, input_path, output_dir, config, log)


_PERSISTENT_ADAPTERS = ("separator", "transkun")
_PERSISTENT_IDLE_SECONDS = 15 * 60
_IN_PERSISTENT_DAEMON = False
_PERSISTENT_MODEL_CACHE: dict = {}
_TRITON_READY_CACHE: bool | None = None
_TRITON_VITERBI_FN = None


def _configure_isolated_triton() -> bool:
    """Configure candidate-local triton-windows & persistent Inductor cache without touching shared .venv."""
    global _TRITON_READY_CACHE
    if _TRITON_READY_CACHE is not None:
        return _TRITON_READY_CACHE
    try:
        cand_root = Path(__file__).resolve().parents[3]
        triton_pkg = cand_root / "deps-fast" / "triton_pkg"
        if triton_pkg.is_dir():
            pkg_str = str(triton_pkg)
            if pkg_str not in sys.path:
                sys.path.insert(0, pkg_str)
            cuda_env = os.environ.get("CUDA_PATH") or os.environ.get("CUDA_HOME")
            valid_cuda = bool(
                cuda_env
                and (Path(cuda_env) / "bin" / "ptxas.exe").is_file()
                and (Path(cuda_env) / "include" / "cuda.h").is_file()
            )
            if not valid_cuda:
                bundled = triton_pkg / "triton" / "backends" / "nvidia"
                if (bundled / "bin" / "ptxas.exe").is_file() and (bundled / "include" / "cuda.h").is_file():
                    os.environ["CUDA_PATH"] = str(bundled)
        if os.name == "nt" and not shutil.which("cl.exe"):
            candidates = []
            for base in (Path(r"C:\Program Files\Microsoft Visual Studio"),
                         Path(r"C:\Program Files (x86)\Microsoft Visual Studio")):
                if base.is_dir():
                    candidates.extend(base.glob("*/*/VC/Tools/MSVC/*/bin/Hostx64/x64/cl.exe"))
            if candidates:
                cl_dir = str(sorted(candidates)[-1].parent)
                os.environ["PATH"] = cl_dir + os.pathsep + os.environ.get("PATH", "")
        cache_base = data_root() / ".cache"
        ind_dir = cache_base / "inductor"
        tri_dir = cache_base / "triton"
        ind_dir.mkdir(parents=True, exist_ok=True)
        tri_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", str(ind_dir))
        os.environ.setdefault("TRITON_CACHE_DIR", str(tri_dir))
        os.environ.setdefault("TORCHINDUCTOR_FX_GRAPH_CACHE", "1")
        os.environ.setdefault("TORCHINDUCTOR_AUTOGRAD_CACHE", "1")
        os.environ.setdefault("CUDA_CACHE_MAXSIZE", "1073741824")
        import triton  # noqa: F401
        if os.name == "nt" and not shutil.which("cl.exe"):
            _TRITON_READY_CACHE = False
            return False
        _TRITON_READY_CACHE = True
        return True
    except Exception:
        _TRITON_READY_CACHE = False
        return False


def _get_triton_viterbi_backward():
    """Return a 100% bit-exact Triton GPU Semi-CRF viterbiBackward function, or None."""
    global _TRITON_VITERBI_FN
    if _TRITON_VITERBI_FN is not None:
        return _TRITON_VITERBI_FN
    if not _configure_isolated_triton():
        return None
    try:
        import torch
        import triton
        import triton.language as tl

        @triton.jit
        def _viterbi_backward_kernel(
            score_ptr,
            noise_ptr,
            ptr_out_ptr,
            T: tl.constexpr,
            BLOCK_T: tl.constexpr,
        ):
            b = tl.program_id(0)
            score_base = score_ptr + b * T * T
            noise_base = noise_ptr + b * (T - 1)
            ptr_base = ptr_out_ptr + b * (T - 1)
            offs = tl.arange(0, BLOCK_T)
            last_diag = tl.load(score_base + (T - 1) * T + (T - 1))
            last_q = tl.where(last_diag > 0.0, last_diag, 0.0)
            q = tl.where(offs == (T - 1), last_q, -1e30)
            for step in range(1, T):
                t = T - step - 1
                row_ptr = score_base + t * T
                valid_end = (offs > t) & (offs < T)
                s_row = tl.load(row_ptr + offs, mask=valid_end, other=-1e30)
                cand = tl.where(valid_end, q + s_row, -1e30)
                best_int_val = tl.max(cand, axis=0)
                is_best = valid_end & (cand == best_int_val)
                best_end_pos = tl.min(tl.where(is_best, offs, BLOCK_T), axis=0)
                q_next = tl.sum(tl.where(offs == (t + 1), q, 0.0), axis=0)
                skip_val = q_next + tl.load(noise_base + t)
                take_skip = skip_val >= best_int_val
                cur_v = tl.where(take_skip, skip_val, best_int_val)
                sel = tl.where(take_skip, -1, best_end_pos - (t + 1))
                tl.store(ptr_base + (step - 1), sel)
                diag_val = tl.load(row_ptr + t)
                q_t = cur_v + tl.where(diag_val > 0.0, diag_val, 0.0)
                q = tl.where(offs == t, q_t, q)

        def fast_viterbi_backward(score, noiseScore, forcedStartPos=None):
            T = int(score.shape[0])
            nBatch = int(score.shape[2])
            score_bbe = score.permute(2, 1, 0).contiguous()
            noise_bt = noiseScore.transpose(0, 1).contiguous()
            ptr_gpu = torch.empty((nBatch, T - 1), dtype=torch.int32, device=score.device)
            BLOCK_T = 1 << (T - 1).bit_length()
            _viterbi_backward_kernel[(nBatch,)](
                score_bbe, noise_bt, ptr_gpu, T=T, BLOCK_T=BLOCK_T, num_warps=4
            )
            diag_cpu = (torch.diagonal(score, dim1=0, dim2=1) > 0).cpu().numpy()
            ptr_cpu = ptr_gpu.cpu().numpy()
            if forcedStartPos is None:
                forcedStartPos = [0] * nBatch
            result = []
            for idx in range(nBatch):
                j = int(forcedStartPos[idx])
                cur_res = []
                cur_diag = diag_cpu[idx]
                cur_ptr = ptr_cpu[idx]
                while j < T - 1:
                    sel = int(cur_ptr[T - j - 2])
                    if cur_diag[j]:
                        cur_res.append((j, j))
                    if sel < 0:
                        j += 1
                    else:
                        i = sel + j + 1
                        cur_res.append((j, i))
                        j = i
                if cur_diag[T - 1]:
                    cur_res.append((T - 1, T - 1))
                result.append(cur_res)
            return result

        _TRITON_VITERBI_FN = fast_viterbi_backward
        return _TRITON_VITERBI_FN
    except Exception:
        return None


def _adapter_endpoint_path(*route) -> Path:
    directory = data_root() / ".cache"
    directory.mkdir(parents=True, exist_ok=True)
    return lease.leased_path(directory / "fast-adapter-worker.json", *route)


def _connect_adapter_daemon(python: str, src: str):
    import socket
    path = _adapter_endpoint_path(python, src)
    if not path.is_file():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        if info.get("python") != python or info.get("src") != src:
            return None
        sock = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2.0)
        sock.settimeout(1200.0)
        return sock, str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _start_adapter_daemon(python: str, src: str, env: dict):
    import secrets
    import time
    path = _adapter_endpoint_path(python, src)
    token = secrets.token_hex(16)
    cmd = [python, "-u", "-m", "astra.adapters", "serve", str(path), token, src]
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
        conn = _connect_adapter_daemon(python, src)
        if conn is not None and conn[1] == token:
            return conn
        if conn is not None:
            conn[0].close()
        time.sleep(0.15)
    return None


def _run_via_persistent_daemon(name: str, input_path: Path, output_dir: Path, options: dict, python: str, src: str, env: dict, log: Log):
    conn = _connect_adapter_daemon(python, src)
    if conn is None:
        with lease.start_lock(_adapter_endpoint_path(python, src)) as acquired:
            if acquired:
                conn = _connect_adapter_daemon(python, src) or _start_adapter_daemon(python, src, env)
    if conn is None:
        return None
    sock, token = conn
    try:
        payload = json.dumps({
            "token": token,
            "model": name,
            "input": str(input_path),
            "output": str(output_dir),
            "options": options,
        }, default=os.fspath) + "\n"
        sock.sendall(payload.encode("utf-8"))
        reader = sock.makefile("r", encoding="utf-8", errors="replace")
        result_obj = None
        error_msg = None
        for raw in reader:
            raw = raw.rstrip("\r\n")
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                log(f"[{name}] {raw}")
                continue
            kind = msg.get("kind")
            if kind == "log":
                line = str(msg.get("line", "")).strip()
                if line:
                    log(f"[{name}] {line}")
            elif kind == "result":
                result_obj = msg.get("data")
                break
            elif kind == "error":
                error_msg = str(msg.get("error") or "persistent worker error")
                break
        reader.close()
        if error_msg is not None:
            raise ModelError(f"{name} worker failed: {error_msg}")
        if result_obj is None:
            raise ModelError(f"{name} worker closed before confirming the result")
        return result_obj
    except ModelError:
        raise
    except OSError as exc:
        raise ModelError(f"{name} worker connection failed: {exc}") from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _serve_persistent(endpoint_path: Path, token: str, src: str) -> None:
    global _IN_PERSISTENT_DAEMON
    _IN_PERSISTENT_DAEMON = True
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
    info = {"port": port, "pid": os.getpid(), "python": os.fspath(sys.executable), "token": token, "src": src}
    tmp = endpoint_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info), encoding="utf-8")
    tmp.replace(endpoint_path)

    last_active = time.monotonic()

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

    lock = threading.Lock()
    try:
        while time.monotonic() - last_active < _PERSISTENT_IDLE_SECONDS:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            last_active = time.monotonic()
            try:
                req = lease.read_request(conn)
                if not isinstance(req, dict):
                    continue
                if req.get("token") != token:
                    continue

                def _send(obj):
                    if obj.get("kind") in ("result", "done", "error"):
                        done.set()
                    try:
                        conn.sendall((json.dumps(obj, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
                    except OSError:
                        pass

                with lock, lease.active_client(conn, abort) as done:
                    writer = _StreamWriter(_send)
                    old_out, old_err = sys.stdout, sys.stderr
                    sys.stdout = writer
                    sys.stderr = writer
                    try:
                        res = _WORKERS[req["model"]](Path(req["input"]), Path(req["output"]), req["options"])
                        writer.flush()
                        _send({"kind": "result", "data": res})
                    except Exception as exc:
                        writer.flush()
                        _send({"kind": "error", "error": f"{type(exc).__name__}: {exc}"})
                    finally:
                        sys.stdout, sys.stderr = old_out, old_err
                        gc.collect()
                        try:
                            import torch
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()
                        except Exception:
                            pass
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


def _gpu_adapter_cache_base(data_cache: Path, name: str, device: str | None) -> Path:
    """Keep executable compiler caches on this machine, outside versioned profiles.

    No stems, input audio or model output live here. Inductor and Triton key
    their own artifacts by compiler, device and graph/kernel inputs.
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if (os.name != "nt" or os.environ.get("ASTRA_RELEASE_NUMBER") != "4"
            or name not in _PERSISTENT_ADAPTERS or device == "cpu" or not local_app_data):
        return data_cache
    parent = Path(local_app_data)
    root = parent / "AstraStudio" / "kernels-v1"
    try:
        # Both adapters use the same leased worker. Keep its cache location
        # stable regardless of which of the two starts the process first.
        if not parent.is_absolute() or not parent.is_dir() or root.resolve() != root:
            return data_cache
        for folder in ("inductor", "triton", "cuda"):
            target = root / folder
            if target.resolve() != target:
                return data_cache
            target.mkdir(parents=True, exist_ok=True)
        return root
    except (OSError, RuntimeError):
        return data_cache


def _run_worker(name: str, input_path: Path, output_dir: Path, config: dict, log: Log):
    input_path = Path(input_path).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    options = {"model_dir": config.get("model_dir", "models")}
    options.update(config.get("models", {}).get(name, {}) if "models" in config else config)
    if name == "separator" and "instruments" in config:
        options["instruments"] = config["instruments"]
    python = os.fspath(lease.windowless_python(options.pop("python", None) or sys.executable))

    env = os.environ.copy()
    src = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH"))))
    env["PYTHONIOENCODING"] = "utf-8"
    profile_cache = data_root() / ".cache"
    cache_base = _gpu_adapter_cache_base(profile_cache, name, options.get("device"))
    # Torch's FX/Triton hits don't cover NVIDIA's driver-compiled kernels.
    # Share only the launcher's default CUDA cache for leased GPU adapters;
    # an explicit override and the independent DuET process keep their paths.
    if cache_base != profile_cache and env.get("CUDA_CACHE_PATH") in (None, str(profile_cache / "cuda")):
        env["CUDA_CACHE_PATH"] = str(cache_base / "cuda")
    ind_dir = cache_base / "inductor"
    tri_dir = cache_base / "triton"
    ind_dir.mkdir(parents=True, exist_ok=True)
    tri_dir.mkdir(parents=True, exist_ok=True)
    env.setdefault("TORCHINDUCTOR_CACHE_DIR", str(ind_dir))
    env.setdefault("TRITON_CACHE_DIR", str(tri_dir))
    env.setdefault("TORCHINDUCTOR_FX_GRAPH_CACHE", "1")
    env.setdefault("TORCHINDUCTOR_AUTOGRAD_CACHE", "1")
    env.setdefault("CUDA_CACHE_MAXSIZE", "1073741824")
    if options.get("device") == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = "-1"

    with tempfile.TemporaryDirectory(prefix=f"astra-{name}-") as temp:
        request_path = Path(temp) / "request.json"
        result_path = Path(temp) / "result.json"
        request = {"input": str(input_path), "output": str(output_dir), "options": options}
        request_path.write_text(json.dumps(request, default=os.fspath), encoding="utf-8")
        command = [python, "-u", "-m", "astra.adapters", "worker", name, str(request_path), str(result_path)]
        from .resource_admission import wait_for_model_admission
        wait_for_model_admission(options.get("device"), name, log)
        log(f"[{name}] {input_path.name} ({python})")
        if name in _PERSISTENT_ADAPTERS and options.get("device") != "cpu" and lease.enabled():
            daemon_res = _run_via_persistent_daemon(name, input_path, output_dir, options, python, src, env, log)
            if daemon_res is not None:
                return daemon_res
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ModelError(f"Cannot start {name} worker with {python}: {exc}") from exc

        last_line = ""
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if line.strip():
                    last_line = line.strip()
                    log(f"[{name}] {last_line}")
            exit_code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
        if exit_code or not result_path.is_file():
            raise ModelError(f"{name} worker failed (exit {exit_code}): {last_line or 'no result'}")
        return json.loads(result_path.read_text(encoding="utf-8"))


def _local_file(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"Missing local model asset: {path}. See docs/MODELS.md.")
    return path


def _torch_device(torch, options: dict, default: str = "auto"):
    requested = options.get("device", default)
    if requested == "auto":
        requested = "cuda" if torch.cuda.is_available() else "cpu"
    device = torch.device(requested)
    if device.type not in ("cpu", "cuda"):
        raise ValueError(f"Expected device auto, cpu or cuda, received {requested!r}")
    if device.type == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but unavailable. Check the PyTorch CUDA build and NVIDIA driver.")
        if device.index is not None:
            torch.cuda.set_device(device)
    print(f"Using {device} (PyTorch {torch.__version__})", flush=True)
    return device


def _enable_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return

    import imageio_ffmpeg

    binary = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve(strict=True)
    expected_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if binary.name.lower() != expected_name:
        directory = data_root() / "tools"
        directory.mkdir(exist_ok=True)
        target = directory / expected_name
        if not target.is_file():
            # The package executable has a versioned name. audio-separator invokes ffmpeg by name.
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as file:
                temporary = Path(file.name)
            try:
                shutil.copy2(binary, temporary)
                if not target.is_file():
                    os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        binary = target
    os.environ["PATH"] = os.pathsep.join((str(binary.parent), os.environ.get("PATH", "")))


@contextmanager
def _background_adapter_tools():
    """Hide subprocess windows only during isolated model inference."""
    if os.name != "nt":
        yield
        return
    original = subprocess.Popen

    class _BackgroundPopen(original):
        def __init__(self, *args, **kwargs):
            # FFmpeg checks and Torch/Triton compilers can launch
            # subprocesses without Windows flags. Preserve all pipe settings.
            if len(args) <= 13:
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            else:  # subprocess.Popen also accepts creationflags positionally.
                args = (*args[:13], args[13] | subprocess.CREATE_NO_WINDOW, *args[14:])
            super().__init__(*args, **kwargs)

    subprocess.Popen = _BackgroundPopen
    try:
        yield
    finally:
        subprocess.Popen = original


def _separator(input_path: Path, output_dir: Path, options: dict) -> dict[str, str]:
    with _background_adapter_tools():
        return _separator_models(input_path, output_dir, options)


def _separator_models(input_path: Path, output_dir: Path, options: dict) -> dict[str, str]:
    selected = set(options.get("instruments", ("piano", "guitar")))
    if "piano" in selected and options.get("piano_model") == "xlance":
        import gc
        import torch
        paths = _separator_with_precision_fallback(input_path, output_dir, {**options, "instruments": ["piano"], "local_model": options["piano_model"]})
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        others = selected - {"piano"}
        if others:
            paths.update(_separator_models(input_path, output_dir, {**options, "instruments": sorted(others)}))
        return paths
    return _separator_with_precision_fallback(input_path, output_dir, options)


def _separator_with_precision_fallback(input_path: Path, output_dir: Path, options: dict):
    """A bounded CUDA OOM retry keeps the model, native context and overlap.

    FP32 is the quality preset; AMP is only a memory fallback, not a different
    separation model. No retries on corrupt models, CPU errors or failed AMP.
    """
    import gc
    import torch
    try:
        return _separator_single(input_path, output_dir, options)
    except torch.cuda.OutOfMemoryError:
        requested = str(options.get("device", "auto"))
        if options.get("autocast", False) or options.get("native_fp16", False) or requested == "cpu" or not torch.cuda.is_available():
            raise
    # Leave the exception scope first so its traceback releases model tensors.
    gc.collect()
    torch.cuda.empty_cache()
    print("[separator] FP32 显存不足，保持模型、窗口和重叠数，以 FP16 重试一次。", flush=True)
    return _separator_single(input_path, output_dir, {**options, "native_fp16": True, "autocast": False, "_precision_fallback": "cuda_oom"})


@contextmanager
def _observe_separator_graph_cache(enabled: bool):
    if not enabled:
        yield
        return
    import logging
    logger = logging.getLogger("torch._inductor.codecache")
    old_level = logger.level
    reported = set()

    class _ObserveCache(logging.Filter):
        def filter(self, record):
            template = record.msg
            if isinstance(template, str):
                for state in ("hit", "miss"):
                    if template.startswith(f"fx graph cache {state} for key") and state not in reported:
                        reported.add(state)
                        print(f"[gpu-graph] {state}", flush=True)
            # Observe INFO for timing, without enabling any extra Torch log output.
            return record.levelno >= old_effective_level

    old_effective_level = logger.getEffectiveLevel()
    observer = _ObserveCache()
    logger.addFilter(observer)
    logger.setLevel(min(old_effective_level, logging.INFO))
    try:
        yield
    finally:
        logger.removeFilter(observer)
        logger.setLevel(old_level)


def _separator_single(input_path: Path, output_dir: Path, options: dict) -> dict[str, str]:
    xlance = options.get("local_model") == "xlance"
    filename = XLANCE_PIANO_MODEL if xlance else SEPARATOR_MODEL
    local_config = XLANCE_PIANO_CONFIG if xlance else None
    selected = set(options.get("instruments", STEMS))
    if not selected or not selected.issubset(STEMS):
        raise ValueError(f"Expected one or more BS-RoFormer stems: {', '.join(STEMS)}")
    model_dir = Path(options["model_dir"]).resolve()
    for asset in ((filename, local_config) if local_config else SEPARATOR_ASSETS):
        _local_file(model_dir / asset)

    _enable_ffmpeg()
    import torch
    from audio_separator.separator import Separator

    class LocalSeparator(Separator):
        def download_model_files(self, model_filename):
            if local_config and model_filename == filename:
                # Explicit local registry for the author's public specialist.
                # Avoid requesting the upstream online model catalogue.
                return (model_filename, "MDXC", filename, str(model_dir / model_filename), local_config)
            return super().download_model_files(model_filename)

        def download_file_if_not_exists(self, url, output_path, verify_checksum=False):
            # v0.48.0 calls this method for cached assets and may re-download on a hash mismatch.
            path = _local_file(Path(output_path))
            if verify_checksum and not self.verify_model_file_checksum(str(path)):
                raise ValueError(f"Model asset failed upstream SHA-256 validation: {path}")

    device = _torch_device(torch, options)
    if device.type == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        torch.backends.cudnn.benchmark = False
    os.environ["AUDIO_SEPARATOR_MODEL_DIR"] = str(model_dir)
    use_amp = device.type == "cuda" and bool(options.get("autocast", False))
    use_native_fp16 = device.type == "cuda" and bool(options.get("native_fp16", False)) and not use_amp
    triton_ok = (device.type == "cuda" and bool(options.get("torch_compile", True))
                 and _configure_isolated_triton())
    use_compile = bool(triton_ok and (use_native_fp16 or use_amp))
    precision_label = "FP16" if use_native_fp16 else ("AMP" if use_amp else "FP32")
    mdxc_params = {
        "batch_size": 1,
        "segment_size": options.get("piano_segment_size", 1101) if xlance else options.get("segment_size", 801),
        "override_model_segment_size": True,
        "overlap": options.get("overlap", 2),
        "pitch_shift": 0,
    }
    cache_key = (
        filename, str(model_dir), mdxc_params["segment_size"], mdxc_params["overlap"],
        use_native_fp16, use_amp, use_compile, str(device),
    )
    cached_sep = _PERSISTENT_MODEL_CACHE.get("separator") if _IN_PERSISTENT_DAEMON else None
    if cached_sep is not None and cached_sep[0] == cache_key:
        separator = cached_sep[1]
        separator.output_dir = str(output_dir)
        separator.model_instance.output_dir = str(output_dir)
        if device.type == "cuda":
            separator.model_instance.model_run.to(device)
    else:
        if _IN_PERSISTENT_DAEMON:
            _PERSISTENT_MODEL_CACHE.pop("separator", None)
        separator = LocalSeparator(
            model_file_dir=str(model_dir),
            output_dir=str(output_dir),
            output_format="WAV",
            sample_rate=44100,
            use_soundfile=True,
            normalization_threshold=1.0,
            use_autocast=use_amp,
            use_native_fp16=use_native_fp16,
            use_torch_compile=use_compile,
            mdxc_params=mdxc_params,
        )
        separator.load_model(filename)
        separator.model_instance._astra_orig_final_process = separator.model_instance.final_process
        if _IN_PERSISTENT_DAEMON:
            _PERSISTENT_MODEL_CACHE["separator"] = (cache_key, separator)

    compiled_active = bool(getattr(separator.model_instance, "is_torch_compiled", False))
    print(f"[separator] {filename}: segment={mdxc_params['segment_size']}, overlap={mdxc_params['overlap']}, "
          f"precision={precision_label}, compile={compiled_active}", flush=True)
    model_inst = separator.model_instance
    orig_write_stem = getattr(model_inst, "_astra_orig_final_process", model_inst.final_process)
    if selected != set(STEMS):
        # v0.48.0 exports every MDXC multi-stem source, even with output_single_stem.
        # Filter at its final writer while preserving the full model inference.
        def write_selected(stem_path, source, stem_name):
            mapped = "piano" if xlance and stem_name.lower() == "keys" else stem_name.lower()
            if mapped in selected:
                return orig_write_stem(stem_path, source, stem_name)

        model_inst.final_process = write_selected
    else:
        model_inst.final_process = orig_write_stem

    names = {name: name for name in STEMS}
    names.update({name.title(): name for name in STEMS})
    if xlance:
        names["keys"] = "piano"
    print(f"[first-window] {'piano' if xlance else 'audio'}", flush=True)
    try:
        with _observe_separator_graph_cache(compiled_active), torch.inference_mode():
            returned = separator.separate(str(input_path), custom_output_names=names)
    finally:
        if _IN_PERSISTENT_DAEMON and device.type == "cuda" and not compiled_active:
            try:
                separator.model_instance.model_run.to("cpu")
            except Exception:
                pass
    paths = {}
    for item in returned:
        path = Path(item)
        if not path.is_absolute():
            path = output_dir / path
        if path.stem in selected and path.suffix.lower() == ".wav" and path.is_file():
            paths[path.stem] = str(path.resolve())
    missing = selected.difference(paths)
    if missing:
        raise RuntimeError(f"Separation did not produce these WAV stems: {', '.join(sorted(missing))}")
    (output_dir / ("xlance_separation.json" if xlance else "sw_separation.json")).write_text(json.dumps({
        "model": filename, "device": str(device), "segment_size": mdxc_params["segment_size"],
        "overlap": mdxc_params["overlap"], "precision": precision_label,
        "precision_fallback": options.get("_precision_fallback"), "sample_rate": 44100,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def _note(pitch, start, end, velocity) -> dict:
    return {
        "pitch": int(pitch),
        "start": float(start),
        "end": float(end),
        "velocity": max(1, min(127, int(velocity))),
    }


def _transkun(input_path: Path, output_dir: Path, options: dict) -> list[dict]:
    with _background_adapter_tools():
        return _transkun_model(input_path, output_dir, options)


def _transkun_model(input_path: Path, output_dir: Path, options: dict) -> list[dict]:
    import moduleconf
    import soundfile as sf
    import soxr
    import torch
    import transkun

    pretrained = Path(transkun.__file__).parent / "pretrained"
    checkpoint_path = _local_file(Path(options.get("checkpoint") or pretrained / "2.0.pt"))
    config_path = _local_file(Path(options.get("config_path") or pretrained / "2.0.conf"))
    device = _torch_device(torch, options)
    if device.type == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        torch.backends.cudnn.benchmark = False
    tk_key = (str(checkpoint_path), str(config_path), str(device))
    cached_tk = _PERSISTENT_MODEL_CACHE.get("transkun") if _IN_PERSISTENT_DAEMON else None
    if cached_tk is not None and cached_tk[0] == tk_key:
        model = cached_tk[1]
        model = model.to(device).eval()
    else:
        if _IN_PERSISTENT_DAEMON:
            _PERSISTENT_MODEL_CACHE.pop("transkun", None)
        model_spec = moduleconf.parseFromFile(str(config_path))["Model"]
        checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
        state = checkpoint.get("best_state_dict") or checkpoint["state_dict"]
        model = model_spec.module.TransKun(conf=model_spec.config)
        model.load_state_dict(state, strict=False)
        model = model.to(device).eval()
        del checkpoint, state
        if _IN_PERSISTENT_DAEMON:
            _PERSISTENT_MODEL_CACHE["transkun"] = (tk_key, model)

    audio, sample_rate = sf.read(input_path, dtype="float32", always_2d=True)
    if sample_rate != model.fs:
        audio = soxr.resample(audio, sample_rate, model.fs)
    segment = options.get("segment_seconds", model.segmentSizeInSecond)
    hop = options.get("hop_seconds", model.segmentHopSizeInSecond)
    if not 0 < hop <= segment:
        raise ValueError("Transkun requires 0 < hop_seconds <= segment_seconds")
    from .transcription_progress import transkun_progress
    from transkun import CRF
    import torch.utils.checkpoint as tcp

    audio_gpu = torch.from_numpy(audio).to(device)
    orig_ckpt = tcp.checkpoint
    orig_proc = model.processFramesBatch
    orig_crf_decode = CRF.NeuralSemiCRFInterval.decode
    if device.type == "cuda":
        # Bypass training-only gradient checkpointing wrapper in inference_mode,
        # run Transformer backbone/scorer in FP16 Tensor Cores, cast CRF scores
        # to FP32, and use exact Triton GPU Semi-CRF Viterbi decoding (F1=1.00000).
        tcp.checkpoint = lambda fn, *args, **kwargs: fn(*args)

        def _fp16_backbone_fp32_crf(frames_batch):
            with torch.amp.autocast("cuda", dtype=torch.float16):
                crf, ctx = orig_proc(frames_batch)
            return CRF.NeuralSemiCRFInterval(crf.score.float(), crf.noiseScore.float()), ctx.float()

        model.processFramesBatch = _fp16_backbone_fp32_crf
        triton_viterbi = _get_triton_viterbi_backward()
        if triton_viterbi is not None:
            def _fast_crf_decode(self, forcedStartPos=None, forward=True):
                if not forward and self.score.is_cuda:
                    try:
                        return triton_viterbi(self.score, self.noiseScore, forcedStartPos)
                    except Exception:
                        pass
                return orig_crf_decode(self, forcedStartPos=forcedStartPos, forward=forward)

            CRF.NeuralSemiCRFInterval.decode = _fast_crf_decode
    try:
        with torch.inference_mode(), transkun_progress(model,len(audio),segment,hop,
                                                emit=lambda line: print(line,flush=True)):
            events = model.transcribe(
                audio_gpu,
                stepInSecond=hop,
                segmentSizeInSecond=segment,
                discardSecondHalf=False,
            )
    finally:
        tcp.checkpoint = orig_ckpt
        model.processFramesBatch = orig_proc
        CRF.NeuralSemiCRFInterval.decode = orig_crf_decode
        if _IN_PERSISTENT_DAEMON and device.type == "cuda":
            try:
                model.to("cpu")
            except Exception:
                pass
    # Transkun's Data.writeMidi maps a negative pitch to controller -pitch.
    pedals = [
        {"number": -int(event.pitch), "start": float(event.start), "end": float(event.end)}
        for event in events
        if event.pitch in (-64, -66, -67)
    ]
    (output_dir / "pedals.json").write_text(json.dumps(pedals, allow_nan=False), encoding="utf-8")
    return [_note(event.pitch, event.start, event.end, event.velocity) for event in events if 0 <= event.pitch <= 127]


def _basic_pitch(input_path: Path, output_dir: Path, options: dict) -> list[dict]:
    from basic_pitch import FilenameSuffix, build_icassp_2022_model_path
    from basic_pitch.inference import predict, Model
    import onnxruntime as ort

    model_path = options.get("model_path") or build_icassp_2022_model_path(FilenameSuffix.onnx)
    model_path = _local_file(Path(model_path))
    if model_path.suffix.lower() != ".onnx":
        raise ValueError("Basic Pitch requires a local .onnx model")
    requested = options.get('device', 'auto')
    if requested not in ('auto', 'cpu', 'cuda'):
        raise ValueError('Basic Pitch device must be auto, cpu or cuda')
    available = ort.get_available_providers()
    use_gpu = requested == 'cuda' or requested == 'auto' and 'CUDAExecutionProvider' in available
    if use_gpu and 'CUDAExecutionProvider' not in available:
        raise RuntimeError('Basic Pitch GPU runtime is missing; install onnxruntime-gpu or select CPU')
    if use_gpu:
        # Reuse the CUDA12/cuDNN9 runtime already present in Astra's isolated
        # PyTorch environment. No torch import or system PATH modification.
        runtime = Path(options.get('cuda_runtime_dir') or
                       data_root() / '.venv' / 'Lib' / 'site-packages' / 'torch' / 'lib')
        if hasattr(ort, 'preload_dlls'):
            ort.preload_dlls(directory=str(runtime) if runtime.is_dir() else None)
    providers = [('CUDAExecutionProvider', {'gpu_mem_limit': 1024 * 1024 * 1024,
                  'arena_extend_strategy': 'kSameAsRequested', 'cudnn_conv_use_max_workspace': '0',
                  'cudnn_conv_algo_search': 'HEURISTIC'}),
                 'CPUExecutionProvider'] if use_gpu else ['CPUExecutionProvider']
    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = int(options.get('threads', 2))
    session = ort.InferenceSession(str(model_path), sess_options=session_options, providers=providers)
    session.disable_fallback()
    if use_gpu and 'CUDAExecutionProvider' not in session.get_providers():
        raise RuntimeError('Basic Pitch CUDA provider failed to load; select CPU instead of silently falling back')
    print(f'Basic Pitch ONNX providers: {session.get_providers()}', flush=True)
    if use_gpu:
        print('Basic Pitch 首个 CUDA 窗口可能需要较长初始化；后续窗口复用同一推理会话。', flush=True)
    # Upstream 0.4.0 hardcodes CPU in Model.__init__. Its public predict()
    # accepts a Model object; reuse its unmodified inference/note decoding with
    # the explicitly chosen ONNX session rather than monkeypatching the package.
    model = Model.__new__(Model)
    model.model_type = Model.MODEL_TYPES.ONNX
    model.model = session
    _, _, events = predict(
        str(input_path),
        model_or_model_path=model,
        onset_threshold=options.get("onset_threshold", 0.5),
        frame_threshold=options.get("frame_threshold", 0.3),
        minimum_note_length=options.get("minimum_note_length_ms", 127.7),
        minimum_frequency=options.get("minimum_frequency"),
        maximum_frequency=options.get("maximum_frequency"),
        melodia_trick=options.get("melodia_trick", True),
    )
    return [_note(pitch, start, end, round(127 * amplitude)) for start, end, pitch, amplitude, _ in events]


def _beat_this(input_path: Path, output_dir: Path, options: dict) -> dict[str, list[float]]:
    default_checkpoint = Path(options["model_dir"]) / "final0.ckpt"
    checkpoint = _local_file(Path(options.get("checkpoint") or default_checkpoint).resolve())

    import soundfile as sf
    import torch
    from importlib.metadata import version
    from beat_this.inference import Audio2Frames
    from beat_this.model.postprocessor import Postprocessor
    from .beat_cache import activation_identity, load_activations, save_activations

    identity = activation_identity(input_path, checkpoint, version('beat_this'))
    activation_path = output_dir / 'activations.npz'
    activations = load_activations(activation_path, identity)
    if activations is None:
        device = _torch_device(torch, options, default="cpu")
        model = Audio2Frames(checkpoint_path=str(checkpoint), device=device, float16=False)
        signal, sample_rate = sf.read(input_path, dtype="float32", always_2d=True)
        with torch.inference_mode():
            beat_logits, downbeat_logits = model(signal, sample_rate)
        activations = (beat_logits.cpu().numpy(), downbeat_logits.cpu().numpy())
        save_activations(activation_path, identity, *activations)
        print('已缓存节拍激活；后续解码可复用，无需重新运行模型。', flush=True)
    else:
        print('复用节拍激活缓存，跳过模型加载与推理。', flush=True)
    # Preserve original minimal detections for diagnostics and explicit fallback.
    beats, downbeats = Postprocessor(type='minimal')(
        torch.from_numpy(activations[0]), torch.from_numpy(activations[1]))
    result = {
        "beats": sorted({float(value) for value in beats if math.isfinite(value) and value >= 0}),
        "downbeats": sorted({float(value) for value in downbeats if math.isfinite(value) and value >= 0}),
    }
    if options.get('decoder') == 'bar_dbn_v1':
        from .rhythm_decode import decode_cached_activations
        try:
            result['bar_result']=decode_cached_activations(activation_path,options.get('decoder_python'))
            print('已按节拍概率联合解码小节与变拍；保留原始拍点供核对。',flush=True)
        except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as exc:
            result['decoder_warning']=f'变拍解码未完成，使用原节拍分析：{exc}'
            print(result['decoder_warning'],flush=True)
    return result


def _cubase_local(input_path: Path, output_dir: Path, options: dict):
    # Lazy import: the legacy path does not load another model or its assets.
    from .cubase_local_worker import analyze_audio
    return analyze_audio(input_path, output_dir, options,
                         log=lambda message: print(message.removeprefix('[cubase_local] '), flush=True))


_WORKERS = {
    "separator": _separator,
    "transkun": _transkun,
    "basic_pitch": _basic_pitch,
    "beat_this": _beat_this,
    "cubase_local": _cubase_local,
}


def main() -> None:
    if len(sys.argv) >= 5 and sys.argv[1] == "serve":
        _serve_persistent(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["worker"])
    parser.add_argument("model", choices=_WORKERS)
    parser.add_argument("request", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    result = _WORKERS[args.model](Path(request["input"]), Path(request["output"]), request["options"])
    args.result.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
