"""Prepare a complete, playable insert chain in the disposable route worker."""
from dataclasses import dataclass

from .vst_project import capture_state


@dataclass
class StagedInsert:
    source_epoch: str
    source_generation: int
    slot_id: str
    key: str
    prior_slots: tuple = ()
    snapshot: dict | None = None

    def prepare(self, candidate):
        # This is executed on the control pool, before the renderer is attached.
        result = candidate.session.request('insert_action', action='insert_add',
            slot_id=self.slot_id, key=self.key, catalog=candidate.effect_catalog, candidate_only=True)
        slots = result.get('inserts', {}).get('slots', [])
        if not result.get('accepted') or not any(
                row.get('slot_id') == self.slot_id and row.get('key') == self.key
                and not row.get('missing') for row in slots):
            raise RuntimeError('候选效果器未完成挂载，原效果链继续播放。')
        expected = [row for row in self.prior_slots if row[0] != self.slot_id]
        restored = [slot_image(row) for row in slots if row.get('slot_id') != self.slot_id]
        # A first insertion creates default empty slots. Existing populated
        # slots must retain order, identity, bypass and availability.
        if [row for row in expected if row[1]] != [row for row in restored if row[1]]:
            raise RuntimeError('候选音色架与正在播放的效果器状态不一致，已保留原效果链。')
        applied = candidate.session.request('candidate_warm_rack')
        if applied.get('applied_revision') != result.get('revision') or applied.get('pending_revision'):
            raise RuntimeError('候选 DSP 未确认新效果链，原效果链继续播放。')
        self.snapshot = capture_state(candidate.session, candidate.instrument)
        return result

    def check(self, old):
        if (self.snapshot is None or old is None or old.epoch != self.source_epoch or
                old.wire.get('state_generation', 0) != self.source_generation):
            raise ValueError('准备期间音色或效果器已更改。请保存当前音色后重试。')


def checkpoint_attachment(snapshot):
    prepared = snapshot['capture']
    return dict(prepared.fields, state=prepared.state)


def slot_image(row):
    return row.get('slot_id'), row.get('key'), bool(row.get('bypassed')), bool(row.get('missing'))
