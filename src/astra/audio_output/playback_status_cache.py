"""GUI-thread memo for the output-status core, not an owner or clock cache.

Keep this dependency list in sync with UnifiedStudioBridge._output_status_base
and output_status_model.output_status. Positions, sample timestamps, seek epochs
and diagnostic counters are deliberately not inputs. The paired stream sends
those diagnostics from the current observation; full reads still include them.
"""
from copy import deepcopy


def _freeze(value):
    # Device read-back and control-panel dictionaries may be updated in place.
    # An identity-only key would hide new parameters or an error on that path.
    if isinstance(value, dict):
        return tuple((k, _freeze(v)) for k, v in value.copy().items())
    if isinstance(value, (list, tuple)):
        return tuple(map(_freeze, value))
    return value


def _inputs(bridge, s):
    h = bridge._output
    intent = getattr(h, 'intent', None)
    mode = getattr(h, 'owner_mode', None)
    audition = getattr(intent, 'audition', None)
    pcm = mode in ('native-pcm', 'native-joint', 'native-audio-click')
    click = (getattr(s, 'midi_diagnostics', None) or {}) if pcm else {}
    raw_click = getattr(s, 'pcm_click', None) if mode in ('raw','native-shared','device') and audition == 'audio' else None
    manager = getattr(bridge, '_devices_mgr', None)
    setup = manager.current if manager is not None else None
    device = None if setup is None else (setup.backend, setup.device, setup.sample_rate, setup.buffer_frames,
                                         manager.state, manager.message, _freeze(manager.actual))
    return (
        bridge._want_raw, bridge._output_device_notice, bridge._output_device_available,
        bridge._allow_output_settings, getattr(bridge, '_output_command_error', None),
        bridge._native_shared_failure, bridge._raw_failure_reason,
        getattr(bridge, '_native_midi_failure', None), getattr(bridge, '_native_pcm_failure', None),
        bool(h), bool(getattr(h, 'retiring', False)), getattr(h, 'blocked', None), mode,
        audition, getattr(intent, 'rate', None),
        s is not None, getattr(s, 'closed', None), getattr(s, 'ready', None), getattr(s, 'playing', None),
        getattr(s, 'preparing', None), getattr(s, 'error', None), getattr(s, 'device_parked', False),
        bool(getattr(s, 'output_overrange', 0)),
        _freeze(getattr(s, 'actual_route', None)) if pcm else None,
        click.get('click_requested'), click.get('click_enabled'), click.get('click_unavailable'),
        getattr(raw_click, 'requested', None), getattr(raw_click, 'error', None),
        device, _freeze(getattr(bridge, '_asio_panel_status', None)),
    )


class PlaybackStatusCache:
    def __init__(self):
        self._inputs = None
        self._core = None
        self.revision = 0

    def view(self, bridge, sample):
        inputs = _inputs(bridge, sample)
        if inputs != self._inputs:
            # Cache a detached, read-only projection. Never hold an owner or
            # reuse the sample that produced it on a subsequent command.
            core = deepcopy(bridge._output_status_base(sample))
            if core != self._core:
                self._core = core
                self.revision += 1
            self._inputs = inputs
        return self.revision, self._core
