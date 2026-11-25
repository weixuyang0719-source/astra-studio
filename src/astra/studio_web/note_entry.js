"use strict";

// Pure decisions shared by the live editor and the Node regression. The editor
// owns the one queue, overlays and requests; this module retains no state.
(function (root, factory) {
  const protocol = factory();
  if (typeof module === 'object' && module.exports) module.exports = protocol;
  else root.AstraNoteEntryProtocol = protocol;
})(globalThis, () => {
  const keys = ['owner', 'project', 'source', 'sourceKey', 'exportPath', 'input', 'audio', 'generation'];
  const sameScope = (a, b) => Boolean(a && b && keys.every(key => a[key] === b[key]));
  const sameBase = (a, b) => sameScope(a, b) && a.version === b.version && a.revision === b.revision;

  function relation(base, view, request) {
    if (!sameScope(base, view)) return 'departed';
    if (sameBase(base, view)) return 'base';
    return view.version > base.version && view.request === request ? 'own' : 'changed';
  }

  function outcome(flight, where, view, notes) {
    const receipt = flight.receipt;
    if (flight.cancelled) {
      return where === 'own' || receipt?.status === 'failed' || receipt?.warning ? 'release' : 'wait';
    }
    if (!receipt) return 'wait';
    if (receipt.status === 'failed') {
      return where === 'base' && receipt.base_current === true && !receipt.cancelled && !receipt.warning
        ? 'failed-at-base' : 'unsafe';
    }
    if (receipt.status !== 'committed' || receipt.warning || receipt.error || receipt.cancelled) return 'unsafe';
    if (where === 'base') return 'wait';
    if (where !== 'own' || view.revision !== receipt.revision) return 'unsafe';
    if (flight.intent.note) {
      const ids = receipt.added_ids;
      // A refused semantic placement may have an acknowledged publication
      // but no content revision or generated ID. Do not call it a saved note.
      if (receipt.changed === false && view.revision === flight.base.revision &&
          Array.isArray(ids) && ids.length === 0) return 'noop';
      if (view.revision === flight.base.revision || !Array.isArray(ids) || ids.length !== 1 ||
          typeof ids[0] !== 'string' || !ids[0] || !notes?.some(n => n.id === ids[0])) return 'unsafe';
    }
    return 'saved';
  }

  // Give normal signal delivery a chance first. A known active transaction
  // needs no full read. Lost notifications get at most three scoped reads;
  // late proof can still settle the lane. Never resend a mutation as recovery.
  function recovery(flight, observed, now) {
    if (flight.reading) return 'wait';
    if (!flight.receipt && observed?.request === flight.request && (observed.pending || observed.status === 'pending')) return 'wait';
    if (now - (flight.receiptAt ?? flight.sent) < 900 || now - flight.readAt < 900) return 'wait';
    return flight.reads < 3 ? 'read' : 'limit';
  }

  return Object.freeze({ limit: 32, sameScope, sameBase, relation, outcome, recovery });
});
