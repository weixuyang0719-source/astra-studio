"""Quiet desktop startup with a persistent diagnostic for pythonw failures."""
from .release_paths import data_root
from pathlib import Path
import importlib.util
import traceback


def main():
    root = data_root()
    try:
        if importlib.util.find_spec('astra.studio') is not None:
            from .studio import main as launch
        elif importlib.util.find_spec('astra.desktop') is not None:
            from .desktop import main as launch
        else:
            from .gui import main as launch
        return launch()
    except Exception:
        error = traceback.format_exc()
        folder = root / '.cache'
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / 'startup-error.log'
        log.write_text(error, encoding='utf-8')
        # A pythonw launch must never fail invisibly. Tk is bundled with Python.
        import tkinter as tk
        from tkinter import messagebox
        window = tk.Tk()
        window.withdraw()
        messagebox.showerror('Astra 启动失败', f'请运行 Install-Astra.cmd 修复环境。\n\n详细日志：{log}\n\n{error.splitlines()[-1]}', parent=window)
        window.destroy()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
