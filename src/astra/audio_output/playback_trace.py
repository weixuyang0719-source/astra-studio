"""Opt-in playback timing trace (R13). Off unless `.cache/raw-output-session/TRACE_PLAYBACK` exists or
ASTRA_PLAYBACK_TRACE=1. One JSONL timeline on the Python monotonic clock:

  owner   position/playing sample from the audio owner (sampled_at = when it was computed)
  cmd     transport command received by the bridge
  emit    player packet pushed to the page
  page:*  page events (receive, displayed position per frame, pause/play request), converted to the
          Python clock with the page's measured offset

Producers append to a bounded ring. Only the trace writer parses page batches, sorts, encodes
and touches the file. The ring can overwrite old rows during a long stall; page batch rejection
and browser event loss are reported separately. No audio or project data is collected.
"""
import collections, json, math, os, threading, time
from pathlib import Path


class PlaybackTrace:
    MAX_BATCHES = 4
    MAX_PAGE_CHARS = 1024 * 1024
    MAX_PAGE_EVENTS = 4000

    def __init__(self, root):
        self.root = Path(root)
        self.enabled = os.environ.get('ASTRA_PLAYBACK_TRACE') == '1' or (self.root / 'TRACE_PLAYBACK').exists()
        self.events = collections.deque(maxlen=20000)
        self.path = self.root / f'playback-trace-{os.getpid()}.jsonl'
        self._last_owner = None
        self._last_native_winmm = None
        self._last_poll = None
        self._writer = self._sampler = None
        if self.enabled:
            from . import raw_pause_guard
            import astra.midi_preview as midi_preview
            self._diag_modules = (raw_pause_guard, midi_preview)
            self._condition = threading.Condition()
            self._closing = threading.Event()
            self._page = collections.deque()
            self._rejected = 0
            self._write_error = None
            for module in self._diag_modules:
                module.DIAG = self.events
            self._writer = threading.Thread(target=self._write_loop, name='Astra-trace-writer', daemon=True)
            self._sampler = threading.Thread(target=self._sample_stalls, name='Astra-trace-stalls', daemon=True)
            self._writer.start()
            self._sampler.start()

    def _sample_stalls(self):
        # Late wakeups can come from OS scheduling/timer resolution or GIL contention.
        # This is a Python-thread service gap, not a measurement of who held the GIL.
        last = time.perf_counter()
        while not self._closing.wait(.002):
            now = time.perf_counter()
            if now - last > .025:  # above the 15.6 ms Windows timer quantum
                self.events.append(dict(src='stall', t=round(now, 6), gap_ms=round((now - last) * 1000, 1)))
            last = now

    def add(self, kind, **fields):
        if self.enabled and not self._closing.is_set():
            self.events.append(dict(src=kind, t=round(time.perf_counter(), 6), **fields))

    def heartbeat(self):
        # The Qt timer was not serviced on cadence. This does not identify the cause.
        if not self.enabled or self._closing.is_set():
            return
        now = time.perf_counter()
        if self._last_poll is not None and now - self._last_poll > .05:
            self.events.append(dict(src='qt_gap', t=round(now, 6), gap_ms=round((now - self._last_poll) * 1000, 1)))
        self._last_poll = now

    def owner(self, status, intent, mode, generation=None):
        if not self.enabled or self._closing.is_set():
            return
        # State changes are immediate; monotone submission counters are sampled
        # at most once a second. No paths, musical content or PCM are recorded.
        endpoint=getattr(status,'pcm_endpoint',None)
        if endpoint:
            # Static per opened device. Emit once per generation or real change.
            fields={k:endpoint[k] for k in ('endpoint_id','device','interface','raw','rate','channels','host_frames') if k in endpoint}
            signature=(generation,mode,tuple(fields.items()))
            if signature!=getattr(self,'_last_pcm_endpoint',None):
                self._last_pcm_endpoint=signature
                self.add('pcm_endpoint',generation=generation,mode=mode,**fields)
        click = getattr(status, 'pcm_click', None)
        if click is not None:
            names = ('requested','prepared','preparing','error','beat_count','click_gain','master_gain')
            desired = getattr(intent, 'metronome', (None,False))
            identity = (generation,mode,bool(desired[1]),getattr(intent,'midi_gain',None),
                        tuple(getattr(click,k,None) for k in names))
            now = time.perf_counter()
            if identity != getattr(self,'_last_click_identity',None) or now-getattr(self,'_last_click_time',-float('inf')) >= 1.:
                self._last_click_identity=identity; self._last_click_time=now
                fields={k:getattr(click,k,None) for k in names+('submitted_onsets','last_onset_frame','submitted_peak')}
                self.add('pcm_click',generation=generation,mode=mode,intent_enabled=bool(desired[1]),
                         intent_gain=getattr(intent,'midi_gain',None),**fields)
        # Driver activity may change while the transport position remains fixed.
        metrics = getattr(status, 'midi_diagnostics', None)
        if isinstance(metrics, dict) and metrics:
            if generation is None: generation = getattr(status, 'generation', None)
            fields = {name: metrics[name] for prefix in ('send', 'reset')
                      for suffix in ('calls', 'completed', 'errors', 'total_ns', 'max_ns')
                      if (name := prefix + '_' + suffix) in metrics}
            signature = (generation, tuple(sorted(fields.items())))
            if signature != getattr(self, '_last_native_winmm', None):
                self._last_native_winmm = signature
                self.add('native_winmm', generation=generation, mode=mode, **fields)
        output = getattr(status, 'output', None)
        if isinstance(output, dict) and output:
            route = getattr(status, 'actual_route', {})
            provider = getattr(status, 'provider', {})
            identity = (generation, mode, tuple((k, str(route.get(k))) for k in ('host_type','device_index','endpoint','raw','shared')),
                        output.get('sample_rate'), output.get('output_latency'), output.get('observed_frames'),
                        output.get('status_flags'), provider.get('misses'), provider.get('error'))
            if identity != getattr(self, '_last_native_pcm', None):
                self._last_native_pcm = identity
                self.add('native_pcm', generation=generation, mode=mode, route=route,
                         sample_rate=output.get('sample_rate'), latency_seconds=output.get('output_latency'),
                         observed_frames=output.get('observed_frames'), callbacks=output.get('callbacks'),
                         status_flags=output.get('status_flags'), provider=provider)
        key = (round(getattr(status, 'position', 0), 4), getattr(status, 'playing', None), getattr(status, 'applied', None))
        if key == self._last_owner:
            return
        self._last_owner = key
        self.add('owner', pos=round(getattr(status, 'position', 0), 6), playing=bool(getattr(status, 'playing', False)),
                 sampled_at=getattr(status, 'sampled_at', None), applied=getattr(status, 'applied', None),
                 preparing=bool(getattr(status, 'preparing', False)), mode=mode, audition=getattr(intent, 'audition', None),
                 epoch=getattr(status, 'epoch', None), pending=getattr(status, 'pending_target', None),
                 intent_playing=getattr(intent, 'playing', None))

    def submit_page(self, raw):
        """Accept a bounded encoded batch, not a claim that it was written."""
        if not self.enabled:
            return {'ok':False, 'enabled':False}
        with self._condition:
            if self._closing.is_set():
                return {'ok':False, 'queued':False, 'closed':True}
            queued = isinstance(raw, str) and len(raw) <= self.MAX_PAGE_CHARS and len(self._page) < self.MAX_BATCHES
            if queued:
                self._page.append(raw)
            else:
                self._rejected += 1  # drop newest; do not evict accepted batches
            self._condition.notify()
            rejected, error = self._rejected, self._write_error
        return {'ok':queued, 'queued':queued, 'path':str(self.path), 'rejected_batches':rejected, 'writer_error':error}

    def close(self):
        """Request a final drain without waiting for a filesystem on the GUI thread."""
        if not self.enabled:
            return
        with self._condition:
            self._closing.set()
            self._condition.notify_all()
        for module in self._diag_modules:
            if getattr(module, 'DIAG', None) is self.events:
                module.DIAG = None

    def wait_closed(self, timeout):
        """For tests or a non-GUI shutdown coordinator only."""
        deadline = time.perf_counter() + timeout
        for thread in (self._sampler, self._writer):
            if thread is not None:
                thread.join(max(0., deadline-time.perf_counter()))
        return all(thread is None or not thread.is_alive() for thread in (self._sampler, self._writer))

    def _write_loop(self):
        seen_rejects = 0
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._page or self._closing.is_set() or self._rejected != seen_rejects, 3.)
                batches = tuple(self._page)
                self._page.clear()
                closing = self._closing.is_set()
                rejects, seen_rejects = self._rejected-seen_rejects, self._rejected
            started = time.perf_counter()
            rows = []
            # The audio/MIDI producers retain the original deque. Atomic popleft
            # avoids list+clear losing appends in between, without a producer lock.
            for _ in range(len(self.events)):
                try:
                    row = self.events.popleft()
                except IndexError:
                    break
                if isinstance(row, dict):
                    rows.append(row)
            invalid = dropped = 0
            for raw in batches:
                try:
                    packet = json.loads(raw)
                    events = packet['events']
                    if not isinstance(events, list):
                        raise ValueError('events must be a list')
                    loss = packet.get('dropped', 0)
                    if type(loss) is int and loss > 0:
                        dropped += loss
                    dropped += max(0, len(events) - self.MAX_PAGE_EVENTS)
                    rows.extend(dict(e, src='page:' + str(e.get('ev','?'))) for e in events[:self.MAX_PAGE_EVENTS] if isinstance(e, dict))
                except (ValueError, TypeError, KeyError, RecursionError):
                    invalid += 1
            if rows or batches or rejects:
                info = dict(rows=len(rows), page_batches=len(batches), rejected_batches=rejects,
                            invalid_batches=invalid, page_dropped_events=dropped,
                            collect_decode_ms=round((time.perf_counter()-started)*1000,3))
                error = None
                try:
                    self._append_rows(rows, info)
                except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
                    # Do not retry indefinitely or retain a failed batch in memory.
                    error = repr(exc)[:160]
                with self._condition:
                    self._write_error = error
            if closing:
                return

    def _append_rows(self, rows, info):
        def stamp(row):
            value = row.get('t')
            return value if type(value) is int or isinstance(value, float) and math.isfinite(value) else 0
        started = time.perf_counter()
        rows.sort(key=stamp)
        now = time.perf_counter()
        info['sort_ms'] = round((now-started)*1000,3)
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.path, 'a', encoding='utf8') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
            f.flush()
            ended = time.perf_counter()
            f.write(json.dumps(dict(info, src='trace_writer', t=round(ended,6), encode_append_ms=round((ended-now)*1000,3))) + '\n')
