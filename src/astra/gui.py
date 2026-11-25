"""Windows desktop interface for the local transcription pipeline."""

from .release_paths import data_root
import json
import math
import os
import queue
import threading
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


ROOT = data_root()
MODES = {"MIDI 和乐谱": "all", "仅 MIDI": "midi", "仅乐谱": "score"}
TEMPO_SCALES = {"原速 (×1)": 1.0, "半速 (×0.5)": 0.5, "倍速 (×2)": 2.0}
OUTPUT_TYPES = {
    ".mid": "MIDI", ".midi": "MIDI", ".pdf": "乐谱", ".musicxml": "乐谱",
    ".xml": "乐谱", ".mxl": "乐谱", ".svg": "乐谱", ".html": "乐谱",
    ".wav": "音频", ".mp3": "音频", ".flac": "音频", ".ogg": "音频",
    ".m4a": "音频", ".aac": "音频", ".aiff": "音频",
}
TEMPO_EXAMPLE = {
    "version": 1,
    "anchors": [
        {"seconds": 0.0, "beat": 0.0},
        {"seconds": 0.5, "beat": 1.0},
        {"seconds": 1.0, "beat": 2.0},
        {"seconds": 2.0, "beat": 4.0},
    ],
    "time_signatures": [{"beat": 0, "numerator": 4, "denominator": 4}],
    "warnings": [],
}


