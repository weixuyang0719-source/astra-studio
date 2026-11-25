"""Interpret measured model progress without estimating the entire desktop job."""

from collections import deque
import json
import math
import re
from statistics import median
from time import monotonic

from .resource_admission import WAITING_PREFIX, RESUMED_PREFIX


_SEPARATOR_WINDOW = re.compile(
    r'^\[separator\] \d+%\|[^|]*\|\s*(\d+)/(\d+) \[[^,]+,\s*(.+)\]$'
)
_SEPARATOR_START = re.compile(r"^\[separator\] \[first-window\] (piano|audio)$")
_RATE = re.compile(r'(\d+(?:\.\d+)?)(it/s|s/it)')
_DETAILS = (
    ('[cubase_local] 先核对记谱拍值', '核对音乐拍值，保持演奏时间不变'),
    ('[cubase_local] 核对变拍前后', '核对变拍前后的完整小节'),
    ('[cubase_local] 验证本机组件', '验证本机节拍网络与计算设备'),
    ('[cubase_local] 计算44.1kHz', '提取多尺度节拍特征'),
    ('[cubase_local] 使用 ', '运行本机节拍网络'),
    ('[cubase_local] 从三类观测', '解码拍点、强拍与拍号'),
    ('[cubase_local] 复用已验证三类观测', '复用节拍观测，跳过模型推理'),
    ('[cubase_local] 复用当前参数', '复用节拍解码结果'),
    ('[duet] 本地记谱：', '加载 DuET 节奏与记谱模型'),
    ('[duet] 按项目速度图整理乐谱', '整理乐谱'),
    ('[harmony] 分析钢琴调性和和弦', '分析和声'),
    ('[basic_pitch] Basic Pitch 首个 CUDA 窗口', '初始化 Basic Pitch 的首个 CUDA 窗口'),
)
_WINDOW_LABELS = {'separator': '分轨窗口', 'duet': 'DuET 节奏与记谱模型窗口', 'transkun': '钢琴转录窗口'}


class JobProgress:
    def __init__(self, clock=monotonic):
        self._clock = clock
        self._started = None
        self._reset_stage()

    def start(self):
        self._started = self._clock()
        self._reset_stage()

    def finish(self):
        self._started = None
        self._reset_stage()

    def _reset_stage(self):
        self.detail = ''
        self._waiting_first_separator_window = False
        self._unit = None
        self._done = self._total = 0
        self._sample_time = self._last_update = None
        self._rates = deque(maxlen=3)

    def stage_changed(self):
        self._reset_stage()

    def observe(self, line):
        if self._started is None:
            return
        if line.startswith((WAITING_PREFIX, RESUMED_PREFIX)):
            self._reset_stage()
            self.detail = line.split("] ", 1)[1]
            return
        for prefix, detail in _DETAILS:
            if line.startswith(prefix):
                self._reset_stage()
                self.detail = detail
                return
        if line.startswith('[separator] ') and ' - Loading model ' in line:
            self._reset_stage()
            self.detail = '加载分轨模型'
            return
        separator_start = _SEPARATOR_START.fullmatch(line)
        if separator_start:
            self._reset_stage()
            track = separator_start.group(1)
            self._waiting_first_separator_window = True
            self.detail = '正在准备钢琴分轨' if track == 'piano' else '正在准备音轨分离'
            return
        if line == '[separator] [gpu-graph] miss' and self._waiting_first_separator_window:
            self.detail = '首次准备 GPU 分轨，稍等一下'
            return
        separator = _SEPARATOR_WINDOW.fullmatch(line)
        if separator:
            done, total = (int(value) for value in separator.groups()[:2])
            if self._waiting_first_separator_window and done == 0 and total > 0:
                # A count of 0/total does not distinguish GPU preparation from a
                # stalled job. Only show measured window progress after one returns.
                return
            speed = _RATE.fullmatch(separator[3])
            seconds_per_window = None
            if speed:
                rate = float(speed[1])
                if rate > 0 and math.isfinite(rate):
                    seconds_per_window = rate if speed[2] == 's/it' else 1 / rate
            self._window('separator', done, total, rate=seconds_per_window)
            return
        unit = next((name for name in ('duet','transkun') if line.startswith(f'[{name}] {{')),None)
        if unit is None:
            return
        try:
            event = json.loads(line.removeprefix(f'[{unit}] '))
            done = event['completed_windows']
            total = event['total_windows']
            timestamp = event['elapsed_seconds']
        except (KeyError, TypeError, ValueError):
            return
        if (type(done) is int and type(total) is int and type(timestamp) in (int, float)
                and math.isfinite(timestamp) and timestamp >= 0):
            self._window(unit, done, total, timestamp=timestamp)

    def _window(self, unit, done, total, *, timestamp=None, rate=None):
        if not 0 <= done <= total or not total:
            return
        if self._unit != unit or self._total != total or (done == 0 < self._done):
            self._reset_stage()
            self._unit, self._total = unit, total
            self.detail = _WINDOW_LABELS[unit]
        if done < self._done or (done == self._done and self._last_update is not None):
            return
        # Ignore the first completed window when estimating runtime. Its
        # measurements may include CUDA warm-up even after the model is loaded.
        if self._done >= 1 and done > self._done:
            if timestamp is not None and self._sample_time is not None:
                rate = (timestamp - self._sample_time) / (done - self._done)
            if rate and rate > 0 and math.isfinite(rate):
                self._rates.append(rate)
            else:
                self._rates.clear()
        self._done, self._sample_time = done, timestamp
        self._last_update = self._clock()

    def snapshot(self):
        if self._started is None:
            return None
        now = self._clock()
        result = {'elapsed': int(now - self._started), 'detail': self.detail}
        if self._unit is None:
            return result
        result.update(done=self._done, total=self._total, unit='窗')
        if (0 < self._done < self._total and len(self._rates) >= 3
                and max(self._rates) <= min(self._rates) * 2):
            rate = median(self._rates)
            # Do not keep showing a stale estimate if the next window stalls.
            if now - self._last_update <= max(3, 2.5 * rate):
                result['remaining'] = math.ceil((self._total - self._done) * rate)
        return result
