"""Small validated persistent editor settings, independent of WebEngine storage."""
import re

ACTIONS = frozenset(('editorSelectTool','editorDrawTool','editorQuantize','editorLocateSelection',
    'editorSelectAll','editorUndo','editorRedo','editorCopy','editorPaste','editorDelete',
    'editorRepeat','editorPasteAtOrigin','editorDuplicate','editorSelectNone','editorCut',
    'editorInvertSelection','editorCCLaneToggle',
    'editorSnapToggle','editorLoop','editorPitchZoomIn','editorPitchZoomOut','editorCCZoomIn','editorCCZoomOut','editorTransportPlay','editorTransportStop','editorJumpLeft','editorJumpRight','editorFocusLeft','editorFocusRight','editorMetronome'))
MODIFIERS = ('Ctrl','Alt','Shift','Meta')
KEY = re.compile(r'(?:[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Numpad(?:[0-9]|Divide|Multiply|Add|Subtract|Decimal|Enter)|'
    r'Delete|Backspace|Enter|Tab|Home|End|PageUp|PageDown|Insert|Plus|Minus|Equal|Slash|Period|Comma|Semicolon|Quote|BracketLeft|BracketRight|Backslash|Backquote)\Z')


def validate_shortcuts(value):
    if not isinstance(value,dict) or set(value)-ACTIONS:
        raise ValueError('快捷键设置包含未知编辑动作。')
    result={};used=set()
    for action,chord in value.items():
        if not isinstance(chord,str) or len(chord)>60:raise ValueError('快捷键组合无效。')
        if chord:
            parts=chord.split('+');key=parts[-1];mods=parts[:-1]
            if (not KEY.fullmatch(key) or key=='B' or mods!=[m for m in MODIFIERS if m in mods]):
                raise ValueError('快捷键格式无效或按键已保留。')
            ctrl='Ctrl' in mods or 'Meta' in mods
            if ((ctrl and key in ('Plus','Equal','Minus','0','O','Enter','R','W','NumpadAdd','NumpadSubtract','Numpad0','NumpadEnter'))
                    or key=='F5' or ('Alt' in mods and key in ('F4','Tab'))):
                raise ValueError('该快捷键用于窗口或播放操作。')
            if chord in used:raise ValueError('快捷键不能重复绑定。')
            used.add(chord)
        result[action]=chord
    return result


def saved_shortcuts(preferences):
    try:return validate_shortcuts(preferences.get('editor_shortcuts',{}))
    except (TypeError,ValueError):return {}
