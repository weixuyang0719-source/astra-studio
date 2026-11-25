"""Table and JSON editing for the project tempo map."""

import json
import math
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from .midi import tempo_events
from .timing import TempoMap


ANCHORS = (("seconds", "时间（秒）", float), ("beat", "拍位（四分拍）", float))
SIGNATURES = (("beat", "起始拍位", float), ("numerator", "拍号分子", int), ("denominator", "拍号分母", int))


def _bpm(row, following):
    if following is None:
        return "沿用上一段"
    seconds = following["seconds"] - row["seconds"]
    beats = following["beat"] - row["beat"]
    return f"{60 * (beats / seconds):.2f}" if seconds > 0 and beats > 0 else "区间无效"


def _validate(data):
    tempo = TempoMap(data)
    for anchor, seconds, beat in zip(tempo.anchors, tempo.seconds, tempo.beats):
        anchor.update(seconds=seconds, beat=beat)
    tempo_events(tempo)
    return data


class _Rows(ttk.Frame):
    def __init__(self, parent, fields, *, show_bpm=False):
        super().__init__(parent, padding=8)
        self.fields = fields
        self.show_bpm = show_bpm
        self.rows = []
        columns = [(key, name) for key, name, _ in fields]
        if show_bpm:
            columns.append(("bpm", "到下一锚点的 BPM"))
        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=[key for key, _ in columns], show="headings", selectmode="browse")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for key, label in columns:
            self.tree.heading(key, text=label)
            self.tree.column(key, anchor="center", width=170)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="添加…", command=lambda: self._edit(None)).pack(side="left")
        self.edit_button = ttk.Button(actions, text="编辑…", command=self._edit_selected, state="disabled")
        self.edit_button.pack(side="left", padx=6)
        self.remove_button = ttk.Button(actions, text="删除", command=self._remove, state="disabled")
        self.remove_button.pack(side="left")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Double-1>", self._edit_at_pointer)
        self.tree.bind("<Return>", self._edit_selected)
        self.tree.bind("<Delete>", self._remove)

    def set_rows(self, rows):
        self.rows = [dict(row) for row in rows]
        self._refresh()

    def _refresh(self, selection=None):
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.rows):
            values = [f"{row[key]:.8g}" for key, _, _ in self.fields]
            if self.show_bpm:
                following = self.rows[index + 1] if index + 1 < len(self.rows) else None
                values.append(_bpm(row, following))
            self.tree.insert("", "end", iid=str(index), values=values)
        if selection is not None:
            selected_id = str(selection)
            self.tree.selection_set(selected_id)
            self.tree.focus(selected_id)
            self.tree.see(selected_id)
        self._selection_changed()

    def _selected(self):
        ids = self.tree.selection()
        return int(ids[0]) if ids else None

    def _selection_changed(self, _event=None):
        index = self._selected()
        can_edit = index is not None and not (self.show_bpm and index == 0)
        can_remove = index is not None and index > 0 and len(self.rows) > (2 if self.show_bpm else 1)
        self.edit_button.configure(state="normal" if can_edit else "disabled")
        self.remove_button.configure(state="normal" if can_remove else "disabled")

    def _edit_at_pointer(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self._edit(int(row_id))

    def _edit_selected(self, _event=None):
        index = self._selected()
        if index is not None:
            self._edit(index)

    def _remove(self, _event=None):
        index = self._selected()
        if index is not None and index > 0 and len(self.rows) > (2 if self.show_bpm else 1):
            del self.rows[index]
            self._refresh(min(index, len(self.rows) - 1))

    def _edit(self, index):
        if self.show_bpm and index == 0:
            return
        parent = self.winfo_toplevel()
        dialog = tk.Toplevel(parent)
        dialog.title("添加一行" if index is None else "编辑所选行")
        dialog.transient(parent)
        dialog.resizable(False, False)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        old_row = self.rows[index] if index is not None else {}
        entries = []
        for position, (key, label, cast) in enumerate(self.fields):
            ttk.Label(frame, text=label).grid(row=position, column=0, sticky="w", padx=(0, 12), pady=3)
            field = ttk.Entry(frame, width=25)
            field.grid(row=position, column=1, sticky="ew", pady=3)
            if key in old_row:
                field.insert(0, str(old_row[key]))
            if index == 0 and key == "beat":
                field.configure(state="readonly")
            entries.append((key, label, cast, field))
        initial = next(field for _, _, _, field in entries if field.cget("state") != "readonly")
        initial.focus_set()
        initial.selection_range(0, "end")

        def close(_event=None):
            dialog.destroy()
            parent.grab_set()
            self.tree.focus_set()

        def apply(_event=None):
            updated = dict(old_row)
            for key, label, cast, field in entries:
                try:
                    value = cast(field.get())
                    if not math.isfinite(value) or value < 0:
                        raise ValueError
                except (OverflowError, ValueError):
                    messagebox.showerror("请检查数值", f"{label}须为有效的非负数。", parent=dialog)
                    field.focus_set()
                    return
                updated[key] = value
            if index is not None:
                del self.rows[index]
            self.rows.append(updated)
            order = self.fields[0][0]
            self.rows.sort(key=lambda row: row[order])
            selection = next(i for i, row in enumerate(self.rows) if row is updated)
            self._refresh(selection)
            close()

        ttk.Button(frame, text="应用", command=apply).grid(row=len(entries), column=1, sticky="e", pady=(8, 0))
        ttk.Button(frame, text="取消", command=close).grid(row=len(entries), column=0, sticky="w", pady=(8, 0))
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.bind("<Escape>", close)
        dialog.bind("<Return>", apply)


class TempoEditor:
    def __init__(self, parent, content, template, source_label, save, can_export):
        self.save = save
        self.template = template
        self.source_label = source_label
        self.data = None
        self.window = tk.Toplevel(parent)
        self.window.title("修正节拍与拍号")
        self.window.geometry("710x520")
        self.window.minsize(610, 420)
        self.window.transient(parent)
        self.window.grab_set()

        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        self.summary = tk.StringVar(self.window)
        ttk.Label(frame, textvariable=self.summary).pack(anchor="w", pady=(0, 5))
        actions = ttk.Frame(frame)
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        self.expert_button = ttk.Button(actions, text="专家 JSON…", command=self._toggle_expert)
        self.expert_button.pack(side="left")
        ttk.Button(actions, text="载入示例", command=self._load_example).pack(side="left", padx=6)
        ttk.Button(actions, text="校验并另存…", command=lambda: self._save(False)).pack(side="right")
        ttk.Button(actions, text="保存并重导出…", command=lambda: self._save(True),
                   state="normal" if can_export else "disabled").pack(side="right", padx=6)

        self.tabs = ttk.Notebook(frame)
        self.anchors = _Rows(self.tabs, ANCHORS, show_bpm=True)
        self.signatures = _Rows(self.tabs, SIGNATURES)
        self.tabs.add(self.anchors, text="速度锚点")
        self.tabs.add(self.signatures, text="拍号与弱起")
        ttk.Label(self.anchors, text="双击行可修改时间或拍位。首锚点为音频起点，固定在 0 秒、0 拍。", wraplength=550).pack(
            anchor="w", pady=(8, 0),
        )
        pickup_bar = ttk.Frame(self.signatures)
        pickup_bar.pack(fill="x", pady=(8, 0))
        ttk.Label(pickup_bar, text="弱起长度（四分拍）").pack(side="left")
        self.pickup = tk.StringVar(self.window)
        ttk.Entry(pickup_bar, textvariable=self.pickup, width=10).pack(side="left", padx=6)
        ttk.Label(self.signatures, text="拍号变化须落在小节线上。没有弱起时填 0。", wraplength=550).pack(anchor="w", pady=(4, 0))

        self.json_text = scrolledtext.ScrolledText(frame, wrap="none", undo=True)
        self.expert = True
        self._show_json(content)
        try:
            self._show_tables(_validate(json.loads(content)))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            self.summary.set(f"当前来源 · {source_label}（请在专家 JSON 中修复后再切回表格）")

    def _show_tables(self, data):
        self.data = data
        self.anchors.set_rows(data["anchors"])
        self.signatures.set_rows(data["time_signatures"])
        self.pickup.set(str(data.get("pickup_beats", 0)))
        self.json_text.pack_forget()
        self.tabs.pack(fill="both", expand=True)
        self.expert = False
        self.expert_button.configure(text="专家 JSON…")
        self.summary.set(f"当前来源 · {self.source_label}")

    def _show_json(self, content):
        self.tabs.pack_forget()
        self.json_text.delete("1.0", "end")
        self.json_text.insert("1.0", content)
        self.json_text.edit_reset()
        self.json_text.pack(fill="both", expand=True)
        self.expert = True
        self.expert_button.configure(text="返回表格")
        self.summary.set(f"当前来源 · {self.source_label}")

    def _table_data(self):
        data = dict(self.data, anchors=self.anchors.rows, time_signatures=self.signatures.rows)
        pickup = self.pickup.get().strip()
        try:
            data["pickup_beats"] = float(pickup)
        except ValueError as exc:
            raise ValueError("请输入有效的弱起长度，没有弱起时填 0。") from exc
        return data

    def _read(self):
        return json.loads(self.json_text.get("1.0", "end-1c")) if self.expert else self._table_data()

    def _toggle_expert(self):
        try:
            if self.expert:
                self._show_tables(_validate(self._read()))
            else:
                self._show_json(json.dumps(self._table_data(), ensure_ascii=False, indent=2))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            self._show_error(exc)

    def _load_example(self):
        if messagebox.askyesno("载入示例", "用 120 BPM、4/4 拍的示例替换当前编辑内容？", parent=self.window):
            self.source_label = "120 BPM、4/4 拍示例"
            if self.expert:
                self._show_json(self.template)
            else:
                self._show_tables(_validate(json.loads(self.template)))

    def _save(self, export):
        try:
            data = _validate(self._read())
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            self._show_error(exc)
            return
        self.save(self.window, data, export)

    def _show_error(self, error):
        messagebox.showerror("速度图校验失败", str(error) or type(error).__name__, parent=self.window)
