"""One Notes session across same-document VST/silent renderer replacement."""
from dataclasses import dataclass

@dataclass
class NotesTransport:
    binding: object
    anchor: float | None = None
    parked: tuple | None = None

def session(studio, binding):
    current = getattr(studio, '_notes_transport_session', None)
    if current is None or not studio._vst_matches(current.binding):
        current = studio._notes_transport_session = NotesTransport(binding)
    return current

def warming_silent(studio, swap, binding):
    # Only a manual Notes instrument mount can observe the old silent clock.
    # Saved/Score opens use their own explicit locator and Play authorization.
    return bool(swap and swap is studio._vst_swap and studio._vst is None
                and studio._mem_goal is not None and swap.get('old') is None
                and swap.get('saved_reopen') is None
                and swap['phase'] in ('probe', 'preflight', 'release')
                and binding is not None and swap['choice'][0].owner is binding.owner
                and studio._vst_matches(swap['choice'][0]))

def checkpoint(studio, binding):
    """Current Notes locator/loop; never infer a play authorization here."""
    active = studio._notes_transport_binding()
    if active is None or active.owner is not binding.owner or not studio._vst_matches(binding):
        return None
    pending = studio._vst_pending_transport()
    r = studio._vst
    if warming_silent(studio, pending, active):
        # _memory_view fences by the current Goal locator. It returns the
        # explicit target until acknowledged, then the running native clock.
        return studio._memory_view()[0]['position'], studio._mem_goal.loop
    # Other pending routes have their own explicit locator policy.
    if pending is not None and pending.get('choice') and pending['choice'][0].owner is binding.owner:
        if r is None or 'position' in pending['explicit']:
            return pending['position'], pending.get('loop') or (0., 0., False)
    if r is not None and studio._vst_matches(studio._vst_binding):
        return r.requested_position, r.loop or (0., 0., False)
    w = studio._mem_goal
    if w is not None:
        return studio._memory_view()[0]['position'], w.loop
    current = getattr(studio, '_notes_transport_session', None)
    return current.parked if current is not None and studio._vst_matches(current.binding) else None
