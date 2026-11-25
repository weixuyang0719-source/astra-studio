"""Private ChordFormer process: official CQT, five-fold mean, global full HMM."""
from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import sys
from time import monotonic


def run(request):
    root = Path(request["root"])
    device = request["device"]
    if device not in ("cpu", "cuda"):
        raise ValueError("Invalid execution device")
    os.environ["ASTRA_CHORDFORMER_DEVICE"] = device
    if device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    source = root / "vendor/chordformer"
    sys.path[:0] = [str(root / "vendor/chordformer-deps"), str(source)]
    os.chdir(source)  # Official import loads training-fold CSV and dictionary here.
    import torch
    import numpy as np
    torch.set_num_threads(4)
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA 不可用，请在高级设置选择 CPU")
    from confor_head16 import ChordNet
    from mir.nn.train import NetworkInterface
    from extractors.cqt import CQTV2
    from extractors.xhmm_ismir import XHMMDecoder
    from mir import DataEntry, io
    from settings import DEFAULT_SR, DEFAULT_HOP_LENGTH
    from astra.chordformer import SOURCE_COMMIT

    began = monotonic()
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    entry = DataEntry()
    entry.prop.set("sr", DEFAULT_SR)
    entry.prop.set("hop_length", DEFAULT_HOP_LENGTH)
    entry.append_file(request["audio"], io.MusicIO, "music")
    entry.append_extractor(CQTV2, "cqt")
    print("计算原曲 CQT", flush=True)
    features = entry.cqt
    duration = len(entry.music) / DEFAULT_SR
    probabilities = []
    with torch.inference_mode():
        for fold in range(5):
            name = f"chordformer_head16(1.0,1.0)_s{fold}.best"
            print(f"模型 {fold + 1}/5：{len(features)} 帧，全曲推理", flush=True)
            net = NetworkInterface(ChordNet(None), name, load_checkpoint=False,
                                   load_path=str(root / "models/chordformer"))
            if net.net.use_gpu != (device == "cuda"):
                raise RuntimeError("ChordFormer device selection mismatch")
            probabilities.append(net.inference(features))
            del net
            gc.collect()
        probabilities = [np.mean([p[i] for p in probabilities], axis=0)
                         for i in range(len(probabilities[0]))]
        print("全曲 full 词表 HMM 解码", flush=True)
        decoder = XHMMDecoder(template_file=str(source / "data/full_chord_list.txt"))
        labels = decoder.decode_to_chordlab(entry, probabilities, False)
    segments = [{"start": float(start), "end": min(float(end), duration), "label": str(label)}
                for start, end, label in labels if float(start) < duration]
    # CQT's last centered frame can extend <1 hop beyond audio; only clip endpoint.
    result = {"segments": segments, "duration_seconds": duration,
              "model": {"name": "ChordFormer", "source_commit": SOURCE_COMMIT,
                        "ensemble": 5, "vocabulary": "full", "decoder": "official XHMMDecoder"},
              "execution": {"device": device, "seconds": monotonic() - began,
                            "torch": torch.__version__, "frames": len(features),
                            "inference": "official_full_sequence",
                            "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated() if device == "cuda" else 0},
              "diagnostics": {"upstream_end_seconds": float(labels[-1][1]),
                              "endpoint_clipped_to_audio": float(labels[-1][1]) > duration,
                              "time_unit": "original_audio_seconds"}}
    Path(request["output"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"识别完成：{len(segments)} 段，{result['execution']['seconds']:.1f} 秒", flush=True)


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
