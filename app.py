"""PDF Compressor - desktop GUI (Tkinter)."""

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from compressor import LOSSLESS, PRESETS, compress_pdf, human_size

APP_NAME = "PDF Compressor"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("800x540")
        self.minsize(760, 440)
        self.files = []
        self.events = queue.Queue()
        self.worker = None

        self._build_ui()
        self.after(100, self._poll_events)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="Add PDFs…", command=self.add_files).pack(side="left")
        ttk.Button(top, text="Add Folder…", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(top, text="Remove", command=self.remove_selected).pack(side="left")
        ttk.Button(top, text="Clear", command=self.clear_files).pack(side="left", padx=4)

        cols = ("file", "original", "compressed", "saved")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=10)
        for c, w, title in (("file", 330, "File"), ("original", 100, "Original"),
                            ("compressed", 100, "Compressed"), ("saved", 90, "Saved")):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor="w" if c == "file" else "e")
        self.tree.pack(fill="both", expand=True, **pad)

        opts = ttk.LabelFrame(self, text="Settings")
        opts.pack(fill="x", **pad)

        ttk.Label(opts, text="Compression:").grid(row=0, column=0, sticky="w", **pad)
        self.preset = tk.StringVar(value=LOSSLESS)
        ttk.Combobox(opts, textvariable=self.preset, values=list(PRESETS),
                     state="readonly", width=28).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(opts, text="Mode:").grid(row=0, column=2, sticky="w", **pad)
        self.mode = tk.StringVar(value="smart")
        ttk.Radiobutton(opts, text="Smart (keeps text)", value="smart",
                        variable=self.mode).grid(row=0, column=3, sticky="w")
        ttk.Radiobutton(opts, text="Max (image only)", value="rasterize",
                        variable=self.mode).grid(row=0, column=4, sticky="w")

        self.gray = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Convert to grayscale (smaller)",
                        variable=self.gray).grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(opts, text="Lossless keeps every image exactly as-is; other presets trade quality for size.",
                  foreground="gray").grid(row=4, column=1, columnspan=4, sticky="w", padx=8, pady=(0, 4))

        ttk.Label(opts, text="Save to:").grid(row=2, column=0, sticky="w", **pad)
        self.outdir = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.outdir, width=50).grid(
            row=2, column=1, columnspan=3, sticky="we", **pad)
        ttk.Button(opts, text="Browse…", command=self.pick_outdir).grid(row=2, column=4, sticky="w")
        ttk.Label(opts, text="(empty = same folder as original, name_compressed.pdf)",
                  foreground="gray").grid(row=3, column=1, columnspan=4, sticky="w", padx=8)
        opts.columnconfigure(3, weight=1)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.start_btn = ttk.Button(bottom, text="Compress", command=self.start)
        self.start_btn.pack(side="left", padx=8)

        self.status = tk.StringVar(value="Add PDF files to begin.")
        ttk.Label(self, textvariable=self.status).pack(fill="x", padx=8, pady=(0, 8))

    # ---------- file list ----------
    def _add(self, paths):
        for p in paths:
            p = os.path.abspath(p)
            if p.lower().endswith(".pdf") and p not in self.files:
                self.files.append(p)
                self.tree.insert("", "end", iid=p, values=(os.path.basename(p), "", "", ""))
        self.status.set(f"{len(self.files)} file(s) ready.")

    def add_files(self):
        self._add(filedialog.askopenfilenames(filetypes=[("PDF files", "*.pdf")]))

    def add_folder(self):
        d = filedialog.askdirectory()
        if d:
            self._add(os.path.join(d, f) for f in sorted(os.listdir(d)))

    def remove_selected(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.files.remove(iid)

    def clear_files(self):
        self.tree.delete(*self.tree.get_children())
        self.files.clear()

    def pick_outdir(self):
        d = filedialog.askdirectory()
        if d:
            self.outdir.set(d)

    # ---------- work ----------
    def _dst_for(self, src):
        base, _ = os.path.splitext(os.path.basename(src))
        folder = self.outdir.get().strip() or os.path.dirname(src)
        return os.path.join(folder, f"{base}_compressed.pdf")

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showinfo(APP_NAME, "Add at least one PDF first.")
            return
        out = self.outdir.get().strip()
        if out and not os.path.isdir(out):
            messagebox.showerror(APP_NAME, "Output folder does not exist.")
            return
        self.start_btn.config(state="disabled")
        jobs = [(f, self._dst_for(f)) for f in self.files]
        args = (jobs, self.preset.get(), self.mode.get(), self.gray.get())
        self.worker = threading.Thread(target=self._run, args=args, daemon=True)
        self.worker.start()

    def _run(self, jobs, preset, mode, gray):
        total_old = total_new = 0
        n = len(jobs)
        for idx, (src, dst) in enumerate(jobs):
            self.events.put(("status", f"Compressing {os.path.basename(src)} ({idx + 1}/{n})…"))

            def prog(done, tot, idx=idx):
                self.events.put(("progress", (idx + done / tot) / n * 100))

            try:
                old, new = compress_pdf(src, dst, preset, mode, gray, prog)
                total_old += old
                total_new += new
                self.events.put(("row", src, old, new))
            except Exception as e:  # keep going with remaining files
                self.events.put(("error", src, str(e)))
        self.events.put(("done", total_old, total_new))

    def _poll_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self.status.set(ev[1])
                elif kind == "progress":
                    self.progress["value"] = ev[1]
                elif kind == "row":
                    _, src, old, new = ev
                    pct = (1 - new / old) * 100 if old else 0
                    self.tree.item(src, values=(os.path.basename(src), human_size(old),
                                                human_size(new), f"{pct:.0f}%"))
                elif kind == "error":
                    _, src, msg = ev
                    self.tree.item(src, values=(os.path.basename(src), "", "ERROR", msg[:40]))
                elif kind == "done":
                    _, old, new = ev
                    self.progress["value"] = 100
                    self.start_btn.config(state="normal")
                    if old:
                        self.status.set(f"Done. {human_size(old)} → {human_size(new)} "
                                        f"({(1 - new / old) * 100:.0f}% smaller)")
                    else:
                        self.status.set("Done (no files compressed).")
        except queue.Empty:
            pass
        self.after(100, self._poll_events)


if __name__ == "__main__":
    App().mainloop()
