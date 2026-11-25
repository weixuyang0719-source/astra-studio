"""Prototype-only transport to a single official-source MuseScore document owner."""
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4
from xml.etree import ElementTree as ET
import base64
import binascii
import json
import math
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QRunnable, QThreadPool, QTimer, Signal, Slot
from shiboken6 import isValid

from .. import native_score_runtime as native
from ..release_paths import data_root
from ..score_artifacts import current_score_export, resolve_score_export
from ..pipeline import read_json
from ..score_commit import native_path
from ..score_opening_projection import project_score_opening


class _CompareAlignTask(QRunnable):
    def __init__(self, seq, signal, work):
        super().__init__()
        self.setAutoDelete(True)
        self._seq = seq
        self._signal = signal
        self._work = work

    def run(self):
        try:
            plan = self._work()
            self._signal.emit(self._seq, plan, '')
        except Exception as exc:
            self._signal.emit(self._seq, None, str(exc))


def _compute_core_bands(rows, start_tick, end_tick, logical_height):
    matched = []
    for r in rows or ():
        if not isinstance(r, list) or len(r) < 7:
            continue
        a, b, x0, x1, y, _w, h = r[:7]
        if b <= start_tick or a >= end_tick or h <= 0:
            continue
        matched.append([float(x0), float(x1), float(y), float(y + h)])
    if not matched:
        return []
    matched.sort(key=lambda t: (t[2], t[0]))
    groups = []
    for x0, x1, y0, y1 in matched:
        placed = False
        for g in groups:
            if not (y0 > g[3] + 14 or y1 < g[2] - 14):
                g[0] = min(g[0], x0)
                g[1] = max(g[1], x1)
                g[2] = min(g[2], y0)
                g[3] = max(g[3], y1)
                placed = True
                break
        if not placed:
            groups.append([x0, x1, y0, y1])
    lh = float(logical_height or 1358)
    return [{
        'x0': max(10.0, g[0] - 7.0),
        'x1': min(950.0, g[1] + 7.0),
        'y0': max(6.0, g[2] - 10.0),
        'y1': min(lh - 6.0, g[3] + 10.0),
    } for g in groups]


_STRUCTURAL_CONTENT_ACTIONS = frozenset({
    'measure-add-before', 'measure-add-after', 'measure-add-start', 'measure-add-end',
    'measure-split', 'measure-join', 'time-delete', 'remove-empty-tail',
    'measure-properties', 'measure-properties-step',
})
_STRUCTURAL_TOOLS = frozenset({'meter-rebar', 'meter', 'time-signature'})


def _clock_digest(tempo):
    from ..score_range import _clock_content
    if not isinstance(tempo, dict):
        return None
    try:
        return sha256(json.dumps(_clock_content(tempo), sort_keys=True, ensure_ascii=True,
                                 separators=(',', ':')).encode('utf-8')).hexdigest()
    except (TypeError, ValueError):
        return None


def _xml_measure_bounds(xml_bytes):
    from ..score_tempo import _measure_layout
    try:
        root = ET.fromstring(xml_bytes)
        parts = root.findall('part')
        if not parts:
            return None
        first_part = parts[0]
        rows = [row for row in _measure_layout(root) if row[0] is first_part]
        if not rows:
            return None
        return [(idx + 1, float(start), float(start + length)) for idx, (_, _, start, length, _) in enumerate(rows)]
    except (ET.ParseError, ValueError, TypeError):
        return None


def _is_structural_command(kind, body):
    if kind == 'content' and body.get('action') in _STRUCTURAL_CONTENT_ACTIONS:
        return True
    if kind == 'tool' and body.get('tool') in _STRUCTURAL_TOOLS:
        return True
    if kind == 'palette-apply':
        raw = json.dumps(body, ensure_ascii=False).lower()
        if any(tok in raw for tok in ('timesig', 'time_sig', 'time-sig', 'measure')):
            return True
    return False


