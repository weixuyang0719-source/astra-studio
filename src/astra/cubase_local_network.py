"""Independent NumPy replay of the network in an installed, audited component.

This module neither loads executable DLL code nor bundles extracted parameters.
It reads the user's installed component in memory, verifies its exact build and
embedded model, then validates inference against the model's embedded fixture.
Input is *already computed* 320-dimensional features, not audio. Passing this
self-test does not establish equivalence of audio features or the host pipeline.
"""
import base64
import hashlib
import json
import struct
from pathlib import Path

import numpy as np


DLL_PATH = Path('C:/Program Files/Steinberg/Cubase 16/Components/beattracking.dll')
DLL_SHA256 = '77f3fa1d1a15362dba8244e155788a997d0393c284d613d9633d11ca4d9a452d'
MODEL_SHA256 = 'c9439b820e86bb5a59ec074cdfa3e576ed8784229c19b23a7aa9ff062ad7757f'
MODEL_OFFSET = 4049876
MODEL_LENGTH = 604840
FILTER_RVA = 0x341480
LAYER_NAMES = ('bidirectional', 'bidirectional_1', 'bidirectional_2')


def _floats(chunks, count):
    values = np.frombuffer(base64.b64decode(''.join(chunks), validate=True), dtype='<f4')
    if values.size != count or not np.isfinite(values).all():
        raise ValueError('本机节拍网络参数尺寸或数值无效')
    return values.copy()


def _rva_offset(raw, rva):
    """Minimal read-only PE section mapping, no loader or private ABI calls."""
    pe = struct.unpack_from('<I', raw, 0x3c)[0]
    if raw[pe:pe + 4] != b'PE\0\0':
        raise ValueError('本机节拍组件不是有效 PE 文件')
    sections = struct.unpack_from('<H', raw, pe + 6)[0]
    optional_size = struct.unpack_from('<H', raw, pe + 20)[0]
    for index in range(sections):
        section = pe + 24 + optional_size + index * 40
        size, address, raw_size, offset = struct.unpack_from('<IIII', raw, section + 8)
        if address <= rva < address + max(size, raw_size):
            if rva - address >= raw_size:
                raise ValueError('所需参数不在组件文件数据中')
            return offset + rva - address
    raise ValueError('组件中找不到所需特征参数')


def _sigmoid(x):
    # Stable finite float32 sigmoid, mathematically identical on finite input.
    return np.exp(-np.logaddexp(np.float32(0), -x))


