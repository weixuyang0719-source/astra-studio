"""Live editor-MIDI routing layered over the existing file/audio bridge.

The memory owner is exclusive with Handoff owners. A retirement with no
release proof blocks replacement; never fall back to a parsed editor SMF.
"""
from dataclasses import replace

from .memory_midi import AppliedMidi, Goal, MemoryMidiOwner, MidiHead, Sample, seconds


class MemoryMidiBridge:
    def __init__(self, *args, memory_midi_session_factory=None, **kwargs):
        self._mem_factory = memory_midi_session_factory
        self._mem_provider = self._mem_epoch = self._mem_selector = None
        self._mem_head, self._mem_problem = MidiHead(None), None
        self._mem_goal = self._mem_owner = self._mem_retiring = None
        self._mem_signature = None
        super().__init__(*args, **kwargs)

    # These methods are trusted host entry points, not Qt/web slots.
    def attach_memory_midi(self, epoch, provider, *, selector=None):
        if self._output_shutdown:
            return
        if type(epoch) is not int or not 0 <= epoch < 2**64 or not callable(provider):
            raise ValueError('invalid live-document MIDI provider')
        self.detach_memory_midi(self._mem_epoch)
        self._edit_preview_invalidate()  # cancels a posted legacy install too
        self._mem_epoch, self._mem_provider, self._mem_selector = epoch, provider, selector
        self.memory_midi_changed(epoch)

    def memory_midi_changed(self, epoch):
        if self._output_shutdown or self._mem_provider is None or epoch != self._mem_epoch:
            return
        prior = self._mem_head.snapshot
        try:
            head = self._mem_provider()
            if not isinstance(head, MidiHead) or type(head.pending) is not bool:
                raise ValueError('invalid applied MIDI head')
            score = head.snapshot
            if score is not None:
                if (not isinstance(score, AppliedMidi) or score.epoch != epoch or
                        type(score.sequence) is not int or not 0 <= score.sequence < 2**64 or
                        (prior is not None and score.sequence < prior.sequence)):
                    raise ValueError('stale or invalid applied MIDI snapshot')
                seconds(score.duration)
                if prior is not None and score.sequence == prior.sequence:
                    score = prior  # one immutable meaning per stamp
            elif prior is not None:
                if not head.pending:
                    raise ValueError('applied MIDI head disappeared without detaching')
                score = prior
            self._mem_head, self._mem_problem = MidiHead(score, head.pending), None
        except Exception as exc:
            self._mem_head, self._mem_problem = MidiHead(prior, True), str(exc) or type(exc).__name__
        w = self._mem_goal
        if w is not None:
            eligible = None if self._mem_head.pending else self._mem_head.snapshot
            if w.score is not eligible:
                view, ended = self._memory_view()
                # A pending head clears the score target, not an acknowledged
                # running clock. Still revoke a Play that has not started yet.
                gate = w.gate if view['playing'] else w.gate+1
                self._put_memory(replace(w, score=eligible, gate=gate,
                                         playing=w.playing and not ended,
                                         position=view['position'] if ended else w.position))
            self._memory_publish()

    def select_memory_midi(self, epoch, *, position=0.):
        if self._output_shutdown:
            return
        if epoch != self._mem_epoch or self._mem_provider is None:
            raise ValueError('No live editor MIDI session selected')
        if self._mem_goal is not None:
            return
        pos = seconds(position)
        self._clear_midi()  # invalidate pending file loads and file transport intents
        self._cancel_midi_intent()
        self._pending_audio = self._deferred_delta = None
        self._retire_output_for_midi()
        self._output_desired = replace(self._output_desired, audition='audio', playing=False)
        self._audition_together = False
        self._device_resume_position = None
        self.memory_midi_changed(epoch)
        head = self._mem_head
        volume = getattr(self, '_midi_volume', None)
        self._mem_goal = Goal(None if head.pending else head.snapshot,
                              position=pos, rate=getattr(self, '_audition_rate', self._output_desired.rate),
                              gain=0. if self._muted else (1. if volume is None else volume/100),
                              click=bool(getattr(self, '_editor_metronome', False)))
        self._sync_editor_transport()
        self._memory_publish(force=True)

    def _retire_memory_owner(self):
        if self._mem_owner is not None:
            self._mem_owner.close()
            self._mem_retiring, self._mem_owner = self._mem_owner, None

    def _pause_memory_for_asio_panel(self):
        # This owner lives outside _output, but still owns a device. Preserve
        # the loaded MIDI and last audible position while releasing the sender.
        w = self._mem_goal
        if w is not None:
            view, _ = self._memory_view()
            self._mem_goal = replace(w, position=view['position'], playing=False, gate=w.gate+1)
        self._retire_memory_owner()
        if w is not None:self._memory_publish(force=True)

    def _leave_memory(self):
        if self._mem_goal is None:
            return
        self._mem_goal = None
        self._retire_memory_owner()
        self._deferred_delta = None
        self._playback_status_ack = None
        self._memory_publish(force=True)

    def leave_memory_midi(self, epoch):
        if epoch == self._mem_epoch:
            self._leave_memory()

    def detach_memory_midi(self, epoch):
        if epoch != self._mem_epoch:
            return
        self._leave_memory()
        self._mem_provider = self._mem_epoch = self._mem_selector = None
        self._mem_head, self._mem_problem = MidiHead(None), None

    def _memory_supported(self):
        return (self._native_midi_enabled and not self._native_midi_failure and not self._output_midi_factory
                and self._dm().current.backend != 'compat')

    def _memory_available(self):
        return (not self._output_shutdown and self._output_device_available and self._memory_supported()
                and getattr(self, '_asio_panel', None) is None)

    def _put_memory(self, goal):
        self._mem_goal = goal
        if self._mem_owner is not None:
            self._mem_owner.put(goal)

    def _try_memory_start(self):
        w = self._mem_goal
        shared_tracks = getattr(self, '_output_shares_kept_endpoint', lambda: False)()
        if (w is None or w.score is None or not w.playing or self._mem_owner is not None
                or self._mem_retiring is not None or (self._output is not None and not shared_tracks) or self._output_retiring
                or self._deferred_delta is not None or self._output_device_notice or not self._memory_available()):
            return
        shared_owner = self._output.owner if shared_tracks and self._output else None
        # Also respect owners kept alive outside the current Handoff slot.
        for owner in self._output_owners:
            if owner is shared_owner:
                continue
            s = owner.snapshot()
            if not (s.closed and s.output_released):
                return
        self._mem_owner = MemoryMidiOwner(w, self._mem_factory)
        self._output_owners.append(self._mem_owner)  # existing shutdown timer owns reaping

    def _memory_release_problem(self):
        if self._mem_retiring is not None:
            s = self._mem_retiring.snapshot()
            if s.closed and not s.output_released:
                return s.error or 'native MIDI output release not confirmed'
        return None

    def _memory_view(self):
        w = self._mem_goal
        assert w is not None
        sample = self._mem_owner.snapshot() if self._mem_owner is not None else Sample()
        score = self._mem_head.snapshot
        duration = float(score.duration) if score is not None else 0.
        error = (self._mem_problem or self._memory_release_problem() or
                 next((h.blocked for h in self._output_retiring if h.blocked), None) or
                 (sample.error if sample.bad_score is None or sample.bad_score is w.score else None))
        available = self._memory_available()
        if not available and not error:
            error = '当前输出路线未提供原生 MIDI 试听。'
        ack = sample.goal
        if ack is not None and ack.gate != w.gate:
            ack = None
        ready = bool(not error and ack is not None and ack.score is w.score)
        # The installed score can still be playing while its replacement is
        # being projected or packed. Only a matching transport gate counts.
        playing = bool(w.playing and ack is not None and sample.playing)
        ended = bool(w.playing and ack is not None and ack.playing and not sample.playing)
        preparing = bool(not error and (w.score is None or (w.playing and not (playing or ended))
                                       or (self._mem_owner is not None and not ready)))
        locator_matches = sample.goal is not None and sample.goal.locator == w.locator
        at = sample.position if locator_matches else w.position
        message = error or ('正在准备当前编辑 MIDI…' if preparing else sample.click_error if ready and w.click else '')
        return dict(position=min(at, duration), duration=duration, rate=w.rate, playing=playing,
                    sampled_at=sample.sampled_at if playing else None, loading=False,
                    available=available, preparing=preparing,
                    play_pending=bool(preparing and w.playing and not (playing or ended)),
                    message=message or '', error=error,
                    applied_sequence=str(score.sequence) if score is not None else None,
                    playback_sequence=str(ack.score.sequence) if ack is not None else None), ended

    def _memory_publish(self, *, force=False):
        w = self._mem_goal
        signature = tuple((k, v) for k, v in self._memory_view()[0].items() if k != 'sampled_at') if w is not None else (
            bool(self._mem_retiring), self._memory_release_problem())
        if (force or signature != self._mem_signature) and not getattr(self, '_closed', False):
            self._mem_signature = signature
            self._player_tick(); self._notify_midi()

    def _output_poll(self):
        was_memory = self._mem_goal is not None or self._mem_retiring is not None
        if self._mem_retiring is not None:
            s = self._mem_retiring.snapshot()
            if s.closed and s.output_released:
                self._mem_retiring = None
        super()._output_poll()
        self._try_memory_start()
        if was_memory or self._mem_goal is not None:
            self._memory_publish()

    def _output_ensure(self, audition=None):
        if self._mem_goal is not None or self._mem_retiring is not None:
            return None  # never race the sender, including on failed release
        return super()._output_ensure(audition=audition)

    def _output_intent(self):
        w = self._mem_goal
        if w is None:
            return super()._output_intent()
        v, ended = self._memory_view()
        return replace(self._output_desired, mode='native-midi', audition='midi', rate=w.rate,
                       position=v['position'], playing=w.playing and not ended, midi_duration=v['duration'],
                       loop=w.loop, midi_gain=w.gain)

    def _output_change(self, **delta):
        w = self._mem_goal
        if w is None:
            return super()._output_change(**delta)
        if self._output_shutdown:
            return
        if delta.get('audition') == 'audio':
            self._leave_memory()
            return super()._output_change(**delta)
        if delta.get('audition') == 'joint':
            raise ValueError('实时编辑 MIDI 尚未接入同步音频试听；未启动旧版预览。')
        before, ended = self._memory_view()
        wanted = w.playing and not ended
        play = delta.get('playing', wanted)
        if type(play) is not bool:
            raise ValueError('invalid MIDI play state')
        if delta.get('playing') is True:
            if not self._memory_available() or self._output_device_notice:
                raise ValueError(before['message'] or '当前输出不可用。')
            self.memory_midi_changed(self._mem_epoch)
            w = self._mem_goal
            # A clean endpoint failure can be retried by an explicit Play.
            if self._mem_owner is not None:
                s = self._mem_owner.snapshot()
                if s.closed and s.output_released:
                    self._mem_owner = None
                    wanted = False
        move = 'position' in delta or ('playing' in delta and play != wanted)
        pos = seconds(delta['position']) if 'position' in delta else (0. if ended and move and play else w.position)
        rate = float(delta.get('rate', w.rate))
        gain = float(delta.get('midi_gain', w.gain))
        if not .25 <= rate <= 2 or not 0 <= gain <= 1:
            raise ValueError('invalid MIDI rate or gain')
        a, b, loop = delta.get('loop', w.loop)
        if type(loop) is not bool:
            raise ValueError('invalid MIDI loop gate')
        click = bool(delta['metronome'][1]) if 'metronome' in delta else w.click
        # If an actual Play is still pending, do not let its older control
        # values authorize an onset after this command. In-place gain/rate/gate
        # changes on a running, acknowledged score do not re-attack notes.
        controls = (rate, gain, (seconds(a), seconds(b), loop), click)
        control_change = controls != (w.rate, w.gain, w.loop, w.click)
        cut = move or (control_change and before['play_pending'])
        if move or control_change or play != w.playing:
            self._put_memory(replace(w, gate=w.gate+int(cut), locator=w.locator+int(move),
                                     position=pos, playing=play,
                                     restart=(play and 'position' not in delta) if move else w.restart,
                                     rate=rate, gain=0. if self._muted else gain, loop=controls[2], click=click))
        self._try_memory_start()
        self._memory_publish(force=True)

    def _select_midi(self, path, *args, **kwargs):
        if self._mem_provider is not None and self._mem_selector is not None and str(path) == self._mem_selector:
            return self.select_memory_midi(self._mem_epoch)
        self._leave_memory()
        return super()._select_midi(path, *args, **kwargs)

    def _edit_preview_queue(self, packet, revision):
        if self._mem_provider is None:
            return super()._edit_preview_queue(packet, revision)
        # Its producer calls memory_midi_changed on *applied*, not on save.
        # Never queue a disk ticket for the bound live editor, even if an
        # export (a different source) is temporarily selected.

    def _edit_preview_selected(self, doc=None):
        return self._mem_goal is not None or super()._edit_preview_selected(doc)

    def _edit_midi_status(self):
        if self._mem_goal is None:
            return super()._edit_midi_status()
        return self._mem_head.pending, self._mem_problem

    def _toggle_midi_output(self, audition):
        if self._mem_goal is None:
            return super()._toggle_midi_output(audition)
        return self._output_change(audition=audition, playing=not self._output_intent().playing)

    def _seek_midi(self, position):
        if self._mem_goal is None:
            return super()._seek_midi(position)
        return self._output_change(position=position)

    def _stop_midi(self):
        if self._mem_goal is None:
            return super()._stop_midi()
        return self._output_change(position=0., playing=False)

    def _stop_audio(self):
        return super()._stop_audio() if self._mem_goal is None else self._stop_midi()

    def _select_audio(self, key, preserve=True):
        self._leave_memory()
        return super()._select_audio(key, preserve)

    def _select_audition_source(self, data):
        if self._mem_goal is None:
            return super()._select_audition_source(data)
        if data['key'] != 'midi':
            self._leave_memory()
            return super()._select_audition_source({**data, 'playing': False})
        r = data.get('range') or {}
        self._audition_range = r.copy()
        delta = dict(loop=(r.get('start', 0), r.get('end', 0), r.get('loop', False)))
        if not data.get('preservePosition'):
            delta.update(position=data['position'], playing=data['playing'])
        return self._output_change(**delta)

    def _sync_editor_transport(self, *, range_only=False, editor_swap=False):
        if self._mem_goal is None:
            return super()._sync_editor_transport(range_only=range_only, editor_swap=editor_swap)
        doc = (self._project or {}).get('editor') or {}
        r = getattr(self, '_audition_range', None) or doc.get('range') or {}
        delta = dict(loop=(r.get('start', 0), r.get('end', 0), r.get('loop', False)))
        if not range_only:
            delta['metronome'] = (None, bool(getattr(self, '_editor_metronome', False)))
        self._output_change(**delta)
        return True

    def _suspend_midi_output(self):
        self._leave_memory()
        return super()._suspend_midi_output()

    def _pause_memory_for_output(self):
        if self._mem_goal is not None:
            self._output_change(playing=False)
            self._retire_memory_owner()

    def _switch_output_device(self):
        self._pause_memory_for_output()
        return super()._switch_output_device()

    def _output_device_changed(self):
        before = self._output_device_id, self._output_device_available
        super()._output_device_changed()
        if before != (self._output_device_id, self._output_device_available):
            self._pause_memory_for_output()

    def _midi_state(self, **kwargs):
        if self._mem_goal is None:
            return super()._midi_state(**kwargs)
        return self._memory_view()[0]

    def _playback_states(self, include_sources, *, compact=False):
        player, midi = super()._playback_states(include_sources, compact=compact)
        if self._mem_goal is not None:
            midi.update(path=self._mem_selector, view_loading=False)
        return player, midi

    def _player_view(self, include_sources, s, *, compact=False):
        out = super()._player_view(include_sources, s, compact=compact)
        if self._mem_goal is not None:
            v, _ = self._memory_view()
            phase = ('blocked' if self._memory_release_problem() else 'error' if v['error'] else
                     'playing' if v['playing'] else 'preparing' if v['preparing'] else 'paused')
            out.update(audition='midi', position=v['position'], playing=False, rate=v['rate'], preparing=v['preparing'])
            # File-owner status memo keys do not describe this owner. Emit the
            # full existing status shape during the live audition.
            out['outputStatus'] = dict(self._output_status_base(None), phase=phase, midiEngine='native',
                                       label=v['message'] or '原生 MIDI', diagnostic=v['error'])
        elif self._mem_retiring is not None and self._output is None:
            problem = self._memory_release_problem()
            out['outputStatus'] = dict(self._output_status_base(None), phase='blocked' if problem else 'preparing',
                                       label=problem or '正在停止 MIDI 输出…')
        return out

    def close(self):
        result = super().close()
        if result is False:
            return False
        self.detach_memory_midi(self._mem_epoch)
        return result