class InlineNativeScoreBridge(QObject):
    response = Signal(str)
    saveStateChanged = Signal(str)
    shadowResponse = Signal(str)
    cursorLayoutResponse = Signal(str)
    compareExcerptReady = Signal(str)
    _compareAlignDone = Signal(int, object, str)
    playbackRevisionChanged = Signal(str, int)
    playbackOwnerClosed = Signal(str)
    _prefix = native.FRAME_PREFIX
    _max_frame = native.MAX_FRAME

    def __init__(self, owner, parent):
        super().__init__(parent)
        self.owner = owner
        self.process = None
        self._load_revision = None
        self._seed_source = None
        self.project = None
        self._external_work = None
        self._generated_work = None
        self._buf = bytearray()
        self._scan = 0
        self._busy = None
        self._active = 0
        self._shadow_request = None
        self._shadow_serial = 0
        self._cursor_request = None
        self._cursor_serial = 0
        self._native_revision = None
        # Unsaved-work guard: the worker reports `dirty`; an untouched generated
        # seed is also "dirty" (never written to piano.mscz) but holds no user
        # work, so user edits are revisions past the loaded one.
        self._dirty = False
        self._host_save = None
        self._host_save_serial = 0
        self._save_request = None
        self._project_save_queued = None
        self._score_text_editing = False
        self._save_error = None
        self._save_receipt = 0
        self._published_save_state = None
        self._playback_request = None
        self._playback_serial = 0
        self._playback_page = None
        self._compare_seq = 0
        self._compare_cmd_serial = 0
        self._compare_req = None
        self._compare_worker_req = None
        self._compare_paused_editing = False
        self._compare_saved_page = None
        self._compare_restore_pending = False
        self._compare_touched_worker = False
        self._pending_main_open = None
        self._compare_snapshot_cache = None
        self._compare_vector_cache = OrderedDict()
        self._compare_cursor_cache = OrderedDict()
        self._compare_job = None
        self._compare_timer = QTimer(self)
        self._compare_timer.setSingleShot(True)
        self._compare_timer.timeout.connect(self._advance_compare)
        self._compareAlignDone.connect(self._on_compare_align_done)
        self._web_score_document_key = None
        self._compare_main_frame = None
        self._score_origin = None
        self._score_document_key = None
        self._worker_editing = False
        self._worker_page = 1
        self._edit_structural_stack = []
        self._redo_structural_stack = []
        self._pending_cmd_meta = None
        self.playbackRevisionChanged.connect(self._playback_changed)
        self.playbackOwnerClosed.connect(self._playback_closed)
        self._exe, self._qt = native.runtime()

    def _send(self, body):
        self.response.emit(json.dumps({**body, 'project': self.project, 'readOnly': self._external_work is not None}, ensure_ascii=False))

    def _emit_save_state(self):
        # Save state belongs to this native document even when the Notes page
        # temporarily owns the web surface. Never infer it from a Notes save.
        ready = self._native_revision is not None
        status = dict(project=self.project, ready=ready, readOnly=self._external_work is not None,
                      dirty=bool(self._dirty and (self._load_revision is None or
                                                   self._native_revision != self._load_revision)),
                      revision=self._native_revision, needsSave=self._dirty,
                      saving=self._save_request is not None or self._project_save_queued is not None,
                      error=self._save_error, receipt=self._save_receipt)
        if status != self._published_save_state:
            self._published_save_state = status
            self.saveStateChanged.emit(json.dumps(status, ensure_ascii=False))

    def _finish_score_save(self, error=None):
        self._save_request = None
        self._save_error = error
        if error is None:
            self._save_receipt += 1
        self._emit_save_state()

    def _current(self, path):
        actual = (self.owner._project or {}).get('path')
        if path != actual or not actual:
            raise ValueError('当前工程已切换。')
        from astra import project_access
        permit=project_access.approved(actual)
        root=permit.validate() if permit is not None else Path(actual).resolve(strict=True)
        if permit is not None:
            permit.file(root/'native-notation-prototype',directory=True,missing=True)
            permit.file(root/'native-notation-prototype'/'piano.mscz',missing=True)
        if not root.is_dir():
            raise ValueError('此验证版只打开自身隔离目录中的测试工程。')
        return root

    def _committed_seed(self, root, state=None, *, compare_mode=False):
        if state is None:
            state = read_json(root / 'project.json')
        editor = (self.owner._project or {}).get('editor') or None
        if compare_mode:
            exact = resolve_score_export(root,state,(editor or {}).get('source_key'))
        else:
            if editor and editor.get('dirty'):
                return None
            exact = current_score_export(root, state, editor)
        if exact:
            seed = exact / 'piano.musicxml'
            if (not seed.is_symlink() and seed.is_file() and seed.resolve().parent == exact
                    and 0 < seed.stat().st_size <= 64 * 1024 * 1024):
                return seed.resolve()
        return None

    def _seed(self, root):
        state = read_json(root / 'project.json')
        editor = (self.owner._project or {}).get('editor') or {}
        exact = self._committed_seed(root, state)
        if exact:
            return exact
        # For new/dirty audio transcriptions, or dirty MIDI projects, use only
        # the published preview of the *current* performance edit. No overwrite,
        # legacy PDF dependency, stale prior MIDI, or write/checkpoint is needed.
        if state.get('source_kind') != 'midi' or editor.get('dirty'):
            candidate = editor.get('preview_path')
            if (not editor.get('revision') or editor.get('preview_pending') or editor.get('preview_error')
                    or editor.get('preview_revision') != editor['revision'] or not candidate):
                raise ValueError('当前编辑版本的钢琴 MIDI 还未准备好，暂不使用旧谱或旧 MIDI。')
            p, expected = Path(candidate), root / 'editing' / 'previews'
            if (not p.is_symlink() and p.is_file() and p.resolve().parent == expected
                    and p.suffix.lower() == '.mid' and 0 < p.stat().st_size <= 64 * 1024 * 1024):
                return p.resolve()
            raise ValueError('无法验证当前编辑版本的钢琴 MIDI 来源。')
        return native.selected_midi(root, state)

    @staticmethod
    def _generated_score_bytes(source, *, hashes=None):
        """Use layout time for generated scores, never for the user's MSCZ."""
        with source.open('rb') as stream:
            raw = stream.read(64 * 1024 * 1024 + 1)
        if not 0 < len(raw) <= 64 * 1024 * 1024:
            raise ValueError('MusicXML size is out of bounds')
        from hashlib import sha256
        if hashes is not None and sha256(raw).hexdigest() != hashes.get(source.name):
            raise ValueError('生成谱面文件已变化。')
        from ..score_midi_seed import project_free_barlines
        if b'astra-source-leading-silence' not in raw:
            return project_free_barlines(raw)
        tempo = source.with_name('tempo_map.json')
        if (tempo.is_symlink() or not tempo.is_file() or tempo.resolve().parent != source.parent
                or not 0 < tempo.stat().st_size <= 16 * 1024 * 1024):
            raise ValueError('Confirmed start requires the exact generated score reference tempo')
        with tempo.open('rb') as stream:
            tempo_data = stream.read(16 * 1024 * 1024 + 1)
        if not 0 < len(tempo_data) <= 16 * 1024 * 1024:
            raise ValueError('Generated score reference tempo size is out of bounds')
        if hashes is not None and sha256(tempo_data).hexdigest() != hashes.get(tempo.name):
            raise ValueError('制谱速度图已变化。')
        reference = json.loads(tempo_data)
        result, _opening = project_score_opening(raw, reference)
        return project_free_barlines(result)

    def _compare_origin(self, source, projected):
        # Exact provenance only for the immutable XML actually imported by this
        # worker, not a mutable MSCZ (including a saved generated MSCZ).
        tempo = source.with_name('tempo_map.json')
        try:
            if (tempo.is_symlink() or tempo.resolve().parent!=source.parent
                    or not 0 < tempo.stat().st_size <= 16*1024*1024):
                return None
            if source.is_symlink() or source.stat().st_size>4*1024*1024:
                return None
            reference=read_json(tempo)
            with source.open('rb') as stream:
                raw=stream.read(4*1024*1024+1)
            if not 0<len(raw)<=4*1024*1024:
                return None
            from ..score_midi_seed import project_free_barlines
            xml,opening=(project_score_opening(raw,reference) if b'astra-source-leading-silence' in raw
                         else (raw,None))
            if project_free_barlines(xml)!=projected:
                return None
            bounds = _xml_measure_bounds(projected)
            return dict(kind='committed_xml',opening_beat=float(opening.beat) if opening else 0.0,
                        opening_seconds=float(opening.seconds) if opening else 0.0,
                        clock_digest=_clock_digest(reference),
                        # The original score's own beat map: both sides share
                        # the audio timeline, so seconds locate its bars exactly.
                        reference_clock=reference,
                        score_end_beat=bounds[-1][2] if bounds else None)
        except (OSError, ValueError, TypeError):
            return None

    def _close_process(self):
        proc, self.process = self.process, None
        self._compare_drop_request()
        self._compare_reset_worker()
        self._compare_timer.stop()
        self._load_revision = None
        self._seed_source = None
        self._buf = bytearray()
        self._scan = 0
        self._busy = None
        self._shadow_request = None
        self._finish_cursor_layout()
        self._native_revision = None
        self._dirty = False
        self._save_request = None
        self._project_save_queued = None
        self._score_text_editing = False
        self._save_error = None
        host_save, self._host_save = self._host_save, None
        if host_save is not None:
            host_save[2]('右侧谱面已关闭，未能确认保存。')
        self._playback_selection = None
        self._finish_playback(None, 'Score document closed')
        if proc is not None:
            self.playbackOwnerClosed.emit(self.project or '')
            if proc.state() != QProcess.ProcessState.NotRunning:
                proc.write(b'{"kind":"quit"}\n')
                proc.closeWriteChannel()
                if not proc.waitForFinished(600):
                    proc.kill()
                    proc.waitForFinished(500)
            proc.deleteLater()
        work, self._external_work = self._external_work, None
        if work is not None:
            work.cleanup()
        generated, self._generated_work = self._generated_work, None
        if generated is not None:
            generated.cleanup()
        self._emit_save_state()

    def close(self):
        self._close_process()

    def unsaved_edits(self):
        """True only for user work the worker has not written to piano.mscz."""
        proc = self.process
        if (proc is None or proc.state() == QProcess.ProcessState.NotRunning
                or self._external_work is not None or not self.project):
            return False
        if self._host_save is not None:
            return True
        revision = self._native_revision
        if not self._dirty:
            return False
        # A fresh generated seed is reported dirty before any edit. Only a
        # revision past the loaded document is the user's own work.
        return self._load_revision is None or revision != self._load_revision

    def unsaved_project(self):
        return self.project if self.unsaved_edits() else None

    def close_guard_pending(self):
        proc = self.process
        return bool(proc is not None and proc.state() != QProcess.ProcessState.NotRunning
                    and (self._busy is not None or self._host_save is not None or self._project_save_queued is not None))

    def close_guard_identity(self):
        return self.process, self.project, self._native_revision

    def save_then(self, done, failed):
        """Write the open native document to its project file, then continue."""
        proc = self.process
        if proc is None or proc.state() != QProcess.ProcessState.Running or not self.project:
            failed('右侧谱面没有可保存的文档。')
            return False
        if self._busy is not None or self._host_save is not None:
            failed('右侧谱面正在处理上一项操作，请稍后再试。')
            return False
        self._host_save_serial = self._host_save_serial % 1_000_000 + 1
        request = 1_900_000_000 + self._host_save_serial
        flags = json.dumps({'kind': 'save', 'request': request, 'baseRevision': self._native_revision or 0},
                           ensure_ascii=True, separators=(',', ':')).encode('ascii')
        self._busy = request
        self._host_save = (request, done, failed)
        self._save_request = request
        self._save_error = None
        if proc.write(flags + b'\n') == -1:
            self._busy = None
            self._host_save = None
            message = '原生谱面进程未接收保存命令。'
            self._finish_score_save(message)
            failed(message)
            return False
        self._emit_save_state()
        return True

    @Slot(str, result=str)
    def requestProjectScoreSave(self, path):
        # The currently open notation owner remains authoritative across the
        # Notes/Tracks/Files tabs, including while its DOM is detached.
        try:
            self._current(path)
        except (OSError, ValueError, RuntimeError) as exc:
            return json.dumps(dict(ok=False,error=str(exc)), ensure_ascii=False)
        if path != self.project or self._external_work is not None:
            return json.dumps(dict(ok=True,status='not_open'))
        proc = self.process
        if proc is None or proc.state() == QProcess.ProcessState.NotRunning:
            if self._dirty or self._save_error:
                return json.dumps(dict(ok=False,error=self._save_error or '原生谱面已关闭，未能确认保存。'),
                                  ensure_ascii=False)
            return json.dumps(dict(ok=True,status='not_open'))
        if self._save_request is not None or self._project_save_queued is not None:
            return json.dumps(dict(ok=True,status='queued'))
        if not self._dirty and self._busy is None:
            self._save_error = None
            self._emit_save_state()
            return json.dumps(dict(ok=True,status='already_saved'))
        self._project_save_queued = (proc,path)
        self._save_error = None
        self._flush_project_score_save()
        self._emit_save_state()
        return json.dumps(dict(ok=self._save_error is None,status='queued',error=self._save_error),ensure_ascii=False)

    def _flush_project_score_save(self):
        wanted = self._project_save_queued
        if wanted is None or self._busy is not None or self._host_save is not None:
            return
        self._project_save_queued = None
        proc,path = wanted
        if proc is not self.process or path != self.project or path != (self.owner._project or {}).get('path'):
            self._emit_save_state()
            return
        if self._score_text_editing:
            self._save_error = '请先完成谱面文字编辑再保存。'
            self._emit_save_state()
            return
        if not self._dirty:
            self._emit_save_state()
            return
        if self._native_revision is None:
            self._save_error = '原生谱面尚未确认，未能保存。'
            self._emit_save_state()
            return
        self.save_then(lambda: None, lambda _error: None)

    def _read(self, proc):
        if not isValid(self) or not isValid(proc) or proc is not self.process:
            return
        # Append in place and scan only the new bytes for the line end: a page
        # frame arrives in many pipe chunks, and re-copying and re-scanning the
        # whole buffer for every chunk was quadratic in the frame size.
        self._buf += proc.readAllStandardOutput().data()
        if len(self._buf) > 2 * self._max_frame:
            self._close_process()
            self._send({'request': self._active, 'error': '原生画面数据超出大小限制。'})
            return
        while True:
            end = self._buf.find(b'\n', self._scan)
            if end < 0:
                self._scan = len(self._buf)
                break
            line = bytes(self._buf[:end])
            del self._buf[:end + 1]
            self._scan = 0
            if not line.startswith(self._prefix):
                continue
            try:
                row = json.loads(line[len(self._prefix):])
                if not isinstance(row, dict):
                    continue
            except ValueError:
                continue
            cursor = self._cursor_request
            if cursor is not None and row.get('request') == cursor[0]:
                self._finish_cursor_layout(row)
                continue
            shadow = self._shadow_request
            if shadow is not None and row.get('request') == shadow[0]:
                self._shadow_request = None
                self.shadowResponse.emit(json.dumps({**row, 'project': self.project, 'receipt': shadow[1]}))
                continue
            if row.get('request') == 0:
                row['request'] = self._active
            if row.get('request') == self._busy:
                pending, self._busy = self._busy, None
                if self._playback_request is not None and self._playback_request[0] == pending:
                    self._accept_playback(proc, row)
                    self._flush_project_score_save()
                    continue
                revision = row.get('revision')
                before = self._native_revision
                if not row.get('error'):
                    if type(row.get('editing')) is bool:
                        self._worker_editing = row['editing']
                    if type(row.get('page')) is int:
                        self._worker_page = row['page']
                cmd = self._pending_cmd_meta
                self._pending_cmd_meta = None
                if self._compare_worker_req is not None and self._compare_worker_req[0] == pending:
                    _, kind, metadata = self._compare_worker_req
                    self._compare_worker_req = None
                    if kind == 'open' and type(revision) is int and not row.get('error'):
                        self._load_revision = revision
                        self._native_revision = revision
                        self.playbackRevisionChanged.emit(self.project or '',revision)
                    if type(row.get('dirty')) is bool and self._native_revision is not None and revision == self._native_revision:
                        self._dirty = row['dirty']
                    self._emit_save_state()
                    self._flush_project_score_save()
                    self._accept_compare_worker(kind,metadata,row)
                    continue
                compare_main=self._compare_main_frame==pending
                if compare_main:
                    self._compare_main_frame=None
                if type(revision) is int and not row.get('error'):
                    if pending==self._active:
                        self._load_revision=revision
                        row['documentSwitch']=True
                        self._web_score_document_key=self._score_document_key
                    elif compare_main and self._web_score_document_key!=self._score_document_key:
                        # A read-only drawer may have first loaded a published
                        # replacement invisibly, whose revision resets to 1.
                        row['documentSwitch']=True
                        self._web_score_document_key=self._score_document_key
                if type(revision) is int and revision > 0 and revision != self._native_revision:
                    self._native_revision = revision
                    if cmd and cmd[0] == pending and before is not None and not row.get('error') and pending != self._active:
                        kind, structural = cmd[1:]
                        if kind == 'undo':
                            if self._edit_structural_stack:
                                self._redo_structural_stack.append(self._edit_structural_stack.pop())
                        elif kind == 'redo':
                            if self._redo_structural_stack:
                                self._edit_structural_stack.append(self._redo_structural_stack.pop())
                        elif kind == 'reopen':
                            self._edit_structural_stack.clear()
                            self._redo_structural_stack.clear()
                            self._score_origin = None
                        elif kind != 'save':
                            self._edit_structural_stack.append(bool(structural))
                            self._redo_structural_stack.clear()
                    self.playbackRevisionChanged.emit(self.project or '', revision)
                if type(row.get('dirty')) is bool and type(revision) is int and revision == self._native_revision:
                    self._dirty = row['dirty']
                if type(row.get('textEditing')) is bool:
                    self._score_text_editing = row['textEditing']
                host_save = self._host_save
                if host_save is not None and host_save[0] == pending:
                    self._host_save = None
                else:
                    host_save = None
                if self._save_request == pending:
                    # A process reply is not sufficient: it must acknowledge
                    # the native saved revision and no remaining dirty edits.
                    error = row.get('error')
                    if not error and (self._dirty or type(revision) is not int or row.get('saved') != revision):
                        error = '右侧谱面仍有未保存的更改。'
                    if error:
                        row['error'] = str(error)
                    self._finish_score_save(str(error) if error else None)
                else:
                    self._emit_save_state()
                if host_save is not None:
                    if row.get('error'):
                        host_save[2](str(row['error']))
                    else:
                        host_save[1]()
                    self._flush_project_score_save()
                    continue
                if 'playbackSelection' in row and revision == self._native_revision:
                    self._playback_selection = row['playbackSelection']
                self._send(row)
                self._flush_project_score_save()
                if self._compare_restore_pending or self._compare_req:
                    self._schedule_compare()

    def _failed(self, proc):
        # A late native Qt notification can race the channel destructor.
        if not isValid(self) or not isValid(proc):
            return
        if proc is self.process:
            self._finish_cursor_layout()
        if proc is self.process and self._shadow_request is not None:
            shadow, self._shadow_request = self._shadow_request, None
            self.shadowResponse.emit(json.dumps({'project': self.project, 'receipt': shadow[1]}))
        if proc is self.process and (self._native_revision is not None or self._busy is not None):
            self._native_revision = None
            self._playback_selection = None
            if self._save_request is not None or self._project_save_queued is not None:
                self._project_save_queued = None
                self._finish_score_save('原生制谱进程退出，右侧谱面未能保存。')
            else:
                if self._dirty:
                    self._save_error = '原生制谱进程退出，未保存的谱面不能确认。'
                self._emit_save_state()
            self.playbackOwnerClosed.emit(self.project or '')
            if self._busy is None:
                return
            pending, self._busy = self._busy, None
            if self._compare_worker_req is not None and self._compare_worker_req[0] == pending:
                self._compare_worker_req = None
                self._compare_error('原生制谱进程退出，未能读取原谱片段。')
                return
            if self._playback_request is not None and self._playback_request[0] == pending:
                self._finish_playback(None, 'Native score worker exited')
                return
            host_save = self._host_save
            if host_save is not None and host_save[0] == pending:
                self._host_save = None
                host_save[2]('原生制谱进程退出，右侧谱面未能保存。')
                return
            self._send({'request': pending, 'error': '原生制谱进程退出，未修改原始 MIDI。'})

    def _start(self, root, path, n, committed_only=False, raster_scale=1, *, compare_mode=False):
        from astra import project_access
        profile = data_root().resolve()
        temp = None
        if not root.is_relative_to(profile) and project_access.approved(root) is None:
            source = self._committed_seed(root,compare_mode=compare_mode)
            if source is None:
                if committed_only:
                    return False
                raise ValueError('此工程没有可核实的已保存谱面。先在原工程生谱，或打开隔离副本。')
            # The worker receives only the staged artifact, never the project's
            # real path. All its potential outputs are confined to this session.
            temp = TemporaryDirectory(prefix='native-score-read-', dir=profile)
            try:
                folder = Path(temp.name)
                staged = folder / 'score.musicxml'
                generated = self._generated_score_bytes(source)
                staged.write_bytes(generated)
                return self._launch(path, n, staged, folder / 'piano.mscz', temp, raster_scale,
                                    origin_info=self._compare_origin(source,generated))
            except BaseException:
                temp.cleanup()
                raise
        return self._start_local(root, path, n, committed_only, raster_scale, compare_mode=compare_mode)

    def _published_native(self, root):
        state = read_json(root/'project.json')
        view = self.owner._project or {}
        source = (view.get('editor') or {}).get('source_key') or view.get('piano_analysis_key')
        return native_path(root, state, source)

    def _start_local(self, root, path, n, committed_only, raster_scale=1, *, compare_mode=False):
        regenerated = self._published_native(root)
        if regenerated is not None:
            return self._launch(path, n, regenerated, regenerated, raster_scale=raster_scale,
                                seed_source=regenerated)
        work = root / 'native-notation-prototype'
        if work.is_symlink() or (work.exists() and (work.resolve(strict=True) != work or not work.is_dir())):
            raise ValueError('测试谱面目录无效。')
        output = work / 'piano.mscz'
        if output.is_symlink() or (output.exists() and (not output.is_file() or output.stat().st_nlink != 1)):
            raise ValueError('只允许读写工程自己的普通谱面文件。')
        source = output if output.exists() else (self._committed_seed(root,compare_mode=compare_mode)
                                                  if committed_only else self._seed(root))
        if source is None:
            return False
        if source.suffix.lower() != '.musicxml':
            from ..score_midi_seed import needs_seed_projection, project_midi_seed
            view = self.owner._project or {}
            reference = (view.get('editor') or {}).get('tempo') or view.get('tempo')
            if source == output or not needs_seed_projection(reference):
                return self._launch(path, n, source, output, raster_scale=raster_scale, seed_source=source)
            generated = TemporaryDirectory(prefix='native-score-seed-', dir=data_root())
            try:
                staged = Path(generated.name) / 'score.musicxml'
                project_midi_seed(source, reference, staged)
                return self._launch(path, n, staged, output, raster_scale=raster_scale,
                                    generated=generated, seed_source=source)
            except BaseException:
                generated.cleanup()
                raise
        # Source is the exact revision from _committed_seed. Project the view
        # before first import, and leave any existing native work untouched.
        generated = TemporaryDirectory(prefix='native-score-seed-', dir=data_root())
        try:
            staged = Path(generated.name) / 'score.musicxml'
            rendered = self._generated_score_bytes(source)
            staged.write_bytes(rendered)
            return self._launch(path, n, staged, output, raster_scale=raster_scale,
                                generated=generated, seed_source=source,
                                origin_info=self._compare_origin(source,rendered))
        except BaseException:
            generated.cleanup()
            raise

    def _can_refresh_generated(self, root):
        """A new committed export may replace only an untouched, unsaved seed."""
        output = root / 'native-notation-prototype' / 'piano.mscz'
        if (self._external_work is not None or output.exists() or output.is_symlink()
                or self._load_revision is None or self._native_revision != self._load_revision
                or self._busy is not None):
            return False
        incoming = self._committed_seed(root)
        return incoming is not None and incoming != self._seed_source

    def _launch(self, path, n, source, output, temp=None, raster_scale=1, generated=None, seed_source=None, *, origin_info=None):
        self._close_process()
        self.project = path
        self._external_work = temp
        self._generated_work = generated
        self._seed_source = seed_source
        self._score_origin = origin_info
        self._score_document_key = uuid4().hex
        proc = QProcess(self)
        proc.setProgram(str(self._exe))
        proc.setArguments(['-o', str(output), str(source)])
        proc.setWorkingDirectory(str(self._exe.parent))
        env = QProcessEnvironment()
        for key, value in native.environment(self._qt).items():
            env.insert(key, value)
        env.insert("ASTRA_SCORE_INITIAL_RASTER_SCALE", str(raster_scale))
        env.insert("ASTRA_SCORE_FRAME_BANDS", "1")  # page reassembles parallel-encoded bands
        proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(lambda: self._read(proc))
        proc.readyReadStandardError.connect(proc.readAllStandardError)
        proc.errorOccurred.connect(lambda _e: self._failed(proc))
        proc.finished.connect(lambda *_a: self._failed(proc))
        self.process, self._active, self._busy = proc, n, n
        self._emit_save_state()
        proc.start()
        return True

    def _finish_playback(self, snapshot, error):
        pending, self._playback_request = self._playback_request, None
        if pending is not None:
            pending[-1](snapshot, error)

    def _accept_playback(self, proc, row):
        _request, project, original, revision, _reply = self._playback_request
        active = (self.owner._project or {}).get('path')
        try:
            if (proc is not original or self.process is not original or project != active
                    or self.project != project or self._native_revision != revision):
                raise ValueError('Score playback source changed')
            if row.get('error'):
                raise ValueError(str(row['error']))
            encoded = row.get('playbackMidi')
            if row.get('revision') != revision or not isinstance(encoded, str) or len(encoded) > 956000:
                raise ValueError('Invalid native score playback revision')
            data = base64.b64decode(encoded, validate=True)
            if not 14 < len(data) <= 700 * 1024 or not data.startswith(b'MThd'):
                raise ValueError('Invalid native score playback packet')
        except (ValueError, binascii.Error) as err:
            self._finish_playback(None, str(err))
            return
        # Retain immutable bytes with owner/revision, never expose to web or disk.
        self._finish_playback((project, revision, data, row.get('playbackBarMap')), None)

    def request_playback_snapshot(self, path, reply):
        """Trusted owner-thread-only path. The caller controls cancellable intent."""
        from astra import project_access
        root = self._current(path)
        if self._external_work is not None or (not root.is_relative_to(data_root().resolve()) and project_access.approved(root) is None):
            raise ValueError('External project score is read only')
        proc, revision = self.process, self._native_revision
        if (self.project != path or not proc or not revision
                or proc.state() != QProcess.ProcessState.Running or self._busy is not None):
            raise ValueError('Native score is not ready for playback')
        self._playback_serial += 1
        if self._playback_serial >= 100_000_000:
            raise ValueError('Native score request limit')
        request = 2_000_000_000 + self._playback_serial
        self._playback_request = (request, path, proc, revision, reply)
        self._busy = request
        packet = json.dumps(dict(kind='playbackSnapshot', request=request, baseRevision=revision)).encode('ascii')
        if proc.write(packet + b'\n') == -1:
            self._busy = None
            self._finish_playback(None, 'Native score worker rejected playback request')

    def _playback_changed(self, path, revision):
        if self._playback_page == path:
            changed=getattr(self.owner,'score_transport_revision',None)
            if changed:changed(revision)

    def _playback_closed(self, path):
        if self._playback_page == path:
            leave=getattr(self.owner,'leave_score_transport',None)
            if leave:leave()

    @Slot(str, bool)
    def setPlaybackPage(self, path, enabled):
        if not enabled:
            held=getattr(self.owner,'_hold_score_for_files',None)
            if self._playback_page==path and held and held(path):
                self._playback_page=None
                return
            self._playback_closed(path)
            if self._playback_page == path:self._playback_page=None
            return
        if path != (self.owner._project or {}).get('path'):
            return
        try:
            from astra import project_access
            root=self._current(path)
            if not root.is_relative_to(data_root().resolve()) and project_access.approved(root) is None:
                return
        except (OSError, ValueError, TypeError):
            return
        if self._playback_page is not None and self._playback_page != path:
            self._playback_closed(self._playback_page)
        self._playback_page=path
        enter=getattr(self.owner,'enter_score_transport',None)
        if enter:enter(path, self)

    @Slot(str, float, float, bool)
    def setScoreLoop(self, path, start, end, active):
        if path == self._playback_page and path == (self.owner._project or {}).get('path'):
            action=getattr(self.owner,'score_transport_loop',None)
            if action:action(self,start,end,active)

    @Slot(str, str, float)
    def controlPlayback(self, path, kind, position):
        if (path == self._playback_page and path == (self.owner._project or {}).get('path')
                and kind in ('seek','stop')):
            action=getattr(self.owner,'score_transport_action',None)
            if action:action(self,kind,position)

    def _after_native_command(self, call, tries):
        """A transport press during a short native edit waits for it (<=2 s)
        instead of being refused: the score's play needs the idle worker."""
        if self._busy is None or self.process is None or tries >= 125:
            return False
        QTimer.singleShot(16, self, lambda: call(tries + 1))
        return True

    @Slot(str)
    def controlScorePlayback(self, payload, _tries=0):
        if not isinstance(payload,str) or len(payload)>2048:
            return
        if self._after_native_command(lambda n: self.controlScorePlayback(payload, n), _tries):
            return
        try:
            msg=json.loads(payload)
        except ValueError:
            return
        if not isinstance(msg,dict):
            return
        path,revision,action,value=(msg.get(k) for k in ('project','revision','action','value'))
        if (not path or path != self._playback_page or path != self.project
                or path != (self.owner._project or {}).get('path') or self._external_work is not None
                or type(revision) is not int or revision != self._native_revision
                or not isinstance(value,dict)
                or action not in ('playPause','pause','stop','rewind','seek','seekSelection','seekTick','speed','loop','loopIn','loopOut','metronome','countIn','seekBeat','flatTempo')):
            return
        if self.owner.enter_score_transport(path, self):
            if action=='playPause':
                self.owner.toggle_score_transport(self)
            else:
                self.owner.score_transport_action(self,action,value)

    @Slot(str)
    def togglePlayback(self, path, _tries=0):
        if self._after_native_command(lambda n: self.togglePlayback(path, n), _tries):
            return
        if path != self._playback_page or path != (self.owner._project or {}).get('path'):
            return
        enter=getattr(self.owner,'enter_score_transport',None)
        if enter and enter(path, self):
            self.owner.toggle_score_transport(self)

    def _finish_cursor_layout(self, row=None):
        pending,self._cursor_request=self._cursor_request,None
        if pending is None:
            return
        _request,receipt,project,revision,page,process=pending
        valid=(row and row.get('revision')==revision==self._native_revision
               and row.get('page')==page and not row.get('error')
               and process is self.process and project==self.project==((self.owner._project or {}).get('path')))
        rows=row.get('rows') if valid else None
        rows=rows if isinstance(rows,list) and len(rows)<=4096 else None
        self.cursorLayoutResponse.emit(json.dumps(dict(project=project,revision=revision,
                                                       page=page,receipt=receipt,rows=rows)))

    @Slot(str)
    def requestCursorLayout(self, payload):
        if not isinstance(payload,str) or len(payload)>1500:
            return
        try:
            data=json.loads(payload)
        except ValueError:
            return
        if not isinstance(data,dict):
            return
        project,revision,page,receipt=(data.get(key) for key in ('project','baseRevision','page','receipt'))
        if type(receipt) is not int or not 0<receipt<2_000_000_000:
            return
        process=self.process
        if (self._cursor_request is None and self._busy is None and process is not None
                and process.state()==QProcess.ProcessState.Running and project
                and project==self.project==((self.owner._project or {}).get('path'))
                and type(revision) is int and revision==self._native_revision
                and type(page) is int and 0<page<=10000):
            self._cursor_serial=self._cursor_serial%6_999_999+1
            request=2_140_000_000+self._cursor_serial
            self._cursor_request=(request,receipt,project,revision,page,process)
            cmd=dict(kind='playbackCursorLayout',request=request,baseRevision=revision,page=page)
            if process.write(json.dumps(cmd,separators=(',',':')).encode('ascii')+b'\n')!=-1:
                return
            self._cursor_request=None
        self.cursorLayoutResponse.emit(json.dumps(dict(project=project,revision=revision,page=page,receipt=receipt,rows=None)))

    @Slot(str)
    def shadow(self, payload):
        # Non-editing, supersedable raster lane. A pointer tap may write to the
        # sequential worker immediately after a pending preview, never wait
        # for its WebChannel receipt or make the visible editor temporarily busy.
        receipt = None
        try:
            if not isinstance(payload,str) or len(payload)>1500:
                return
            data = json.loads(payload)
            if not isinstance(data,dict):
                return
            receipt = data.get('receipt')
            if type(receipt) is not int or not 0 < receipt < 2_000_000_000:
                return
            x, y, page = data.get('x'), data.get('y'), data.get('page')
            revision = data.get('baseRevision')
            current = (self.owner._project or {}).get('path')
            running = self.process is not None and self.process.state() == QProcess.ProcessState.Running
            position = type(x) in (int,float) and type(y) in (int,float) and 0 <= x < 960 and 0 <= y < 1800
            if (self._shadow_request is None and self._busy is None and running and current and
                    data.get('project') == current == self.project and type(page) is int and 0 < page <= 10000 and
                    type(revision) is int and revision == self._native_revision and position and
                    math.isfinite(x) and math.isfinite(y) and self._external_work is None):
                self._shadow_serial = self._shadow_serial % 19_999_999 + 1
                request = 2_120_000_000 + self._shadow_serial
                request_packet = dict(kind='shadow-note',request=request,baseRevision=revision,x=x,y=y,page=page)
                self._shadow_request = (request,receipt)
                packet = json.dumps(request_packet,allow_nan=False,separators=(',', ':')).encode('ascii')
                if self.process.write(packet+b'\n') != -1:
                    return
                self._shadow_request = None
        except (ValueError,TypeError,OverflowError,RuntimeError):
            pass
        if receipt is not None:
            self.shadowResponse.emit(json.dumps({'project': self.project, 'receipt': receipt}))

    def _compare_drop_request(self):
        self._compare_seq += 1
        self._compare_req = None
        if self._compare_job is not None:
            self._compare_job[1].set()

    def _compare_reset_worker(self):
        self._compare_main_frame = None
        self._score_origin = None
        self._score_document_key = None
        self._worker_editing = False
        self._worker_page = 1
        self._edit_structural_stack.clear()
        self._redo_structural_stack.clear()
        self._pending_cmd_meta = None
        self._compare_worker_req = None
        self._compare_paused_editing = False
        self._compare_saved_page = None
        self._compare_restore_pending = False
        self._compare_touched_worker = False
        self._pending_main_open = None
        self._compare_snapshot_cache = None
        self._compare_vector_cache.clear()
        self._compare_cursor_cache.clear()

    def _schedule_compare(self):
        if not self._compare_timer.isActive():
            self._compare_timer.start(25)

    def _advance_compare(self):
        self._flush_project_score_save()
        if self._compare_restore_pending:
            self._pump_compare_restore()
        elif self._compare_req is not None:
            self._pump_compare()

    def _compare_error(self, message, req=None):
        req = req or self._compare_req
        self._compare_drop_request()
        if req is not None:
            self.compareExcerptReady.emit(json.dumps(dict(
                seq=req['seq'],clientTicket=req['ticket'],project=req['project'],
                ok=False,error=message),ensure_ascii=False))

    def _compare_left_current(self,req):
        if req['project'] != (self.owner._project or {}).get('path'):
            return False
        from .. import live_owner, editor_checkpoint
        path, expected = req['project'], req['query']['leftRevision']
        owner = live_owner.retained(path)
        if owner is not None:
            with owner.lock:
                return not owner.closed and owner.head['revision']==expected
        try:
            rec = editor_checkpoint.read(path)
            return rec is not None and rec.index['revision']==expected
        except (OSError, ValueError):
            return False

    def _next_compare_cmd_id(self):
        self._compare_cmd_serial = self._compare_cmd_serial % 19_999_999 + 1
        return 2_060_000_000 + self._compare_cmd_serial

    def _send_compare_cmd(self, kind, fields, meta):
        proc = self.process
        if proc is None or proc.state() != QProcess.ProcessState.Running or self._busy is not None:
            return False
        cid = self._next_compare_cmd_id()
        packet = {'kind': kind, 'request': cid, 'baseRevision': self._native_revision or 0, **fields}
        raw = json.dumps(packet, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')
        self._busy = cid
        self._compare_worker_req = (cid, kind, meta)
        self._compare_touched_worker = True
        if proc.write(raw + b'\n') == -1:
            self._busy = None
            self._compare_worker_req = None
            return False
        return True

    def _capture_left_notes_snapshot(self, project_path, expected_revision):
        """Read current working Notes MIDI notes & duration safely from live_owner or checkpoint."""
        try:
            from .. import live_owner, editor_checkpoint
            owner = live_owner.retained(project_path)
            if owner is not None:
                frozen = owner.freeze(base=expected_revision)
                doc = json.loads(frozen.body_bytes())
            else:
                rec = editor_checkpoint.read(project_path)
                doc = json.loads(rec.body) if rec is not None else None
            if not isinstance(doc,dict) or doc.get('revision')!=expected_revision:
                return [],0.0,''
            if len(doc.get('notes',())) > 60_000:
                return [],float(doc.get('duration') or 0.0),expected_revision
            notes = [(int(n['pitch']), float(n['start']), float(n['end']))
                     for n in doc.get('notes', ())
                     if isinstance(n, dict) and float(n.get('end', 0)) > float(n.get('start', 0))]
            dur = float(doc.get('duration') or 0.0)
            if notes:
                dur = max(dur, max(n[2] for n in notes))
            return notes, dur, str(doc.get('revision') or '')
        except Exception:
            return [], 0.0, ''

    @Slot()
    def cancelCompareExcerpt(self):
        self.endCompareSession()

    def _pump_compare_restore(self):
        if not self._compare_restore_pending or self._compare_req is not None:
            return
        if self.process is None or self.process.state() != QProcess.ProcessState.Running:
            self._compare_restore_pending = False
            self._compare_paused_editing = False
            self._compare_saved_page = None
            if self._pending_main_open is not None:
                (request,path,_body,_root),self._pending_main_open=self._pending_main_open,None
                self.response.emit(json.dumps({'request':request,'project':path,
                        'error':'原生谱面进程退出，请重新打开乐谱。'},ensure_ascii=False))
            return
        if self._busy is not None or self._host_save is not None:
            self._schedule_compare()
            return
        if self._compare_saved_page is not None and self._worker_page != self._compare_saved_page:
            target_p, self._compare_saved_page = self._compare_saved_page, None
            if self._send_compare_cmd('followPage', {'page': target_p}, {'restore': True}):
                return
        self._compare_saved_page = None
        if self._compare_paused_editing and not self._worker_editing:
            self._compare_paused_editing = False
            if self._send_compare_cmd('mode', {'editing': True}, {'restore': True}):
                return
        self._compare_paused_editing = False
        self._compare_restore_pending = False
        if self._pending_main_open is not None:
            pending, self._pending_main_open = self._pending_main_open, None
            self._fulfill_main_open(*pending)

    def _fulfill_main_open(self, request, path, body, root):
        """Complete a Main Score open/autoOpen after any compare session finishes."""
        if (self.project != path or self.process is None
                or self.process.state() != QProcess.ProcessState.Running):
            self.response.emit(json.dumps({
                'request': request, 'project': path, 'skipped': True,
                'hasUnsavedUserEdits': False,
            }))
            return
        if self._busy is not None:
            if self._compare_worker_req is not None or self._compare_restore_pending:
                self._pending_main_open = (request, path, body, root)
                return
            preserved = body.get('kind') == 'autoOpen' and self._committed_seed(root) not in (None, self._seed_source)
            self.response.emit(json.dumps({
                'request': request, 'project': path, 'skipped': True,
                'nativeScorePreserved': preserved,
                'hasUnsavedUserEdits': self.unsaved_edits(),
            }))
            return
        if self._compare_saved_page is not None and self._worker_page != self._compare_saved_page:
            self._pending_main_open = (request, path, body, root)
            self._compare_restore_pending = True
            self._pump_compare_restore()
            return
        self._compare_saved_page = None
        if self._compare_paused_editing and not self._worker_editing:
            self._compare_paused_editing = False
            self._compare_touched_worker = False
            flags = json.dumps({'kind': 'mode', 'request': request,
                                'baseRevision': self._native_revision or 0,
                                'editing': True},
                               ensure_ascii=True, separators=(',', ':')).encode('ascii')
            self._busy = request
            self._pending_cmd_meta = (request, 'mode', False)
            self._compare_main_frame = request
            if self.process.write(flags + b'\n') != -1:
                return
            self._busy = None
            self._pending_cmd_meta = None
            self._compare_main_frame = None
        elif body.get('baseRevision', 0) == 0 or self._compare_touched_worker:
            self._compare_touched_worker = False
            raster_scale = body.get('rasterScale', 1)
            flags = json.dumps({'kind': 'refreshFrame', 'request': request,
                                'baseRevision': self._native_revision or 0,
                                'rasterScale': raster_scale},
                               ensure_ascii=True, separators=(',', ':')).encode('ascii')
            self._busy = request
            self._pending_cmd_meta = (request, 'refreshFrame', False)
            self._compare_main_frame = request
            if self.process.write(flags + b'\n') != -1:
                return
            self._busy = None
            self._pending_cmd_meta = None
            self._compare_main_frame = None
        preserved = body.get('kind') == 'autoOpen' and self._committed_seed(root) not in (None, self._seed_source)
        self.response.emit(json.dumps({
            'request': request, 'project': path, 'skipped': True,
            'nativeScorePreserved': preserved,
            'hasUnsavedUserEdits': self.unsaved_edits(),
        }))

    @Slot()
    def endCompareSession(self):
        """Called when leaving Notes or returning to Main Score; restores paused Edit mode & page."""
        self._compare_drop_request()
        if self._compare_paused_editing or self._compare_saved_page is not None:
            self._compare_restore_pending = True
            self._pump_compare_restore()

    @Slot(str)
    def requestCompareExcerpt(self, payload):
        """Entry point for dedicated read-only cropped right compare excerpt.

        Supports instant Layer-0 exact fast-path, progressive 0ms provisional
        reference display while Gotoh alignment computes, and guaranteed non-empty
        fallback ('大致位置') with zero fake highlights.
        """
        self._compare_drop_request()
        seq = self._compare_seq
        self._compare_restore_pending = False
        path = ticket = None
        try:
            data = json.loads(payload) if isinstance(payload, str) and len(payload) <= 16384 else None
            if not isinstance(data, dict):
                raise ValueError('无效的对照请求。')
            path,ticket = data.get('project'),data.get('clientTicket')
            root = self._current(path)
            if (type(ticket) is not int or not 0 < ticket < 2**53 or
                not isinstance(data.get('leftRevision'),str) or not 0 < len(data['leftRevision']) <=128):
                raise ValueError('无效的对照版本。')
            a,b=data.get('startSeconds'),data.get('endSeconds')
            if not all(type(x) in (int,float) and math.isfinite(x) for x in (a,b)) or not 0 <= a < b:
                raise ValueError('无效的对照区间。')
            a,b=data.get('startBeat'),data.get('endBeat')
            if (a,b)!=(None,None) and (not all(type(x) in (int,float) and math.isfinite(x) for x in (a,b)) or not 0<=a<b):
                raise ValueError('无效的对照拍点。')
            cached_input=data.get('cachedVectorKeys',[])
            if not isinstance(cached_input,list) or len(cached_input)>24:
                raise ValueError('无效的对照缓存凭据。')
            cached={k for k in cached_input if isinstance(k,str) and len(k)<80}
            self._compare_req = {
                'seq':seq,'ticket':ticket,
                'expires':monotonic()+25.0,
                'project': path,
                'root': root,
                'query': data,
                'cached_keys': cached,
                'phase': 'init',
                'warm_only': data.get('warmOnly') is True,
                'plan': None,
                'final_plan': None,
                'align_started': False,
                'provisional_emitted': False,
            }
            self._pump_compare()
        except Exception as err:
            self._compare_req = None
            self.compareExcerptReady.emit(json.dumps({
                'seq':seq,'clientTicket':ticket,'project':path,'ok':False,'error':str(err)
            }, ensure_ascii=False))

    def _accept_compare_worker(self, cmd_kind, meta, row):
        if meta.get('restore'):
            self._pump_compare_restore()
            return
        if self._compare_restore_pending or self._pending_main_open is not None:
            self._schedule_compare()
            return
        req = self._compare_req
        if req is None or req['seq'] != meta.get('seq'):
            # An actual drawer click can overtake the read-only warmup.
            # A healthy snapshot from this exact native document is still useful.
            if (cmd_kind == 'playbackSnapshot' and not row.get('error')
                    and meta.get('warm_snapshot') == (self.project, self._score_document_key, self._native_revision)
                    and row.get('revision') == self._native_revision):
                midi, bars = row.get('playbackMidi'), row.get('playbackBarMap')
                if isinstance(midi, str) and isinstance(bars, dict):
                    try:
                        self._compare_snapshot_cache = (self.project, self._native_revision,
                                                        base64.b64decode(midi,validate=True), bars)
                    except (ValueError,binascii.Error):
                        pass
            if cmd_kind == 'open' and row.get('error') and req is not None:
                self._compare_error(str(row['error']),req)
                return
            self._pump_compare()
            return
        if row.get('error'):
            if cmd_kind == 'playbackCursorLayout':
                # Cursor layout error should not block rendering the excerpt
                key = (req['project'], self._native_revision, meta.get('page'))
                self._compare_cache_put(self._compare_cursor_cache,key,[])
                self._pump_compare()
                return
            self._compare_error(str(row['error']),req)
            return
        if cmd_kind == 'playbackSnapshot':
            encoded = row.get('playbackMidi')
            bm = row.get('playbackBarMap')
            if isinstance(encoded, str) and isinstance(bm, dict):
                try:
                    midi_bytes=base64.b64decode(encoded,validate=True)
                except (ValueError,binascii.Error):
                    self._compare_error('原谱音符快照无效。',req)
                    return
                self._compare_snapshot_cache=(req['project'], self._native_revision,midi_bytes,bm)
            else:
                self._compare_error('原谱音符快照无效。',req)
                return
        elif cmd_kind == 'vector-page':
            vec = row.get('vector')
            p = row.get('vectorPage') or meta.get('page')
            if isinstance(vec,dict) and type(p) is int:
                self._compare_cache_put(self._compare_vector_cache,(req['project'],self._native_revision,p),vec)
            else:
                self._compare_error('原谱页未返回可绘制内容。',req)
                return
        elif cmd_kind == 'playbackCursorLayout':
            p = row.get('page') or meta.get('page')
            rows = row.get('rows') if isinstance(row.get('rows'), list) else []
            if isinstance(p, int):
                self._compare_cache_put(self._compare_cursor_cache,(req['project'],self._native_revision,p),rows)
        self._pump_compare()

    @staticmethod
    def _compare_cache_put(cache,key,value):
        cache[key]=value;cache.move_to_end(key)
        while len(cache)>24:
            cache.popitem(last=False)

    @Slot(int, object, str)
    def _on_compare_align_done(self, seq, plan, error):
        if self._compare_job is not None and self._compare_job[0] == seq:
            self._compare_job = None
        req = self._compare_req
        if req is None or req['seq'] != seq or not self._compare_left_current(req) or req.get('right') != (self._score_document_key,self._native_revision):
            self._schedule_compare()
            return
        if error or not isinstance(plan, dict):
            # Even if alignment threw unexpectedly, keep the provisional excerpt cleanly
            if req.get('plan'):
                fallback = dict(req['plan'], mode='approximate', label='大致位置',
                                detail='未能完成精细对齐，当前显示附近参考谱段。', coreBars=None,
                                coreStartTick=None, coreEndTick=None)
                req['final_plan'] = fallback
                req['plan'] = fallback
                req['phase'] = 'final'
                self._pump_compare()
            return
        req['final_plan'] = plan
        req['plan'] = plan
        req['phase'] = 'final'
        self._pump_compare()

    def _emit_compare_plan(self, req):
        if not self._compare_left_current(req) or req.get('right')!=(self._score_document_key,self._native_revision):
            self._compare_error('工程内容已变化，请刷新对照。',req)
            return
        plan=req['plan']
        proj=req['project']
        rev=self._native_revision
        out_strips = []
        mode = plan.get('mode')
        s_tick = plan.get('coreStartTick')
        e_tick = plan.get('coreEndTick')
        for strip in plan.get('strips') or []:
            p = strip['page']
            vec = self._compare_vector_cache.get((proj, rev, p))
            v_key = f'{self._score_document_key}:{rev}:{p}'
            lh = int((vec or {}).get('h') or strip.get('logicalHeight') or 1358)
            crop_top = max(0, min(lh - 80, int(strip['cropTop'])))
            crop_bottom = max(crop_top + 80, min(lh, int(strip['cropBottom'])))
            safe_top = max(0, int(strip.get('safeTop', 0)))
            safe_bottom = min(lh, int(strip.get('safeBottom', lh)))
            bands = []
            if mode in ('exact', 'aligned') and s_tick is not None and e_tick is not None:
                c_rows = self._compare_cursor_cache.get((proj, rev, p)) or []
                bands = _compute_core_bands(c_rows, s_tick, e_tick, lh)
                if bands:
                    b_top = min(b['y0'] for b in bands)
                    b_bot = max(b['y1'] for b in bands)
                    crop_top = max(safe_top, min(crop_top, int(b_top - 14)))
                    crop_bottom = min(safe_bottom, max(crop_bottom, int(b_bot + 16)))
            out_strips.append({
                'page': p,
                'cropTop': crop_top,
                'cropBottom': crop_bottom,
                'logicalHeight': lh,
                'vectorKey': v_key,
                'vector': None if v_key in req['cached_keys'] else vec,
                'coreBands': bands,
            })
            req['cached_keys'].add(v_key)
        payload = {
            'ok': True,
            'seq':req['seq'],'clientTicket':req['ticket'],
            'documentKey':self._score_document_key,
            'project':proj,
            'revision': rev,
            'hasUnsavedUserEdits': self.unsaved_edits(),
            'phase': req['phase'],
            'mode': mode,
            'label': plan.get('label', ''),
            'detail': plan.get('detail', ''),
            'coreBars': plan.get('coreBars'),
            'contextBars': plan.get('contextBars'),
            'strips': out_strips,
        }
        self.compareExcerptReady.emit(json.dumps(payload, ensure_ascii=False))

    def _pump_compare(self):
        req=self._compare_req
        if req is None:
            return
        if monotonic()>req['expires'] or not self._compare_left_current(req):
            self._compare_error('原谱对照已过期，请刷新。',req)
            return
        if self._busy is not None or self._host_save is not None or self._cursor_request is not None:
            self._schedule_compare()
            return
        proj=req['project']
        running=(self.project==proj and self.process is not None
                 and self.process.state()==QProcess.ProcessState.Running)
        if 'authority' not in req:
            try:
                req['authority']=self._published_native(req['root'])
            except (OSError,ValueError,RuntimeError) as exc:
                self._compare_error(str(exc),req);return
        regenerate=bool(req['authority'] is not None and req['authority']!=self._seed_source)
        warming=req['warm_only']
        # A hidden warmup may prepare an unopened score, but may not replace
        # a running document or change its page or edit mode.
        if warming and ((running and regenerate) or (not running and self.unsaved_edits())):
            self._compare_req=None
            return
        if not running or regenerate:
            # A new Update Score generation is authoritative; an ordinary
            # compare must never otherwise discard local authoring.
            if self.project!=proj and self.unsaved_edits():
                self._compare_error('尚有另一工程的未保存谱面。',req);return
            cid=self._next_compare_cmd_id()
            try:
                started=self._start(req['root'],proj,cid,committed_only=True,raster_scale=.5,compare_mode=True)
            except (OSError,ValueError,RuntimeError) as exc:
                self._compare_error(str(exc),req);return
            if not started:
                self._compare_error('当前工程尚无已生成的正式乐谱。',req);return
            # _launch retired the old process, not this scoped UI request.
            req['seq']=self._compare_seq
            self._compare_req=req
            self._compare_worker_req=(cid,'open',{'seq':req['seq']})
            self._compare_touched_worker=True
            return
        if not warming and self._worker_editing and not self._score_text_editing:
            self._compare_paused_editing=True
            self._compare_vector_cache.clear()
            if not self._send_compare_cmd('mode',{'editing':False},{'seq':req['seq']}):
                self._schedule_compare()
            return
        rev=self._native_revision
        if type(rev) is not int or not self._score_document_key:
            self._compare_error('当前谱面未能加载。',req);return
        bound=(self._score_document_key,rev)
        if req.get('right',bound)!=bound:
            self._compare_error('原谱已变化，请刷新对照。',req);return
        req['right']=bound
        if (self._compare_snapshot_cache is None or
            self._compare_snapshot_cache[:2]!=(proj,rev)):
            meta={'seq':req['seq']}
            if warming:
                meta['warm_snapshot']=(proj,self._score_document_key,rev)
            if not self._send_compare_cmd('playbackSnapshot',{},meta):
                self._schedule_compare()
            return
        if warming:
            self._compare_req=None
            return
        _,_,midi_bytes,bar_map=self._compare_snapshot_cache
        if req['final_plan'] is not None:
            req['plan']=req['final_plan'];req['phase']='final'
        if req['plan'] is None:
            from ..score_compare_align import align_compare_excerpt, provisional_excerpt_plan
            q=req['query']; start_s=q['startSeconds'];end_s=q['endSeconds']
            start_b=q.get('startBeat');end_b=q.get('endBeat')
            c_digest=q.get('clockDigest') if start_b is not None and end_b is not None else None
            structural=any(self._edit_structural_stack)
            origin=self._score_origin
            exact=bool(origin and not structural and origin.get('clock_digest') and
                       (origin['clock_digest']==c_digest or origin.get('reference_clock')))
            page_heights={p:v['h'] for (pr,rv,p),v in self._compare_vector_cache.items()
                          if pr==proj and rv==rev and v.get('h')}
            left_rev=q['leftRevision']
            reference_only=q.get('referenceOnly') is True
            if exact or reference_only:
                # The initial rough view and exact immutable provenance need
                # no whole Notes serialization or quadratic work on the UI.
                dur=((self.owner._project or {}).get('editor') or {}).get('duration')
                left_notes,left_dur=[],max(1.0,end_s,float(dur or 0.0))
            else:
                left_notes,left_dur,actual=self._capture_left_notes_snapshot(proj,left_rev)
                if actual!=left_rev:
                    self._compare_error('音符已变化，请刷新对照。',req);return
            options=dict(project=proj,left_revision=left_rev,right_revision=rev,left_notes=left_notes,
                left_duration=left_dur,right_midi_bytes=midi_bytes,bar_map=bar_map,origin_info=origin,
                structural_edited=structural,start_seconds=start_s,end_seconds=end_s,
                start_beat=start_b or 0.0,end_beat=end_b or 0.0,clock_digest=c_digest,page_heights=page_heights)
            if exact:
                req['plan']=req['final_plan']=align_compare_excerpt(**options)
                req['phase']='final'
            elif reference_only:
                approx=provisional_excerpt_plan(bar_map,start_seconds=start_s,end_seconds=end_s,
                        left_duration=left_dur,origin_info=origin,page_heights=page_heights)
                req['plan']=req['final_plan']=dict(approx,mode='approximate',label='大致位置',
                        detail='当前显示附近参考谱段。')
                req['phase']='final'
            else:
                req['plan']=provisional_excerpt_plan(bar_map,start_seconds=start_s,end_seconds=end_s,
                                                      left_duration=left_dur,origin_info=origin,page_heights=page_heights)
                req['phase']='provisional'
                req['align_options']=options
        if req.get('align_options') is not None and self._compare_job is None:
            seq=req['seq']; cancel=Event(); options=req.pop('align_options')
            self._compare_job=(seq,cancel)
            def work():
                from ..score_compare_align import align_compare_excerpt
                return align_compare_excerpt(**options,cancelled=cancel.is_set)
            QThreadPool.globalInstance().start(_CompareAlignTask(seq,self._compareAlignDone,work))
        plan=req['plan']
        if req['phase']=='provisional' and req['provisional_emitted']:
            return
        strips=plan.get('strips') or []
        if not strips:
            self._compare_error('原谱没有可查看的小节。',req);return
        # Bound the rendering queue as well as the dynamic programming path.
        if len(strips)>8:
            plan=dict(plan,mode='approximate',label='大致位置',detail='对照区间过长，只显示前几页。',
                      coreBars=None,coreStartTick=None,coreEndTick=None,strips=strips[:8])
            req['plan']=plan
            strips=plan['strips']
        highlight=plan.get('mode') in ('exact','aligned')
        for strip in strips:
            page=strip['page']
            if (proj,rev,page) not in self._compare_vector_cache:
                if not self._send_compare_cmd('vector-page',{'page':page},{'seq':req['seq'],'page':page}):
                    self._schedule_compare()
                return
            if highlight and (proj,rev,page) not in self._compare_cursor_cache:
                if self._worker_page!=page:
                    if self._compare_saved_page is None:
                        self._compare_saved_page=self._worker_page
                    if not self._send_compare_cmd('followPage',{'page':page},{'seq':req['seq'],'page':page}):
                        self._schedule_compare()
                    return
                if not self._send_compare_cmd('playbackCursorLayout',{'page':page},{'seq':req['seq'],'page':page}):
                    self._schedule_compare()
                return
        self._emit_compare_plan(req)
        if self._compare_req is not req:
            return
        if req['phase']=='final':
            self._compare_req=None
        else:
            req['provisional_emitted']=True

    @Slot(str)
    def dispatch(self, payload):
        request = -1
        path = None
        try:
            body = json.loads(payload)
            request = body.get('request', -1)
            if type(request) is not int or not 0 < request < 2_000_000_000:
                raise ValueError('无效的谱面请求。')
            path = body.get('project')
            root = self._current(path)
            kind = body.get('kind')
            if kind in ('open', 'autoOpen'):
                self._compare_drop_request()
                if self.project == path and self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning:
                    regenerated = self._published_native(root)
                    replace = kind == 'autoOpen' and regenerated is not None and regenerated != self._seed_source
                    if replace and self._busy is not None:
                        self.response.emit(json.dumps({'request':request, 'project':path, 'skipped':True,
                                                       'retryAutoOpen':True}))
                        return
                    if not replace and self._compare_touched_worker:
                        self._pending_main_open = (request,path,body,root)
                        self._compare_restore_pending = True
                        self._advance_compare()
                        return
                    if not replace and (kind != 'autoOpen' or regenerated is not None or not self._can_refresh_generated(root)):
                        preserved = (kind == 'autoOpen' and regenerated is None
                                     and self._committed_seed(root) not in (None, self._seed_source))
                        self.response.emit(json.dumps({'request': request, 'project': path, 'skipped': True,
                                                       'nativeScorePreserved': preserved}))
                        return
                raster_scale = body.get('rasterScale', 1)
                if (type(raster_scale) not in (int, float) or not math.isfinite(raster_scale)
                        or not 0.5 <= raster_scale <= 3):
                    raise ValueError('Invalid initial score raster scale')
                if not self._start(root, path, request, committed_only=(kind == 'autoOpen'), raster_scale=raster_scale):
                    self.response.emit(json.dumps({'request': request, 'project': path, 'skipped': True}))
                return
            if path != self.project or self.process is None or self.process.state() != QProcess.ProcessState.Running:
                raise ValueError('请先载入钢琴谱面。')
            if self._busy is not None:
                raise ValueError('上一项谱面操作还在处理中。')
            if self._external_work is not None and kind not in ('page','refreshFrame','score-edge','find-score','page-raster','vector-page','followPage'):
                raise ValueError('原工程只读预览。编辑或导出请打开隔离副本。')
            # Keep the worker lifecycle and private playback receipt host-only.
            # Local notation commands are admitted by the native document, not a
            # second catalogue of editing actions in the QWebChannel.
            if (not isinstance(kind, str) or not 0 < len(kind) <= 32 or
                    not kind.isascii() or not kind.replace('-', '').isalnum() or
                    kind in ('quit', 'playbackSnapshot', 'shadow-note')):
                raise ValueError('不支持的谱面操作')
            body.pop('project', None)
            flags = json.dumps(body, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode('ascii')
            if len(flags) >= 4000:
                raise ValueError('谱面请求过长。')
            self._busy = request
            self._pending_cmd_meta = (request,kind,_is_structural_command(kind,body))
            if kind == 'save':
                self._save_request = request
                self._save_error = None
            if self.process.write(flags + b'\n') == -1:
                self._busy = None
                self._pending_cmd_meta = None
                raise RuntimeError('原生谱面进程未接收命令。')
            if kind == 'save':
                self._emit_save_state()
        except (OSError, ValueError, TypeError, LookupError, AttributeError, RuntimeError) as err:
            if self._save_request is not None and self._save_request == request:
                self._finish_score_save(str(err))
            self.response.emit(json.dumps({'request': request, 'project': path, 'error': str(err)}, ensure_ascii=False))

