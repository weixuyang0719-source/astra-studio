"""Publish a staged graph only after the native device has accepted matching audio."""
def attachment_marker(owner):
    head = getattr(owner, 'head', {})
    if 'instrument_state' not in head:
        return 'legacy'
    ref = head['instrument_state']
    if ref is None:
        return 'disabled'
    return (ref['instrument'], ref['class_id'], ref['sha256'])

def _edited(swap):
    return ('attachment_at_start' in swap and
            attachment_marker(swap['choice'][0].owner) != swap['attachment_at_start'])

_CONFLICT = '准备期间工程音色已更改，已取消本次切换并保留工程编辑。'

def _discard(studio, swap, message):
    candidate = swap['candidate']
    candidate.retire()
    studio._vst_retiring.append(candidate)
    studio._vst_swap = None
    studio._vst_state_warning = message
    studio.logs.emit(message)

def _end_return(studio, swap, message):
    # If either producer failed during rollback there is no certified path
    # back. Stop the route normally instead of disposing its audible writer.
    if (studio._vst is swap['old'] and studio._kept_device and
            studio._kept_device.stat(0) != swap['old'].device['kept_endpoint']['token']):
        studio._vst_choice = None
        studio._vst_leave(capture=False)
    _discard(studio, swap, message)

def _withdraw(studio, swap):
    # The source still owns its live renderer and project token until adopt().
    # Cancel before the hardware cut where possible. If the callback won,
    # use its existing aligned handover in reverse before retiring the trial.
    endpoint = studio._kept_device
    endpoint.request(0)
    target = swap['candidate'].device.get('kept_endpoint', {}).get('token')
    if target is None or endpoint.stat(0) != target:
        _discard(studio, swap, _CONFLICT)
        return
    swap['returning_to_source'] = True
    swap['armed'] = False
    swap['deadline'] = swap['candidate'].now() + 30

def adopt(studio, swap):
    owner = swap['choice'][0].owner
    with owner.lock:
        if _edited(swap):
            _withdraw(studio, swap)
            return
        _publish(studio, swap)

def _publish(studio, swap):
    route, old, binding = swap['candidate'],swap['old'],swap['choice'][0]
    studio._vst,studio._vst_binding,studio._vst_choice=route,binding,swap['choice']
    studio._vst_swap=None
    if (old.instrument.key != route.instrument.key and not old.error and
            old.wire.get('isolated_inserts') and not getattr(old,'capture_failed',False)):
        old.standby_binding = binding
    old.retire()
    studio._vst_retiring.append(old)
    generation=route.wire.get('state_generation',0)
    route.capture_owner.bind_instrument_route(route.epoch,generation,changed=True)
    route.capture_bound=True
    capture=getattr(route,'prepared_capture',None)
    if capture and capture['generation']==generation:
        route.fx_checkpoint=capture
        route.capture_owner.update_instrument_state(capture['capture'],route_token=route.epoch,generation=generation)
        studio._editor_native_stamp=tuple(binding.owner.head['stamp'])
        route.prepared_capture=None
    if swap.get('staged_attachment') is not None:
        studio._vst_fx_committed(swap)
    studio._edit_emit_save_status()

def disarm(studio,swap):
    """If the callback already won, route ensuing controls to the newly audible graph."""
    if swap.get('armed'):
        endpoint=studio._kept_device
        endpoint.request(0)
        if swap.get('returning_to_source'):
            swap['armed'] = False
            if endpoint.stat(0) == swap['old'].device['kept_endpoint']['token']:
                _discard(studio,swap,_CONFLICT)
                return True
            return False
        if endpoint.stat(0)==swap['candidate'].device['kept_endpoint']['token']:
            adopt(studio,swap)
            return True
        swap['armed']=False
    return False

