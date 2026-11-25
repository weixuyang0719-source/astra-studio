
"""One project-scoped, paused automatic restore of an approved saved VST rack."""
from dataclasses import dataclass
from time import perf_counter

from .kept_handover import attachment_marker

_DEVICE_FIELDS = ('backend', 'device', 'sample_rate', 'buffer_frames')

def device_settings(manager):
    return tuple(getattr(manager.current, key) for key in _DEVICE_FIELDS)


@dataclass
class Attempt:
    binding: object
    marker: object
    attachment: dict
    phase: str = 'lookup'
    future: object = None
    swap: object = None
    result: dict = None
    error: str = None
    retried: bool = False
    refreshed: bool = False
    play_requested: bool = False
    deadline: float = 0.
    device: tuple = None
    background: bool = False

    @property
    def pending(self):
        return self.phase in ('lookup', 'catalog', 'mounting')


def slot_signature(slots, *, persisted=False):
    return tuple((s['slot_id'], (s['plugin'] or {}).get('key') if persisted else s.get('key'),
                  s['bypassed']) for s in slots)


def _release_image(attempt):
    attempt.attachment = attempt.swap = None
    if attempt.result is not None:
        attempt.result = dict(identity=attempt.result.get('identity'))


def current(studio):
    attempt = getattr(studio, '_vst_saved_restore', None)
    return attempt if attempt is not None and studio._vst_matches(attempt.binding) else None


def abandon(studio, *, include_background=True):
    """A newer audio/source selection is authoritative over the lookup."""
    attempt = getattr(studio, '_vst_saved_restore', None)
    if attempt is not None and attempt.pending:
        if not include_background and attempt.background and studio._vst_matches(attempt.binding):
            return
        if attempt.future is not None:
            attempt.future.cancel()
            attempt.future = None
        if attempt.swap is None:
            # No plugin was opened. Returning to MIDI can still make the
            # first restoration attempt for this project.
            attempt.phase = 'deferred' if not attempt.retried else 'failed'
        else:
            attempt.phase = 'failed'
        attempt.play_requested = False
        attempt.background = False
        attempt.error = '已切换试听源，未继续恢复已保存音源。'
        _release_image(attempt)


def _selected(studio, binding):
    # A project can be shown on Tracks / Score without owning the MIDI lane.
    # Merely opening its metadata must never displace original audio.
    return ((getattr(studio, '_midi_file', None) or {}).get('path') == binding.uri and
            getattr(studio, '_mem_goal', None) is not None)


def lookup(studio):
    attempt = current(studio)
    return attempt if (attempt and attempt.phase in ('lookup', 'catalog') and
                       _selected(studio, attempt.binding)) else None


def _resolve(fetch, config, project, attachment):
    from astra.vst_host.rack_state import from_attachment
    rack = from_attachment(attachment)
    image = rack['instrument']
    if image is None:
        return dict(instrument=None, empty=True)
    identity = (image['key'], image['class_id'])
    try:
        values, effects, coverage = fetch(config, project)
        libraries, instruments, choices, _, detail = values
    except (KeyError, AttributeError, OSError, TypeError, ValueError, RuntimeError, ImportError) as exc:
        return dict(identity=identity, error=str(exc) or '本机 VST 目录读取失败。')
    instrument = instruments.get(identity[0])
    result = dict(rack=rack, instrument=instrument, catalog=values, effects=effects,
                  coverage=coverage, identity=identity, libraries=libraries, choices=choices)
    if libraries is None:
        return dict(result, error=detail or '本机 VST 支持文件不可用。')
    if instrument is None:
        return dict(result, missing=True,
                    error=f'找不到已保存的乐器 {image.get("name") or identity[0]}。')
    if instrument.class_id.lower() != identity[1]:
        return dict(result, error='已保存乐器与本机插件类不符，没有载入其他音源。')
    missing = []
    for slot in rack['slots']:
        plugin = slot['plugin']
        if plugin is None:
            continue
        row = effects.get(plugin['key'])
        name = plugin.get('name') or (row or {}).get('name') or plugin['key']
        if row is not None and row['class_id'].lower() != plugin['class_id']:
            return dict(result, error=f'已保存效果器 {name} 与本机插件类不符，没有替换该效果器。')
        if row is None or not row.get('available'):
            missing.append(name)
    if missing:
        return dict(result, missing=True, error='找不到已保存的效果器：'+'、'.join(missing)+'。')
    return result


def can_retry(attempt, key=None):
    return bool(attempt is not None and attempt.phase == 'failed' and not attempt.retried and
                attempt.result and attempt.result.get('identity') and
                (key is None or key == attempt.result['identity'][0]))


def _enqueue(studio, attempt):
    from astra.studio_bridge import PREFERENCES
    attempt.phase = 'lookup'
    attempt.future = studio._vst_dispatcher.submit(
        _resolve, studio._vst_setup_values, PREFERENCES.with_name('vst-output.json'),
        attempt.binding.owner.project, attempt.attachment)


