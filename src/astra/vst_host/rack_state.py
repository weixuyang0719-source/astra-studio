"""Versioned opaque project attachment. No paths, plugin loading or audio calls.

A rack travels in the existing atomic formal/recovery attachment. Empty slots
are records, not deleted array elements; unavailable plugin states stay intact.
"""
import base64
from copy import deepcopy
from hashlib import sha256
import json
import re

MAGIC = b"ASTRA-VST-RACK\x00\x01"
RACK_KEY = "astra.vst-rack"
RACK_CLASS = "a57a0000000000000000000000000001"
MAX_BYTES = 64 * 1024 * 1024


def plugin_display_name(value):
    """Untrusted display metadata cannot be a raw class identifier."""
    if not isinstance(value, str) or not 0 < len(value) <= 256:
        return None
    label = value.strip()
    if not label or not all(c.isprintable() for c in label):
        return None
    if re.fullmatch(r'(?:vst3-)?[0-9a-f]{32}', label, re.IGNORECASE):
        return None
    return label


def _plugin(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid rack plugin")
    out = deepcopy(value)
    if not isinstance(out.get("key"), str) or not out["key"]:
        raise ValueError("Missing plugin key")
    if not isinstance(out.get("class_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", out["class_id"]):
        raise ValueError("Invalid plugin class")
    if "name" in out:
        name = plugin_display_name(out["name"])
        if name is None:
            del out["name"]
        else:
            out["name"] = name
    state = out.get("state")
    if type(state) is not bytes or not 0 < len(state) <= MAX_BYTES:
        raise ValueError("Invalid opaque plugin state")
    return out


def validate(rack):
    if not isinstance(rack, dict) or rack.get("version") != 1:
        raise ValueError("Unsupported rack version")
    out = deepcopy(rack)
    out["instrument"] = _plugin(out.get("instrument"))
    slots = out.get("slots")
    if not isinstance(slots, list) or len(slots) > 32:
        raise ValueError("Invalid fixed insert slots")
    seen = set()
    for row in slots:
        if not isinstance(row, dict) or not isinstance(row.get("slot_id"), str) or not row["slot_id"] or row["slot_id"] in seen:
            raise ValueError("Invalid or duplicate slot identity")
        seen.add(row["slot_id"])
        if type(row.get("bypassed")) is not bool:
            raise ValueError("Invalid slot bypass")
        row["plugin"] = _plugin(row.get("plugin"))
    return out


def encode(rack):
    value = validate(rack)
    for plugin in [value["instrument"], *(s["plugin"] for s in value["slots"])]:
        if plugin is not None:
            plugin["state"] = base64.b64encode(plugin["state"]).decode("ascii")
    data = MAGIC + json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Rack attachment exceeds existing project attachment limit")
    return data


def decode(data):
    if type(data) is not bytes or not data.startswith(MAGIC) or len(data) > MAX_BYTES:
        raise ValueError("Invalid rack attachment")
    value = json.loads(data[len(MAGIC):].decode("utf-8"))
    for plugin in [value.get("instrument"), *(s.get("plugin") for s in value.get("slots", []))]:
        if plugin is not None:
            plugin["state"] = base64.b64decode(plugin["state"], validate=True)
    return validate(value)


def from_attachment(value):
    if value is None or value.get("disabled"):
        return dict(version=1, instrument=None, slots=[])
    if value.get("instrument") == RACK_KEY and value.get("class_id") == RACK_CLASS:
        return decode(value["state"])
    return validate(dict(version=1, instrument=dict(key=value["instrument"],
                    class_id=value["class_id"], state=value["state"]), slots=[]))


def attachment(rack):
    state = encode(rack)
    return dict(instrument=RACK_KEY, class_id=RACK_CLASS, state=state, sha256=sha256(state).hexdigest())


def clear_slot(rack, slot_id):
    value = validate(rack)
    for slot in value["slots"]:
        if slot["slot_id"] == slot_id:
            slot["plugin"] = None
            slot.pop("missing",None)
            return value
    raise ValueError("Unknown insert slot")


def initial_slots():
    """Product's four initial fixed rows; persisted rows are never regenerated."""
    return [dict(slot_id=f'slot-{i}',bypassed=False,plugin=None) for i in range(1,5)]
