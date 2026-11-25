"""Pure-audio RAW pause guard. Shared MIDI/ordinary transports stay unchanged.
Only activated by explicit 1x audio-only owner admission. No diagnostic I/O.
"""
import copy,threading,time
import numpy as np
from .raw_transport_owner import RawTransport,ContinuousTimeline,RATE,loop_target,loop_repeats,loop_landing_ready,LateRelease,actual_pcm_endpoint
from .raw_pcm_click import PcmMetronome
# Diagnostics sink (R13): a deque set by the bridge only when playback tracing is on. Appends only.
DIAG = None


class PauseGuardTransport(RawTransport):
    def _run(self):
        if self._observer_factory is not None:
            self._publish(closed=True, error='isolated fade candidate excludes MIDI observers', output_released=True)
            self._done.set()
            return
        fading = None
        deferred = []
        pause_serial = 0
        pause_started = 0.0
        device = None
        observer = None
        observer_started = False
        timeline = None
        playback_token = (0, 0)
        submitted = 0
        last_heard = [0]
        running = False
        playing = False
        hold = self._initial[0]
        epoch = 0
        applied = 0
        gain = 1.0
        initialization_release_known = False
        late_release = None
        last_iter = None
        pending = None
        pending_since = time.monotonic()
        error = None
        source = self._initial[1]
        click = PcmMetronome(self._duration)
        applied_revision = 0
        try:
            timeline = ContinuousTimeline(self._cache)
            timeline.source = timeline.old = source
            timeline.seek(round(hold * RATE))
            device = self._backend_factory()
            if self._observer_factory is not None:
                observer_started = True
                observer = self._observer_factory()
                self._observer = observer
            self._publish(ready=True, host_frames=device.capacity, pcm_endpoint=actual_pcm_endpoint(device))
    
            def heard():
                last_heard[0] = max(0, submitted - device.padding())
                return last_heard[0]
    
            def position():
                nonlocal playback_token
                (frame, playback_token) = timeline.played_cursor(heard() if submitted else 0)
                return min(self._duration, frame / RATE) if submitted else hold
    
            def reset_at(seconds):
                nonlocal submitted, running, hold, epoch
                device.stop()
                device.reset()
                running = False
                submitted = 0
                hold = max(0.0, min(self._duration, float(seconds)))
                timeline.seek(round(hold * RATE))
                click.discontinuity()
                epoch += 1
    
            def latest_unheard():
                if pending is not None:
                    return pending[1]
                h = heard()
                future = [t / RATE for (o, t, l) in timeline.segments if o > h]
                return future[-1] if future else None
            def fade_heard():
                # A reset may only discard what follows the fade. Interrupting
                # while faded audio is still queued truncates full-level PCM
                # (an audible click); the wait is at most one host buffer.
                tail_end = fading.get('tail_end') if fading is not None else None
                return tail_end is not None and submitted - device.padding() >= tail_end
            quit_now = False
            while not quit_now:
                drained = fade_heard()
                with self._cv:
                    interrupt_fade = drained and (not self._closing) and any((a in {'seek', 'source', 'play'} or (a == 'batch' and any((ba in {'seek', 'source', 'play'} for (ba, bv) in v[0]))) for (qs, a, v) in list(self._commands) + deferred))
                    if fading is not None and (not self._closing) and (not interrupt_fade):
                        commands = []
                    else:
                        commands = list(self._commands)
                        self._commands.clear()
                if interrupt_fade:
                    reset_at(hold)
                    fading = None
                expanded = [] if fading is not None else deferred
                if fading is None:
                    deferred = []
                for (seq, action, args) in commands:
                    if action == 'batch':
                        (batch, revision) = args
                        expanded.extend(((seq, a, v) for (a, v) in batch))
                        expanded.append((seq, 'revision', (revision,)))
                    else:
                        expanded.append((seq, action, args))
                while expanded:
                    (seq, action, args) = expanded.pop(0)
                    if fading is not None and (not self._closing) and action in {'seek', 'source', 'play'} and fade_heard():
                        reset_at(hold)
                        fading = None
                        if deferred:
                            expanded[0:0] = deferred + [(seq, action, args)]
                            deferred = []
                            continue
                    if fading is not None and action not in {'close', 'device_changed'}:
                        deferred.append((seq, action, args))
                        continue
                    if action == 'revision':
                        applied_revision = args[0]
                        applied = seq
                        continue
                    if action in {'close', 'device_changed'}:
                        if action == 'device_changed':
                            error = 'RAW output device changed; ordinary playback fallback required'
                        if fading is not None:
                            for (_, a, v) in deferred:
                                if a == 'seek':
                                    hold = max(0.0, min(self._duration, float(v[0])))
                        applied = seq
                        quit_now = True
                        break
                    if action == 'seek':
                        self._publish(ended=False)
                        target = max(0.0, min(self._duration, float(args[0])))
                        if timeline.loop and playing:
                            target = loop_target(target,tuple(x/RATE for x in timeline.loop))
                        if playing and running:
                            pending = (seq, target)
                            pending_since = time.monotonic()
                        else:
                            reset_at(target)
                            pending = None
                    elif action == 'pause':
                        pause_serial += 1
                        pause_started = time.monotonic()
                        uncommitted = pending is not None
                        target = latest_unheard()
                        hold = position() if target is None else target
                        pending = None
                        playing = False
                        pause_padding = device.padding() if running and submitted else 0
                        if pause_padding > 0:
                            try:
                                with timeline.lock:
                                    scratch = copy.copy(timeline)
                                    scratch.lock = threading.RLock()
                                    scratch.segments = list(timeline.segments)
                                    scratch._segment_ids = dict(timeline._segment_ids)
                                    if hasattr(timeline, 'jump_tail'):
                                        scratch.jump_tail = timeline.jump_tail.copy()
                                    fade_frame, fade_loop = scratch.cursor, scratch.loop
                                    pcm = scratch.render(240)
                                    faded_to = scratch.cursor
                            except BufferError:
                                reset_at(hold)
                                applied = seq
                                continue
                            w = (0.5 * (1 + np.cos(np.pi * np.arange(240) / 239))).astype('float32')[:, None]
                            # Release only a click already started in submitted PCM. No new beat
                            # onsets in the pause tail or in the safety silence that follows it.
                            pcm = click.mix(pcm * w * gain, fade_frame, fade_loop, gain, fade=w)
                            click.discontinuity()
                            pcm[-1] = 0
                            if not uncommitted:
                                # Queued host audio (up to one buffer, ~47 ms on the
                                # Focusrite shared stream) is still heard before the fade.
                                # Resume where sound actually stopped, not where the key
                                # was pressed; otherwise that buffer is replayed.
                                hold = max(0.0, min(self._duration, faded_to / RATE))
                            now = time.monotonic()
                            fading = {'pcm': pcm, 'written': 0, 'seq': seq, 'began': now, 'progress': now, 'heard': submitted - device.padding()}
                            continue
                        device.stop()
                        running = False
                        click.discontinuity()
                        if target is not None:
                            reset_at(target)
                    elif action == 'play':
                        self._publish(ended=False)
                        if position() >= self._duration:
                            reset_at(0)
                        current=position();target=loop_target(current,tuple(x/RATE for x in timeline.loop) if timeline.loop else None)
                        if target!=current:reset_at(target)
                        playing = True
                        pending_since = time.monotonic()
                        if not running and submitted and (device.padding() > 0):
                            device.start()
                            running = True
                    elif action == 'source':
                        target = int(args[0])
                        timeline.select(target)
                        source = target
                        if not playing:
                            reset_at(hold)
                    elif action == 'gain':
                        gain = float(args[0])
                    elif action == 'midi_gain':
                        click.set_gain(float(args[0]))
                    elif action == 'metronome':
                        click.request(*args)
                    elif action == 'loop':
                        (a, z, on) = args
                        if on and max(a,z) > self._duration:
                            raise ValueError('loop outside source')
                        pos = latest_unheard()
                        if pos is None:
                            pos = position()
                        timeline.loop = (round(a * RATE), round(z * RATE)) if on and round(a*RATE)!=round(z*RATE) else None
                        if timeline.loop and playing:
                            pos = loop_target(pos,(a,z))
                        reset_at(pos)
                        pending = None
                    applied = seq
                if quit_now:
                    break
                click.poll()
                if fading is not None:
                    padding = device.padding()
                    now = time.monotonic()
                    consumed = submitted - padding
                    if consumed > fading['heard']:
                        fading['heard'] = consumed
                        fading['progress'] = now
                    if now - fading['progress'] > 0.25 or now - fading['began'] > 0.5:
                        raise TimeoutError('pause fade endpoint did not drain within safety deadline')
                    available = device.capacity - padding
                    n = min(available, 240 - fading['written'])
                    if n:
                        if fading['written'] == 0:
                            pass
                        at = fading['written']
                        device.write(fading['pcm'][at:at + n])
                        submitted += n
                        fading['written'] += n
                    if fading['written'] == 240 and fading.get('tail_end') is None:
                        fading['tail_end'] = submitted
                    tail_end = fading.get('tail_end')
                    if tail_end is not None and consumed >= tail_end + 2400:
                        seq = fading['seq']
                        reset_at(hold)
                        fading = None
                        applied = seq
                    elif tail_end is not None:
                        zeros = max(0, available - n)
                        if zeros:
                            device.write(np.zeros((zeros, 2), dtype='float32'))
                            submitted += zeros
                    self._publish(applied=applied, intent_revision=applied_revision, position=hold, playing=False, preparing=False, pcm_click=click.status(gain))
                    with self._cv:
                        if not self._closing:
                            self._cv.wait(0.002)
                    continue
                diag = DIAG
                if diag is not None:
                    now_d = time.perf_counter()  # trace clock (QPC)
                    if playing and running and submitted and device.padding() == 0:
                        diag.append(dict(src='underrun', t=round(now_d, 6), epoch=epoch))  # buffer fully drained: audible gap
                    if last_iter is not None and now_d - last_iter > .025:
                        diag.append(dict(src='owner_gap', t=round(now_d, 6), gap_ms=round((now_d - last_iter) * 1000, 1), playing=playing))
                    last_iter = now_d
                frames = [timeline.cursor] + ([timeline.loop[0]] if timeline.loop else []) + ([round(pending[1] * RATE)] if pending else [])
                self._cache.protect({(i, s) for f in frames for i in range(len(self._cache.paths)) for s in range(max(0, f // RATE), max(0, f // RATE) + 2)})
                self._cache.prefetch(timeline.cursor)
                if timeline.loop:
                    self._cache.prefetch(timeline.loop[0])
                if pending is not None:
                    (_, target) = pending
                    f = round(target * RATE)
                    self._cache.prefetch(f)
                    with self._cache.lock:
                        target_ready = all(((i, s) in self._cache.data for i in range(len(self._cache.paths)) for s in range(f // RATE, (f + device.capacity) // RATE + 1)))
                    if target_ready:
                        timeline.jump(f)
                        click.discontinuity()
                        pending = None
                    elif time.monotonic() - pending_since > 5:
                        raise TimeoutError('RAW seek data not ready')
                if playing:
                    if not loop_repeats(timeline.loop) and position() >= self._duration:
                        playing = False
                        reset_at(self._duration)
                        self._publish(ended=True)
                    else:
                        available = device.capacity - device.padding()
                        if not running and not loop_landing_ready(self._cache,timeline.loop,device.capacity):
                            if time.monotonic()-pending_since>5:raise TimeoutError('RAW loop landing data not ready')
                            available=0
                        if available:
                            try:
                                pcm = click.render(timeline, available, gain)
                            except BufferError:
                                if running:
                                    raise
                                if time.monotonic() - pending_since > 5:
                                    raise TimeoutError('RAW initial data not ready')
                            else:
                                device.write(pcm)
                                click.submitted()
                                submitted += available
                                if not running:
                                    device.start()
                                    running = True
                if self._cache.failures:
                    raise RuntimeError(str(self._cache.failures))
                current = position() if playing else hold
                if not submitted:
                    (_, playback_token) = timeline.played_cursor(0)
                self._publish(applied=applied, intent_revision=applied_revision, epoch=epoch, playback_token=playback_token, position=current, playing=playing, preparing=playing and (not running), source=source, pending_target=pending[1] if pending else None, pcm_click=click.status(gain))
                if observer is not None:
                    observer.step(self.snapshot())
                    if getattr(observer, 'error', None):
                        raise RuntimeError('MIDI follower: ' + str(observer.error))
                with self._cv:
                    if not self._commands:
                        self._cv.wait(0.002 if playing else 0.02)
        except BaseException as exc:
            error = repr(exc)
            initialization_release_known = bool(getattr(exc, 'output_released', False))
            late_release = getattr(exc, 'release_event', None)
        finally:
            click.close()
            preserved = hold if not playing else self._status.position
            if pending is not None:
                preserved = pending[1]
            elif timeline is not None and fading is None:
                future = [t / RATE for (o, t, l) in timeline.segments if o > last_heard[0]]
                if future:
                    preserved = future[-1]
            unresolved_target = None
            with self._cv:
                self._closing = True
                waiting = list(deferred)
                for (qseq, qa, qv) in self._commands:
                    if qa == 'batch':
                        waiting.extend(((qseq, a, v) for (a, v) in qv[0]))
                    else:
                        waiting.append((qseq, qa, qv))
            for (_, a, v) in waiting:
                if a == 'seek':
                    unresolved_target = max(0.0, min(self._duration, float(v[0])))
            if unresolved_target is not None:
                preserved = unresolved_target
            released = False
            try:
                if device is not None:
                    try:
                        device.close()
                    except LateRelease as late:
                        late_release = late.release_event
                        raise
                    released = True
                else:
                    released = initialization_release_known
            except BaseException as exc:
                error = (error + '; ' if error else '') + 'cleanup: ' + repr(exc)
            finally:
                if observer is not None:
                    try:
                        observer.close()
                    except BaseException as exc:
                        released = False
                        error = (error + '; ' if error else '') + 'MIDI cleanup: ' + repr(exc)
                elif observer_started:
                    released = released and initialization_release_known
                if released:
                    self._observer = None
                if not released and late_release is not None:
                    # Closed but NOT released: Handoff blocks any replacement. Wait here (owner thread,
                    # never the GUI) until the late resource is really closed, then confirm.
                    self._publish(applied=applied, intent_revision=applied_revision, epoch=epoch, position=preserved, playing=False, preparing=False, closed=True, output_released=False, error=error, source=source, pending_target=unresolved_target, pcm_click=click.status(gain))
                    late_release.wait()
                    released = True
                    error = (error + '; ' if error else '') + 'late device release confirmed'
                self._publish(applied=applied, intent_revision=applied_revision, epoch=epoch, position=preserved, playing=False, preparing=False, closed=True, output_released=released, error=error, source=source, pending_target=unresolved_target, pcm_click=click.status(gain))
                self._done.set()
