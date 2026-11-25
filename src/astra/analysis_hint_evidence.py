"""Read-only navigation evidence from a validated successful export directory."""
import json
import math
from pathlib import Path


def export_metadata(export, name):
    """Only fixed sibling artifacts; never follow metadata paths or escaped symlinks."""
    if name not in {"tempo_map.json", "editor_snapshot.json", "piano_timing_review.json"}:
        return {}
    try:
        root = Path(export).resolve()
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, RuntimeError):
        return {}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def build_analysis_hint_evidence(export, report, export_tempo, duration):
    revision = report.get("editor_revision")
    revision = revision if isinstance(revision, (str, int)) and not isinstance(revision, bool) else None
    result = {"source_export": str(Path(export).resolve()), "source_revision": revision,
              "ranges": [], "opening": None, "local_timing_available": False}
    if report.get("status") not in ("complete", "complete_with_warnings"):
        return result
    snapshot = export_metadata(export, "editor_snapshot.json") if revision is not None else {}
    same_revision = revision is not None and snapshot.get("revision") == revision
    # The archived snapshot is a stronger duration source than mutable project state.
    limit = snapshot.get("duration") if same_revision else duration
    if not _finite(limit) or limit <= 0:
        return result

    def add(rows, kind):
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("outside_audio"):
                continue
            start, end = row.get("start_seconds"), row.get("end_seconds")
            if not (_finite(start) and _finite(end) and 0 <= start < end <= limit):
                continue
            count = row.get("affected_note_count")
            if kind == "duet" and (not isinstance(count, int) or isinstance(count, bool) or count <= 0):
                continue
            result["ranges"].append({"kind": kind, "start_seconds": start,
                                     "end_seconds": end, "count": count if kind == "duet" else None})

    review = export_metadata(export, "piano_timing_review.json")
    add(review.get("uncertain_intervals"), "duet")
    # Never substitute current/editor tempo for the tempo archived with this report.
    if not same_revision or not export_tempo or snapshot.get("tempo") != export_tempo:
        return result
    result["local_timing_available"] = True
    resolution = export_tempo.get("metrical_resolution")
    if isinstance(resolution, dict):
        add(resolution.get("local_timing_review"), "local_timing")
    opening = export_tempo.get("opening_phase")
    if isinstance(opening, dict):
        seconds = opening.get("seconds")
        if _finite(seconds) and 0 <= seconds <= limit:
            result["opening"] = {"seconds": seconds, "confirmed": opening.get("confirmed") is True,
                                 "origin": opening.get("origin") if isinstance(opening.get("origin"), str) else None}
    return result