class AstraApp:
    def __init__(self, window):
        self.window = window
        self.events = queue.SimpleQueue()
        self.busy = False
        self.project = None
        self.result_dir = None
        self.result_files = []
        self.audio = tk.StringVar()
        self.output = tk.StringVar(value=str(ROOT / "outputs"))
        self.mode = tk.StringVar(value=next(iter(MODES)))
        self.piano = tk.BooleanVar(value=True)
        self.guitar = tk.BooleanVar(value=True)
        self.keep_stems = tk.BooleanVar(value=True)
        self.tempo = tk.StringVar()
        self.bpm = tk.StringVar()
        self.meter = tk.StringVar(value="4/4")
        self.scale = tk.StringVar(value=next(iter(TEMPO_SCALES)))
        self.status = tk.StringVar(value="请选择要转谱的音频。")
        self.project_label = tk.StringVar(value="尚未选择项目")
        self.report_summary = tk.StringVar()
        self.report_path = None
        self.log_path = None
        self.window.title("Astra 音频转谱")
        self.window.minsize(700, 670)
        self.window.geometry("860x720")
        self._build()
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.after(100, self._poll)

    def _build(self):
        page = ttk.Frame(self.window, padding=16)
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)
        heading = ttk.Frame(page)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(heading, text="Astra 音频转谱", font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        ttk.Button(heading, text="帮助与安装", command=self._show_help).pack(side="right")

        settings = ttk.LabelFrame(page, text="1  选择音频与输出", padding=10)
        settings.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self._path_row(settings, 0, "音频文件", self.audio, self._choose_audio)
        self._path_row(settings, 1, "输出目录", self.output, self._choose_output)
        choices = ttk.Frame(settings)
        choices.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Checkbutton(choices, text="钢琴", variable=self.piano).pack(side="left")
        ttk.Checkbutton(choices, text="吉他", variable=self.guitar).pack(side="left", padx=(6, 16))
        ttk.Label(choices, text="输出").pack(side="left", padx=(0, 6))
        self._combo(choices, self.mode, MODES, 15).pack(side="left")
        ttk.Checkbutton(choices, text="保留分轨音频", variable=self.keep_stems).pack(side="left", padx=12)

        advanced_panel = ttk.Frame(page)
        advanced_panel.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.advanced_visible = tk.BooleanVar()
        ttk.Checkbutton(
            advanced_panel, text="高级设置 · 速度、拍号与缓存重导出",
            variable=self.advanced_visible, command=self._toggle_advanced,
        ).pack(anchor="w")
        self.advanced = ttk.Frame(advanced_panel, padding=(10, 6))
        self._path_row(self.advanced, 0, "速度图文件", self.tempo, self._choose_tempo)
        timing = ttk.Frame(self.advanced)
        timing.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(timing, text="固定 BPM").pack(side="left", padx=(0, 6))
        ttk.Entry(timing, textvariable=self.bpm, width=8).pack(side="left")
        ttk.Label(timing, text="拍号").pack(side="left", padx=(12, 6))
        ttk.Entry(timing, textvariable=self.meter, width=7).pack(side="left")
        ttk.Label(timing, text="速度倍率").pack(side="left", padx=(12, 6))
        self._combo(timing, self.scale, TEMPO_SCALES, 13).pack(side="left")
        ttk.Button(timing, text="编辑速度图", command=self._edit_tempo).pack(side="left", padx=8)
        ttk.Label(
            self.advanced, wraplength=620,
            text="速度图支持 JSON 或 MIDI。两项留空时优先复用项目已保存的速度图；没有可用缓存时自动分析。"
                 "重导出沿用项目速度图，倍率仅用于本次操作。",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        cached = ttk.Frame(self.advanced)
        cached.grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 0))
        self.choose_project_button = ttk.Button(cached, text="选择缓存项目…", command=self._choose_project)
        self.choose_project_button.pack(side="left")
        self.export_button = ttk.Button(cached, text="仅重导出", command=self._reexport, state="disabled")
        self.export_button.pack(side="left", padx=6)
        ttk.Label(cached, text="沿用已有音符数据，无需再次识别").pack(side="left", padx=4)

        actions = ttk.Frame(page)
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.run_button = ttk.Button(actions, text="开始转谱", command=self._run)
        self.run_button.pack(side="left")
        self.progress = ttk.Progressbar(actions, mode="determinate", length=96)
        self.progress.pack(side="left", padx=10)
        ttk.Label(actions, textvariable=self.status).pack(side="left")

        panels = ttk.Panedwindow(page, orient="vertical")
        panels.grid(row=4, column=0, sticky="nsew")
        results = ttk.LabelFrame(panels, text="2  项目结果", padding=8)
        log_panel = ttk.LabelFrame(panels, text="3  运行日志", padding=8)
        panels.add(results, weight=1)
        panels.add(log_panel, weight=2)

        results_header = ttk.Frame(results)
        results_header.pack(fill="x", pady=(0, 5))
        self.folder_button = ttk.Button(
            results_header, text="打开结果文件夹", command=lambda: self._open(self.result_dir), state="disabled",
        )
        self.folder_button.pack(side="right")
        self.file_button = ttk.Button(
            results_header, text="打开所选文件", command=self._open_selected, state="disabled",
        )
        self.file_button.pack(side="right", padx=6)
        self.correct_button = ttk.Button(
            results_header, text="修正节拍…", command=lambda: self._edit_tempo(use_project=True), state="disabled",
        )
        self.correct_button.pack(side="right")
        ttk.Label(results_header, textvariable=self.project_label).pack(side="left")
        self.report_row = ttk.Frame(results)
        ttk.Button(self.report_row, text="打开报告", command=lambda: self._open(self.report_path)).pack(side="right")
        ttk.Label(self.report_row, textvariable=self.report_summary, wraplength=510).pack(side="left")
        self.results_body = ttk.Frame(results)
        self.results_body.pack(fill="both", expand=True)
        self.results = tk.Listbox(self.results_body, height=4, exportselection=False)
        results_scroll = ttk.Scrollbar(self.results_body, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=results_scroll.set)
        self.results.pack(side="left", fill="both", expand=True)
        results_scroll.pack(side="right", fill="y")
        self.results.bind("<<ListboxSelect>>", self._selection_changed)
        self.results.bind("<Double-1>", self._open_selected)
        self.results.bind("<Return>", self._open_selected)

        self.log_button = ttk.Button(
            log_panel, text="打开本次日志", command=lambda: self._open(self.log_path), state="disabled",
        )
        self.log_button.pack(anchor="e", pady=(0, 4))
        self.log_text = scrolledtext.ScrolledText(log_panel, height=7, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def _path_row(self, parent, row, label, variable, browse):
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Button(parent, text="浏览…", command=browse).grid(row=row, column=2, padx=(8, 0), pady=3)

    def _combo(self, parent, variable, values, width):
        return ttk.Combobox(parent, textvariable=variable, values=list(values), width=width, state="readonly")

    def _toggle_advanced(self):
        if self.advanced_visible.get():
            self.advanced.pack(fill="x")
        else:
            self.advanced.pack_forget()

    def _choose_audio(self):
        path = filedialog.askopenfilename(
            parent=self.window, title="选择要转谱的音频",
            filetypes=[("音频文件", "*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.aif *.aiff *.wma *.opus"),
                       ("所有文件", "*.*")],
        )
        if path:
            self.audio.set(path)

    def _choose_output(self):
        path = filedialog.askdirectory(parent=self.window, title="选择输出目录")
        if path:
            self.output.set(path)

    def _choose_tempo(self):
        path = filedialog.askopenfilename(
            parent=self.window, title="选择速度图",
            filetypes=[("速度图文件", "*.json *.mid *.midi"), ("JSON 文件", "*.json"), ("MIDI 文件", "*.mid *.midi")],
        )
        if path:
            self.tempo.set(path)
            self.bpm.set("")

    def _choose_project(self):
        output = Path(self.output.get().strip() or ROOT / "outputs")
        path = filedialog.askdirectory(
            parent=self.window, title="选择含 project.json 的缓存项目文件夹",
            initialdir=output if output.is_dir() else ROOT,
        )
        if not path:
            return
        project = Path(path)
        if not (project / "project.json").is_file():
            messagebox.showerror("无法打开项目", "所选文件夹缺少 project.json。", parent=self.window)
            return
        self._show_project(project)
        self.status.set("已打开缓存项目。")

    def _options(self):
        tempo_text = self.tempo.get().strip()
        bpm_text = self.bpm.get().strip()
        if tempo_text and bpm_text:
            raise ValueError("请在导入速度图和固定 BPM 之间选择一项。")
        tempo_path = Path(tempo_text).expanduser().resolve() if tempo_text else None
        if tempo_path and not tempo_path.is_file():
            raise ValueError("找不到所选速度图文件。")
        if tempo_path and tempo_path.suffix.lower() not in (".json", ".mid", ".midi"):
            raise ValueError("请选择 JSON 或 MIDI 格式的速度图。")
        bpm = None
        if bpm_text:
            try:
                bpm = float(bpm_text)
            except ValueError as exc:
                raise ValueError("请输入有效的 BPM 数值，例如 120。") from exc
            if not math.isfinite(bpm) or not 10 <= bpm <= 600:
                raise ValueError("固定 BPM 须在 10 到 600 之间。")
        meter = "4/4"
        if bpm is not None:
            try:
                numerator, denominator = map(int, self.meter.get().split("/"))
                if not 1 <= numerator <= 32 or denominator not in (1, 2, 4, 8, 16, 32):
                    raise ValueError
            except ValueError as exc:
                raise ValueError("拍号格式示例为 4/4 或 6/8。分子须在 1 到 32 之间，分母须为 1、2、4、8、16 或 32。") from exc
            meter = f"{numerator}/{denominator}"
        return {
            "mode": MODES[self.mode.get()], "tempo_path": tempo_path, "bpm": bpm,
            "meter": meter, "beat_scale": TEMPO_SCALES[self.scale.get()],
        }

    def _run(self):
        if self.busy:
            return
        try:
            input_text = self.audio.get().strip()
            input_path = Path(input_text).expanduser().resolve()
            if not input_text or not input_path.is_file():
                raise ValueError("请选择有效的音频文件。")
            instruments = [name for name, enabled in (("piano", self.piano), ("guitar", self.guitar)) if enabled.get()]
            if not instruments:
                raise ValueError("请至少选择一种乐器。")
            output_root = Path(self.output.get().strip() or ROOT / "outputs").expanduser().resolve()
            options = self._options()
        except (OSError, ValueError) as exc:
            messagebox.showerror("请检查设置", str(exc), parent=self.window)
            return
        keep_stems = self.keep_stems.get()

        def run(log):
            from . import pipeline

            try:
                config = pipeline.load_config()
            except json.JSONDecodeError as exc:
                raise ValueError(f"config.json 格式有误，请检查第 {exc.lineno} 行第 {exc.colno} 列。") from exc
            if not isinstance(config, dict):
                raise ValueError("config.json 必须包含一个 JSON 对象。")
            return pipeline.run(input_path, output_root, instruments, config=config, keep_stems=keep_stems,
                                log=log, **options)

        self._start(run, output_root, "正在转谱…", f"开始转谱：{input_path}")

    def _reexport(self):
        if self.busy or self.project is None:
            return
        try:
            options = self._options()
        except (OSError, ValueError) as exc:
            messagebox.showerror("请检查设置", str(exc), parent=self.window)
            return
        project = self.project

        def reexport(log):
            from . import pipeline

            return pipeline.reexport(project, log=log, **options)

        self._start(reexport, project.parent, "正在重导出…", f"从缓存重导出：{project}")

    def _start(self, work, log_root, status, description):
        if self.busy:
            return
        self.busy = True
        self.status.set(status)
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        scale_at_start = self.scale.get()
        self.log_path = None
        self.log_button.configure(state="disabled")
        self._refresh_buttons()

        def worker():
            trace_logged = False
            try:
                folder = log_root / "_logs"
                folder.mkdir(parents=True, exist_ok=True)
                log_path = folder / f"{datetime.now():%Y-%m-%d_%H%M%S_%f}.log"
                with log_path.open("x", encoding="utf-8", buffering=1) as stream:
                    self.events.put(("log_path", log_path))

                    def log(message):
                        line = f"[{datetime.now():%H:%M:%S}] {message}"
                        stream.write(line + "\n")
                        self.events.put(("log", line))

                    try:
                        log(description)
                        result = Path(work(log)).resolve()
                        log(f"完成：{result}")
                    except Exception:
                        log(traceback.format_exc().rstrip())
                        trace_logged = True
                        raise
            except Exception as exc:
                if not trace_logged:
                    self.events.put(("log", traceback.format_exc().rstrip()))
                self.events.put(("error", str(exc) or type(exc).__name__))
            else:
                self.events.put(("done", (result, scale_at_start)))

        threading.Thread(target=worker, name="astra-pipeline").start()

    def _poll(self):
        for _ in range(100):
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                self.log_text.configure(state="normal")
                self.log_text.insert("end", value + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            elif event == "log_path":
                self.log_path = value
                self.log_button.configure(state="normal")
            else:
                self.busy = False
                self.progress.stop()
                self.progress.configure(mode="determinate", value=100 if event == "done" else 0)
                if event == "done":
                    project, scale_at_start = value
                    if self.scale.get() == scale_at_start:
                        self.scale.set(next(iter(TEMPO_SCALES)))
                    self._show_project(project)
                    self.status.set("处理完成，可打开项目结果。")
                else:
                    self.status.set("处理失败，详情见运行日志。")
                    self._refresh_buttons()
                    messagebox.showerror("处理失败", value, parent=self.window)
        self.window.after(100, self._poll)

    def _show_project(self, project):
        self.project = project
        title = project.name
        self.project_label.set(title if len(title) <= 44 else title[:43] + "…")
        try:
            state = json.loads((project / "project.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            state = {}
        latest = state.get("latest_export") if isinstance(state, dict) else None
        export = project / "exports" / Path(latest).name if isinstance(latest, str) and latest else None
        self.result_dir = export if export and export.is_dir() else project
        self._show_report(self.result_dir / "report.json")
        outputs = list(self.result_dir.glob("*"))
        if self.result_dir != project:
            outputs.extend(path for path in project.glob("*") if OUTPUT_TYPES.get(path.suffix.lower()) == "音频")
        self.results.delete(0, "end")
        self.result_files = sorted(
            (path for path in outputs if path.suffix.lower() in OUTPUT_TYPES and path.is_file()),
            key=lambda path: (OUTPUT_TYPES[path.suffix.lower()] == "音频", str(path).casefold()),
        )
        for path in self.result_files:
            kind = OUTPUT_TYPES[path.suffix.lower()]
            self.results.insert("end", f"{kind}   {path.name}")
        self._refresh_buttons()

    def _show_report(self, path):
        self.report_row.pack_forget()
        self.report_path = None
        self.report_summary.set("")
        try:
            report = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if not isinstance(report, dict) or not isinstance(report.get("instruments"), dict):
            return
        stats = {instrument: values for instrument, values in report["instruments"].items() if isinstance(values, dict)}

        def count(key):
            values = [item.get(key) for item in stats.values()]
            return str(sum(values)) if values and all(type(value) is int for value in values) else "未统计"

        instrument_names = {"piano": "钢琴", "guitar": "吉他"}
        notes = " · ".join(f"{instrument_names.get(key, key)} {item.get('notes', '—')} 音符" for key, item in stats.items())
        warnings = report.get("warnings")
        warning_count = str(len(warnings)) if isinstance(warnings, list) else "未统计"
        shifts = f"量化偏移 >80 ms  起点 {count('large_quantization_shifts')} / 终点 {count('large_quantization_end_shifts')}"
        self.report_summary.set("｜".join(part for part in (notes, shifts, f"警告 {warning_count}") if part))
        self.report_path = path
        self.report_row.pack(fill="x", before=self.results_body, pady=(0, 5))

    def _refresh_buttons(self):
        ready = "disabled" if self.busy else "normal"
        self.run_button.configure(state=ready)
        self.choose_project_button.configure(state=ready)
        self.export_button.configure(state=ready if self.project else "disabled")
        self.correct_button.configure(state=ready if self.project else "disabled")
        self.folder_button.configure(state="normal" if self.project else "disabled")
        self._selection_changed()

    def _selection_changed(self, _event=None):
        self.file_button.configure(state="normal" if self.results.curselection() else "disabled")

    def _open_selected(self, _event=None):
        selected = self.results.curselection()
        if selected:
            self._open(self.result_files[selected[0]])

    def _open(self, path):
        if path is not None:
            try:
                os.startfile(str(path))
            except OSError as exc:
                messagebox.showerror("无法打开", f"{path}\n{exc}", parent=self.window)

    def _edit_tempo(self, use_project=False):
        if self.busy:
            return
        from .pipeline import read_json, save_json
        from .tempo_editor import TempoEditor

        selected = "" if use_project else self.tempo.get().strip()
        source = Path(selected).expanduser() if selected else None
        if source is None and self.project:
            for folder in (self.result_dir, self.project):
                candidate = folder / "tempo_map.json" if folder else None
                if candidate and candidate.is_file():
                    source = candidate
                    break
        template = json.dumps(TEMPO_EXAMPLE, ensure_ascii=False, indent=2)
        try:
            if source is None:
                content = template
            elif source.suffix.lower() in (".mid", ".midi"):
                from .midi import import_tempo_midi

                duration = read_json(self.project / "project.json")["duration"] if self.project else 1.0
                content = json.dumps(import_tempo_midi(source, duration), ensure_ascii=False, indent=2)
            else:
                content = source.read_text(encoding="utf-8-sig")
        except (EOFError, KeyError, OSError, TypeError, ValueError) as exc:
            messagebox.showerror("无法读取速度图", str(exc), parent=self.window)
            return

        def save(editor, data, export):
            path = filedialog.asksaveasfilename(
                parent=editor, title="保存修订后的速度图", initialfile="tempo_override.json",
                initialdir=self.project or (source.parent if source else None), defaultextension=".json",
                filetypes=[("JSON 文件", "*.json")],
            )
            if not path:
                return
            try:
                path = Path(path).resolve()
                if path.suffix.lower() != ".json":
                    raise ValueError("请使用 .json 文件扩展名。")
                if self.project:
                    project = self.project.resolve()
                    if path == project / "tempo_map.json" or (project / "exports") in path.parents:
                        raise ValueError("请另存为单独的 JSON 文件，保留项目与已有导出的速度图。")
                save_json(path, data)
            except (OSError, ValueError) as exc:
                messagebox.showerror("保存失败", str(exc), parent=editor)
                return
            self.tempo.set(str(path))
            self.bpm.set("")
            editor.destroy()
            if export:
                self.scale.set(next(iter(TEMPO_SCALES)))
                self._reexport()

        source_label = source.name if selected and source else (
            "项目速度图" if source else "120 BPM、4/4 拍示例"
        )
        TempoEditor(self.window, content, template, source_label, save, self.project is not None)

    def _show_help(self):
        help_window = tk.Toplevel(self.window)
        help_window.title("帮助与安装")
        help_window.transient(self.window)
        frame = ttk.Frame(help_window, padding=16)
        frame.pack(fill="both", expand=True)
        command = r".\.venv\Scripts\python.exe -m astra.cli doctor"
        ttk.Label(
            frame, wraplength=420,
            text="在项目目录的 PowerShell 中运行下方命令，查看分轨、节拍、转录模型和内置谱面渲染状态。",
        ).pack(anchor="w")
        ttk.Label(frame, text=command).pack(anchor="w", pady=8)

        def copy_command():
            help_window.clipboard_clear()
            help_window.clipboard_append(command)

        ttk.Button(frame, text="复制命令", command=copy_command).pack(side="left")
        ttk.Button(frame, text="打开模型安装说明", command=lambda: self._open(ROOT / "docs" / "MODELS.md")).pack(
            side="left", padx=8,
        )

    def _close(self):
        if self.busy:
            messagebox.showinfo("任务运行中", "请等待处理完成后关闭窗口。", parent=self.window)
        else:
            self.window.destroy()


def main():
    window = tk.Tk()
    AstraApp(window)
    window.mainloop()


if __name__ == "__main__":
    main()