def start(studio, binding, *, retry_key=None, play_requested=False, background=False):
    if (studio._closed or studio._output_shutdown or getattr(studio, '_editor_closing', False) or
            studio._vst is not None or studio._vst_current_fault()):
        return False
    if not background and not _selected(studio, binding):
        return False
    prior = current(studio)
    if not background and prior is not None and prior.background and prior.pending:
        if attachment_marker(binding.owner) != prior.marker:
            abandon(studio, include_background=True)
            prior = None
        else:
            prior.background = False
            if play_requested:
                prior.play_requested = True
            if prior.swap is not None and prior.swap is studio._vst_swap:
                prior.swap['background_suspend'] = False
                if getattr(studio, '_mem_goal', None) is not None and 'position' not in prior.swap['explicit']:
                    prior.swap['position'] = studio._mem_goal.position
                if play_requested:
                    prior.swap['playing'] = True
            return True
    if studio._vst_pending_transport() is not None or (background and getattr(studio, '_vst_swap', None) is not None):
        return False
    if retry_key is not None:
        if not can_retry(prior, retry_key):
            return False
    elif prior is not None and prior.phase != 'deferred':
        return False
    if prior is None:
        abandon(studio, include_background=True)  # stop waiting on the earlier project's catalog job
    attachment = binding.owner.instrument_state()
    if not attachment or attachment.get('disabled'):
        return False
    standby = getattr(studio, '_instrument_standby', None)
    if standby is not None:
        standby.prime()
    attempt = Attempt(binding, attachment_marker(binding.owner), attachment,
                      retried=retry_key is not None, deadline=perf_counter()+30.,
                      play_requested=False if background else bool(play_requested),
                      background=bool(background))
    studio._vst_saved_restore = attempt
    _enqueue(studio, attempt)
    return True


def toggle_notes_play(studio,binding):
    """Own only one selected project's explicit Play intent while its rack opens."""
    if not _selected(studio,binding) or not studio._vst_matches(binding):return False
    attempt=current(studio)
    if attempt is not None:
        if lookup(studio) is attempt:
            if attachment_marker(binding.owner)!=attempt.marker:
                abandon(studio)
            else:
                attempt.play_requested=not attempt.play_requested
            return True
        if attempt.phase!='deferred':
            return False  # A failed attempt cannot be retried implicitly.
    return start(studio,binding,play_requested=True)


def _fail(studio, attempt, detail):
    attempt.play_requested=False
    attempt.phase = 'failed'
    attempt.error = str(detail) or '未知错误'
    _release_image(attempt)
    studio.logs.emit('未恢复工程 VST 机架，已保留原保存状态：'+attempt.error)


def tick(studio):
    attempt = getattr(studio, '_vst_saved_restore', None)
    if attempt is None or not attempt.pending:
        return
    if (not studio._vst_matches(attempt.binding) or studio._closed or studio._output_shutdown or
            getattr(studio, '_editor_closing', False)):
        abandon(studio)
        studio._vst_saved_restore = None
        return
    if attempt.phase == 'mounting':
        if studio._vst_swap is attempt.swap:
            return
        candidate = attempt.swap['candidate']
        if studio._vst is candidate and candidate.ready and not candidate.error:
            attempt.phase = 'ready'
            _release_image(attempt)
        else:
            detail = candidate.error or studio._vst_state_warning or '已取消连接。'
            # The warning belongs to this project and must not appear on a
            # project opened after a delayed load response.
            studio._vst_state_warning = None
            position = attempt.swap['position']
            was_background = attempt.background
            _fail(studio, attempt, detail)
            if not was_background and getattr(candidate, 'saved_reopen_entered', False):
                studio._vst_reopen_silent_after_saved_failure(attempt.binding, position)
        return
    if attempt.background:
        if studio._vst is not None or getattr(studio, '_vst_swap', None) is not None:
            abandon(studio, include_background=True)
            return
    elif (not _selected(studio, attempt.binding) or studio._vst is not None or
            studio._vst_pending_transport() is not None or getattr(studio._mem_goal, 'playing', False)):
        abandon(studio, include_background=True)
        return
    if attachment_marker(attempt.binding.owner) != attempt.marker:
        abandon(studio, include_background=True)
        return
    if attempt.phase == 'catalog':
        scan = getattr(studio, '_plugin_scan_status', {})
        if (getattr(studio, '_plugin_scan', None) is not None or not scan.get('initialized')) and perf_counter()<attempt.deadline:
            return
        _enqueue(studio, attempt)
        return
    if not attempt.future.done():
        return
    task, attempt.future = attempt.future, None
    try:
        result = attempt.result = task.result()
        if result.get('empty'):
            attempt.phase = 'empty'
            _release_image(attempt)
            return
        if result.get('missing') and not attempt.refreshed:
            # The normal startup scanner may have resolved a plugin without a
            # static module manifest. Never run or restart a full search here.
            attempt.refreshed = True
            attempt.phase = 'catalog'
            return
        if result.get('error'):
            raise ValueError(result['error'])
        studio._vst_catalog = (attempt.binding, result['catalog'])
        studio._vst_effect_catalog = result['effects']
        studio._vst_catalog_coverage = result['coverage']
        manager = studio._dm()
        if getattr(manager, 'awaiting_asio_confirmation', False):
            raise ValueError(manager.message)
        from .vst_project import global_device_route
        device = global_device_route(manager.current, result['choices'], active_endpoint=getattr(studio, '_kept_device', None))
        attempt.device = device_settings(manager)
        # Reuse normal exclusive endpoint admission; only this exact stored
        # instrument and effect chain are eligible. No original audio change.
        studio._vst_choose(attempt.binding, result['instrument'], result['libraries'], device, True,
                           saved_reopen=attempt, background_suspend=attempt.background)
        attempt.swap = studio._vst_swap
        attempt.phase = 'mounting'
        # Start paused unless the person explicitly pressed Play while
        # this same saved MIDI route was connecting.
        if attempt.background:
            attempt.swap['position'] = 0.
            attempt.swap['playing'] = False
        else:
            attempt.swap['position'] = studio._mem_goal.position
            attempt.swap['playing'] = attempt.play_requested
    except (KeyError, AttributeError, OSError, TypeError, ValueError, RuntimeError, ImportError) as exc:
        _fail(studio, attempt, exc)