class LocalBeatNetwork:
    """Three BiLSTM layers (25 units each direction) and three-class softmax."""

    def __init__(self, model):
        params = model['trainable_params']
        self.layers = []
        input_size = 320
        for name in LAYER_NAMES:
            layer = []
            for direction in ('forward', 'backward'):
                layer.append((
                    _floats(params[name][direction + '_weights'], input_size * 100).reshape(input_size, 100),
                    _floats(params[name][direction + '_recurrent_weights'], 2500).reshape(25, 100),
                    _floats(params[name][direction + '_bias'], 100),
                ))
            self.layers.append(layer)
            input_size = 50
        self.dense = _floats(params['time_distributed']['weights'], 150).reshape(50, 3)
        self.bias = _floats(params['time_distributed']['bias'], 3)
        self.self_test = None
        self._torch_models = {}

    def predict(self, features):
        values = np.asarray(features, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != 320 or not np.isfinite(values).all():
            raise ValueError('节拍网络需要有限的 N × 320 特征，不接受音频或其它模型概率')
        if not len(values):
            return np.empty((0, 3), dtype=np.float32)
        for layer in self.layers:
            outputs = []
            for backward, (weight, recurrent, bias) in enumerate(layer):
                projected = values @ weight + bias
                h = np.zeros(25, dtype=np.float32)
                c = h.copy()
                output = np.empty((len(values), 25), dtype=np.float32)
                indices = range(len(values) - 1, -1, -1) if backward else range(len(values))
                for index in indices:
                    gates = projected[index] + h @ recurrent
                    gi, gf, gc, go = np.split(gates, 4)
                    c = _sigmoid(gf) * c + _sigmoid(gi) * np.tanh(gc)
                    h = _sigmoid(go) * np.tanh(c)
                    output[index] = h
                outputs.append(output)
            values = np.concatenate(outputs, axis=1)
        logits = values @ self.dense + self.bias
        result = np.exp(logits - logits.max(axis=1, keepdims=True))
        result /= result.sum(axis=1, keepdims=True)
        if not np.isfinite(result).all():
            raise ValueError('节拍网络产生了无效概率，已拒绝继续解码')
        return result

    def predict_torch(self, features, *, device):
        """Optional backend for the application's explicitly resolved CPU/GPU.

        The UI/device policy decides the device before starting the worker.
        No silent CPU fallback, half precision, chunking or changed recurrence.
        """
        import torch

        values = np.asarray(features, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != 320 or not np.isfinite(values).all():
            raise ValueError('节拍网络需要有限的 N × 320 特征')
        if device not in ('cpu', 'cuda'):
            raise ValueError('请明确选择节拍网络的 CPU 或 GPU')
        if device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('本次选择的 GPU 不可用，请重新选择设备')
        if not len(values):
            return np.empty((0, 3), dtype=np.float32)
        if device not in self._torch_models:
            layers = []
            for index, weights in enumerate(self.layers):
                layer = torch.nn.LSTM(320 if index == 0 else 50, 25,
                                      bidirectional=True, batch_first=True)
                with torch.no_grad():
                    for backward, (weight, recurrent, bias) in enumerate(weights):
                        suffix = '_reverse' if backward else ''
                        getattr(layer, 'weight_ih_l0' + suffix).copy_(torch.from_numpy(weight.T.copy()))
                        getattr(layer, 'weight_hh_l0' + suffix).copy_(torch.from_numpy(recurrent.T.copy()))
                        getattr(layer, 'bias_ih_l0' + suffix).copy_(torch.from_numpy(bias))
                        getattr(layer, 'bias_hh_l0' + suffix).zero_()
                layers.append(layer.to(device).eval())
            dense = torch.from_numpy(self.dense).to(device)
            bias = torch.from_numpy(self.bias).to(device)
            self._torch_models[device] = (layers, dense, bias)
        layers, dense, bias = self._torch_models[device]
        # cuDNN's default TF32 changed the embedded-fixture probabilities by
        # 3e-4 on the target GPU. Keep this replay on full FP32 arithmetic.
        with torch.inference_mode(), torch.backends.cudnn.flags(allow_tf32=False):
            result = torch.from_numpy(values).unsqueeze(0).to(device)
            for layer in layers:
                result, _ = layer(result)
            result = torch.softmax(result[0] @ dense + bias, dim=-1).cpu().numpy()
        if not np.isfinite(result).all():
            raise ValueError('节拍网络产生了无效概率，已拒绝继续解码')
        return result

    def verify(self, fixtures):
        errors = []
        for fixture in fixtures:
            source, target = fixture['inputs'][0], fixture['outputs'][0]
            x = _floats(source['values'], int(np.prod(source['shape']))).reshape(source['shape'])
            y = _floats(target['values'], int(np.prod(target['shape']))).reshape(target['shape'])
            predicted = self.predict(x)
            if predicted.shape != y.shape:
                raise ValueError('本机节拍网络内置测试输出形状不一致')
            errors.append(float(np.max(np.abs(predicted - y))))
        if not errors or max(errors) > 1e-5:
            raise ValueError('本机节拍网络内置测试未通过，已拒绝使用')
        self.self_test = {'cases': len(errors), 'max_absolute_error': max(errors), 'tolerance': 1e-5}
        return dict(self.self_test)


def load_installed_network(path=DLL_PATH):
    """Read local parameters only; no persistence, DLL execution or download."""
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != DLL_SHA256:
        raise ValueError('本机节拍组件版本尚未核验，请勿套用其它版本的偏移和参数')
    payload = raw[MODEL_OFFSET:MODEL_OFFSET + MODEL_LENGTH]
    if hashlib.sha256(payload).hexdigest() != MODEL_SHA256:
        raise ValueError('节拍网络内容与已核验版本不符')
    model = json.loads(payload)
    network = LocalBeatNetwork(model)
    network.verify(model['tests'])
    offset = _rva_offset(raw, FILTER_RVA)
    filters, consumed = json.JSONDecoder().raw_decode(raw[offset:offset + 200000].decode('ascii', errors='replace'))
    bands = []
    for scale, (window, count) in enumerate(zip((1024, 2048, 4096), (22, 46, 92))):
        weights = np.zeros((window // 2 + 1, count), dtype=np.float32)
        if len(filters[str(scale)]) != count:
            raise ValueError('本机节拍滤波器频带数不匹配')
        for band in range(count):
            for bin_text, value in filters[str(scale)][str(band)].items():
                bin_index = int(bin_text)
                if not 0 <= bin_index < len(weights) or not np.isfinite(value) or value < 0:
                    raise ValueError('本机节拍滤波器系数无效')
                weights[bin_index, band] = value
        bands.append(weights)
    provenance = {'kind': 'independent_network', 'method': 'local_native_model_replay',
                  'component_sha256': DLL_SHA256, 'model_sha256': MODEL_SHA256,
                  'filter_sha256': hashlib.sha256(raw[offset:offset + consumed]).hexdigest(),
                  'network_self_test': dict(network.self_test),
                  'audio_feature_equivalence': False, 'host_equivalence': False}
    return network, bands, provenance
