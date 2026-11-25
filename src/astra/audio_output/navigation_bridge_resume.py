"""Isolated production-bridge resume adapter; requires a closed worker receipt."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ResumeReceipt:
    owner: object
    uri: str
    instrument: object
    libraries: object
    device_signature: tuple
    rack_generation: int
    rate: int
    frames: int
    session: object

def resume_navigation(bridge, receipt, choice, *, position, rack_generation,
                      device_signature, release_confirmed, expected_generation,
                      generation):
    binding,instrument,libraries,device,restore=choice
    if not release_confirmed or bridge._vst_retiring:
        return 'wait'
    if (expected_generation!=generation or not bridge._vst_matches(binding)
            or receipt.owner is not binding.owner or receipt.uri!=binding.uri
            or receipt.instrument!=instrument or receipt.libraries!=libraries
            or receipt.rack_generation!=rack_generation or receipt.device_signature!=device_signature
            or not device or device.get('sample_rate')!=receipt.rate
            or bridge._vst is not None or bridge._vst_swap is not None
            or receipt.session.process.poll() is not None):
        return 'miss'
    candidate=bridge._vst_new_route(choice)
    if not getattr(candidate,'prepare_device',None):
        return 'miss'
    def prepare_exact(route):
        if not bridge._vst_matches(binding):
            raise RuntimeError('Navigation binding changed before device lease')
        if not bridge._vst_observe_capacity(route):return False
        endpoint=bridge._kept_device
        live=endpoint.poll()
        if live is None:return False
        if (live['rate'],live['frames'])!=(receipt.rate,receipt.frames):
            raise RuntimeError('Negotiated format differs from parked instrument')
        return endpoint.lease(route)
    candidate.prepare_device=prepare_exact
    candidate.session=receipt.session
    candidate.rack_restored=True
    candidate.restored_instrument_identity=(instrument.key,instrument.class_id.lower())
    # Fresh epoch & projection from _vst_new_route; stock ready route binds capture.
    # Do not restore an image, carry old receipts, or authorize Play.
    bridge._vst_enter(choice,position,prepared=candidate)
    return 'resume'