def pump(studio,swap):
    candidate,old,binding=swap['candidate'],swap['old'],swap['choice'][0]
    endpoint=studio._kept_device
    try:
        if not swap.get('returning_to_source') and _edited(swap):
            _withdraw(studio, swap)
            if studio._vst_swap is None:return
        returning = swap.get('returning_to_source', False)
        target = old if returning else candidate
        if swap.get('armed') and endpoint.stat(0)==target.device['kept_endpoint']['token']:
            if returning:_discard(studio,swap,_CONFLICT)
            else:adopt(studio,swap)
            return
        if swap.get('armed') and endpoint.stat(10)==target.device['kept_endpoint']['token']:
            raise RuntimeError('新插件在试听交接时停止按时输出，已自动恢复原乐器。')
        if old is not studio._vst or old.error or not endpoint or endpoint.closing:
            raise RuntimeError('当前乐器或设备已改变。')
        endpoint.poll()
        if candidate.error or candidate.projection_error:
            raise RuntimeError(candidate.error or candidate.projection_error)
        if candidate.now()>swap['deadline']:
            raise RuntimeError('新插件未在时限内产生时间对齐的音频，已保留原乐器。')
        if studio._vst_retiring:return  # SPSC slot cannot be reused until the prior writer exits.
        # Audio rendering and the UI's persisted instrument checkpoint advance
        # independently. Replay exactly the frozen music actually driving the old
        # renderer, including real edits as they are offered to that renderer.
        frozen=old.replay_snapshot
        if frozen is None:return
        if candidate.wanted!=old.wanted:
            if disarm(studio,swap):return
            candidate.offer(frozen)
            candidate.music_revision=getattr(old,'music_revision',None)
        if getattr(candidate, 'rate_error', None):
            raise RuntimeError(candidate.rate_error)
        if hasattr(candidate, 'set_rate'):
            candidate.set_rate(getattr(old, 'requested_rate', 1.))
        loop=old.loop
        if loop:candidate.set_loop(*loop)
        candidate.set_pcm(old.pcm_source)
        candidate.set_mix(studio._vst_mix())
        candidate.pump(True)
        if not candidate.ready or candidate.applied is None or candidate.applied!=old.applied or candidate.applied!=candidate.wanted:return
        if hasattr(candidate, 'playback_rate') and not (candidate.playback_rate == old.playback_rate == old.requested_rate):
            return
        revision=swap.get('transport_revision',0)
        if revision!=swap.get('synchronized'):
            if (old.commands or old.awaiting or old.motion is not None or
                    (old.flight and old.flight[0] != 'rt_status')):
                return
            # Exact phase is verified per PCM block; this seek seeds the VST's real
            # MIDI chase including physically held keys and sustained note-offs.
            candidate.transport('seek',old.position)
            if old.wants_play:candidate.transport('play')
            else:candidate.transport('pause')
            swap['synchronized']=revision
            return
        if (candidate.commands or candidate.motion is not None or candidate.awaiting or
                candidate.playing!=old.playing or not candidate.pcm_ready or not old.pcm_ready):return
        if (candidate.playing and abs(candidate.position-old.position)>1.5 and
                candidate.now()-swap.get('last_chase',0)>.5):
            if disarm(studio,swap):return
            candidate.transport('seek',old.position)
            swap['last_chase']=candidate.now()
            return
        if not returning and swap.get('staged_attachment') is not None and not studio._vst_fx_can_commit(swap):
            raise RuntimeError('效果器机架已改变，请重新选择。')
        if not returning and hasattr(candidate, 'live_insert_generation'):
            current = (old.wire.get('inserts') or {}).get('generation', 0)
            if current > candidate.live_insert_generation:
                raise RuntimeError('换琴准备期间效果器设置已改变，已保留当前乐器和最新设置。')
        if not swap['armed']:
            endpoint.request(target.device['kept_endpoint']['token'])
            swap['armed']=True
    except (ValueError,RuntimeError,OSError,KeyError) as exc:
        if studio._vst_swap is None:
            studio._vst_state_warning=str(exc)
            studio.logs.emit(str(exc))
            return
        if swap.get('returning_to_source'):
            _end_return(studio,swap,str(exc))
            return
        if disarm(studio,swap):return
        _discard(studio,swap,str(exc))
