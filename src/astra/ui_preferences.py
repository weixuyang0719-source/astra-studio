"""Application UI preferences; never edits a musical project."""
DEFAULTS = {"cursor_theme": "soft", "rhythm_motion": True, "notes_return_on_pause": False,
            # Score playback menu: 预备拍 / 忽略谱面速度, remembered across restarts.
            "score_count_in": False, "score_flat_tempo": False}

def validate_ui_preferences(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError("设置内容无法识别，请重新打开设置。")
    result = {}
    for key, item in value.items():
        if key == "cursor_theme" and item in ("soft", "system"):
            result[key] = item
        elif key in ("rhythm_motion", "notes_return_on_pause", "score_count_in", "score_flat_tempo") and type(item) is bool:
            result[key] = item
        else:
            raise ValueError("设置值无效，原设置未改变。")
    return result

def saved_ui_preferences(preferences):
    result = dict(DEFAULTS)
    saved = preferences.get("ui_preferences", {})
    if isinstance(saved, dict):
        for key, value in saved.items():
            try:
                result.update(validate_ui_preferences({key: value}))
            except (ValueError, TypeError):
                pass
    return result
