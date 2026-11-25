"""Worker-thread insert preparation; native owns callback publication/reclamation."""
from copy import deepcopy
import os
from pathlib import Path
from time import monotonic_ns
from .rack_state import validate

INSERT_EXPIRED = 'Effect preparation exceeded its time limit; previous inserts kept'


def _check_prepare_deadline(deadline_ns):
    if deadline_ns is not None and monotonic_ns() >= deadline_ns:
        raise RuntimeError(INSERT_EXPIRED)


class InsertControl:
    def __init__(self, instrument, rate):
        self.instrument, self.host, self.rate = instrument, instrument.host, rate
        self.slots = []
        self.plugins = {}
        self.revision = 0
        self.generation = 0
        self._last_generations = {}
        self.error = None
        self.native_transactions=bool(getattr(self.host,"native_insert_transactions",False))
        self._after_boundary=None
        self._previous=None
        self.recall='none'
        self.favorite=None

    def prepare_favorite(self, key, row):
        if self.favorite is None:
            if row is None:
                return
            from .remote_effect import FavoriteEffect
            self.favorite=FavoriteEffect(self.instrument,self.rate)
        if any(s['plugin'] and s['plugin']['key']==key and s['slot_id'] in self.plugins
               for s in self.slots):
            key=row=None
        self.favorite.select(key,row,getattr(self.instrument,'insert_standby',None))

    def _stage(self, row, *, state=None, deadline_ns=None):
        try:
            return self.host.stage_insert(self.instrument,row["module"],row["class_id"],
                                          state=state,deadline_ns=deadline_ns)
        except RuntimeError as exc:
            if str(exc) == "Insert preparation expired":
                raise RuntimeError(INSERT_EXPIRED) from exc
            raise

    def add_live(self, slots, index, key, catalog, *, deadline_ns=None):
        row = catalog.get(key)
        if row is None:
            raise ValueError("Effect is not in application catalog")
        sid=slots[index]["slot_id"]
        if self.native_transactions:
            prior=next((s for s in self.slots if s["slot_id"]==sid),None)
            mounted=self.plugins.get(sid)
            value=prior["plugin"] if prior else None
            identity=(os.path.normcase(str(Path(row["module"]).resolve())),row["class_id"].upper())
            if value and value["key"]==key and not getattr(mounted,'failed',False) and getattr(mounted,"insert_identity",None)==identity:
                # Re-selecting a ready slot preserves its deliberately mounted instance.
                _check_prepare_deadline(deadline_ns)
                return dict(accepted=True,revision=self.revision,unchanged=True)
            if self._after_boundary:
                self._collect_previous(self.instrument.inserts_status())
            candidate=self._take_previous(sid,key,row,None,deadline_ns)
            if candidate is not None and self.favorite and self.favorite.key==key:
                self.favorite.clear()
            if candidate is None and self.favorite is not None:
                ready=self.favorite.take(key,row)
                if ready is not None:
                    from .remote_effect import RemoteEffect
                    candidate=RemoteEffect(self.instrument,row['module'],row['class_id'],self.rate,
                                           None,deadline_ns,prepared=ready)
            if candidate is None:
                candidate=self._stage(row,deadline_ns=deadline_ns)
        else:
            _check_prepare_deadline(deadline_ns)
            candidate=self.host.open_effect(row["module"],row["class_id"],self.rate,self.instrument.max_block)
        try:
            if not self.native_transactions:
                _check_prepare_deadline(deadline_ns)
            # This is already the initialized instance with exactly this state.
            image=candidate.image if hasattr(candidate,'image') else candidate.get_state()
            slots[index]["plugin"] = dict(key=key, class_id=row["class_id"].lower(),state=image)
            result=self.publish(slots, catalog, prepared={sid: candidate}, deadline_ns=deadline_ns,
                                remember_slot=sid)
            if hasattr(candidate,'timing'):result['startup']=candidate.timing
            return result
        finally:
            # The enclosing transaction owns this candidate until publication
            # succeeds. A failure or unchanged slot does not retire the old rack.
            if self.plugins.get(sid) is not candidate:
                candidate.close()

    def publish(self, slots, catalog, *, prepared=None, deadline_ns=None, remember_slot=None,restore_image=None):
        # Restore/requests pass a complete ordered fixed-slot image. Only the
        # application catalog resolves a path; a project cannot nominate DLLs.
        desired = validate(dict(version=1, instrument=None, slots=slots))["slots"]
        old = {s["slot_id"]:s for s in self.slots}
        next_plugins, fresh = {}, []
        try:
            for slot in desired:
                value = slot["plugin"]
                if value is None:
                    slot.pop('missing',None)
                    continue
                sid = slot["slot_id"]
                plugin = prepared.get(sid) if prepared is not None else None
                row = catalog.get(value["key"])
                mounted=self.plugins.get(sid)
                if mounted is not None and old.get(sid,{}).get("plugin") == value and (
                    not self.native_transactions or (
                        plugin is None and row is not None and
                        getattr(mounted,"insert_identity",None)==(
                            os.path.normcase(str(Path(row["module"]).resolve())),row["class_id"].upper())
                    )
                ):
                    next_plugins[sid] = mounted
                    continue
                # Missing plugins retain their opaque state, but never substitute.
                if row is None:
                    slot["missing"] = True
                    continue
                if row["class_id"].lower() != value["class_id"]:
                    raise ValueError("Effect class does not match application catalog")
                if plugin is None:
                    if self.native_transactions:
                        plugin=self._stage(row,state=value["state"],deadline_ns=deadline_ns)
                        fresh.append(plugin)
                    else:
                        plugin=self.host.open_effect(row["module"],row["class_id"],self.rate,self.instrument.max_block)
                        fresh.append(plugin)
                        plugin.set_state(value["state"])
                next_plugins[sid] = plugin
                slot.pop("missing",None)
            ordered = [(next_plugins[s["slot_id"]],s["bypassed"]) for s in desired if s["slot_id"] in next_plugins]
            revision = self.revision+1
            # Native publication is the commit point. Never label an acknowledged
            # publication canceled because transport of its result took longer.
            if self.native_transactions:
                try:
                    self.instrument.set_inserts([p for p,b in ordered],[b for p,b in ordered],
                                                revision,deadline_ns=deadline_ns)
                except RuntimeError as exc:
                    if str(exc) == "Insert preparation expired":
                        raise RuntimeError(INSERT_EXPIRED) from exc
                    raise
            else:
                _check_prepare_deadline(deadline_ns)
                self.instrument.set_inserts([p for p,b in ordered],[b for p,b in ordered],revision)
        except BaseException:
            for plugin in fresh:
                plugin.close()
            raise
        prior = self.plugins
        self.plugins, self.slots, self.revision = next_plugins, desired, revision
        self.generation += 1
        self.error = None
        # Native chain owns its own references until old processing is retired.
        for sid,plugin in prior.items():
            if next_plugins.get(sid) is not plugin:
                if (sid==remember_slot and hasattr(plugin,'park') and not plugin.failed
                        and (plugin.factory_image is not None or restore_image is not None)):
                    self._discard_previous()
                    self._after_boundary=(revision,plugin,sid,old[sid]['plugin']['key'],restore_image)
                else:
                    plugin.close()
        return dict(accepted=True,revision=revision)

    def restore(self, slots, catalog):
        # A fresh instrument may recreate several independent insert workers.
        # Perform vendor startup concurrently, then link the native proxies on
        # the instrument's creating thread and commit the complete chain once.
        if self.plugins or not getattr(self.host, 'isolated_inserts', False):
            return self.publish(slots, catalog)
        rows = []
        for slot in slots:
            value = slot['plugin']
            row = catalog.get(value['key']) if value is not None else None
            if row is None:
                continue
            if row['class_id'].lower() != value['class_id']:
                raise ValueError('Effect class does not match application catalog')
            rows.append((slot['slot_id'],value['key'],row,value['state']))
        if len(rows) < 2:
            peer=self._take_previous(*rows[0]) if rows else None
            if peer is not None:
                sid=rows[0][0]
                try:
                    return self.publish(slots,catalog,prepared={sid:peer})
                finally:
                    if self.plugins.get(sid) is not peer:peer.close()
            self._discard_previous()
            return self.publish(slots, catalog)
        self._discard_previous()
        from concurrent.futures import ThreadPoolExecutor
        from .remote_effect import RemoteEffect, prepare_peer
        effects, pending = {}, {}
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='Restore insert') as loading:
                for sid,_,row,state in rows:
                    pending[sid]=loading.submit(prepare_peer, self.host.dll._name, row['module'],
                        row['class_id'], self.rate, self.instrument.max_block, state, None)
                try:
                    for sid,_,row,state in rows:
                        ready=pending.pop(sid).result()
                        effects[sid]=RemoteEffect(self.instrument,row['module'],row['class_id'],
                            self.rate,state,prepared=ready)
                    return self.publish(slots,catalog,prepared=effects)
                finally:
                    for future in pending.values():
                        future.cancel()
        finally:
            for future in pending.values():
                if future.done() and not future.cancelled() and future.exception() is None:
                    future.result().peer.terminate()
            for sid,effect in effects.items():
                if self.plugins.get(sid) is not effect:
                    effect.close()

    def status(self):
        observed = self.instrument.inserts_status()
        self._collect_previous(observed)
        generations = {sid:p.state_generation() for sid,p in self.plugins.items()}
        if generations != self._last_generations:
            self.generation += 1
            self._last_generations = generations
        return dict(observed, favorite=self.favorite.status() if self.favorite else None,
                    generation=self.generation,topology_revision=self.revision,recall=self.recall,slots=[
            dict(slot_id=s["slot_id"],key=s["plugin"]["key"] if s["plugin"] else None,
                bypassed=s["bypassed"],missing=bool(s.get("missing") or getattr(self.plugins.get(s["slot_id"]),"failed",False)),
                worker_pid=getattr(self.plugins.get(s["slot_id"]),"worker_pid",None),
                editor_pending=getattr(self.plugins.get(s["slot_id"]),"editor_pending",False),
                editor_error=getattr(self.plugins.get(s["slot_id"]),"editor_error",None)) for s in self.slots])

    def capture(self, *, confirmed_images=False):
        # Remote workers maintain confirmed state images; swapping instruments
        # may read them while the original effects continue processing.
        before = self.status()["generation"]
        value = deepcopy(self.slots)
        for slot in value:
            plugin = self.plugins.get(slot["slot_id"])
            if plugin is not None:
                slot["plugin"]["state"] = plugin.handover_image() if confirmed_images else plugin.get_state()
        return value, before

    def close(self):
        if self.favorite:
            self.favorite.close()
        # Caller first releases the instrument/device; these are worker refs.
        self._discard_previous()
        for plugin in self.plugins.values():
            plugin.close()
        self.plugins.clear()

    def _collect_previous(self, observed):
        if self._previous and self._previous.claimed:
            self._previous=None
            self.recall='expired'
        if self._after_boundary and observed['applied_revision']>=self._after_boundary[0]:
            _,effect,slot,key,image=self._after_boundary
            self._after_boundary=None
            try:
                self._previous=effect.park(slot,key,image)
                self.recall='parked' if self._previous else 'unhealthy_or_busy'
            except (RuntimeError,ValueError,OSError):
                self._previous=None
            finally:
                if effect.handle:effect.close()

    def _take_previous(self, sid, key, row, state, deadline_ns=None):
        previous=self._previous
        if previous is None:
            return None
        if previous.key!=key:
            self.recall='different_effect'
            return None
        if state!=previous.restore_image:
            self.recall='different_checkpoint'
            return None
        identity=os.path.normcase(str(Path(row['module']).resolve())),row['class_id'].upper()
        if previous.identity!=identity or (state is not None and previous.slot!=sid):
            self.recall='different_class_or_slot'
            return None
        self._previous=None
        ready=previous.take()
        if ready is None:
            self.recall='reset_failed_or_expired'
            return None
        from .remote_effect import RemoteEffect
        try:
            effect=RemoteEffect(self.instrument,row['module'],row['class_id'],self.rate,
                                state,deadline_ns,prepared=ready)
            self.recall='reused'
            return effect
        except (RuntimeError,ValueError,OSError):
            self.recall='probe_failed'
            _check_prepare_deadline(deadline_ns)
            return None

    def _discard_previous(self):
        if self._previous:
            self._previous.discard()
            self._previous=None
        if self._after_boundary:
            _,effect,_,_,_=self._after_boundary
            self._after_boundary=None
            effect.close()

    def standby_for_restore(self):
        if self.favorite:
            self.favorite.clear()
        self._discard_previous()
        live=[s for s in self.slots if s['plugin'] is not None]
        sid=image=None
        if len(live)==1:
            sid=live[0]['slot_id']
            effect=self.plugins.get(sid)
            if effect and not getattr(effect,'failed',True):
                image=effect.image
        self.publish([],{},remember_slot=sid,restore_image=image)
        self.instrument.warm_candidate_chain()
        self._collect_previous(self.instrument.inserts_status())
