"""Manual live-test desktop: run inside the selected Linux graphical session.

python3 textbox_linux.py /private/test-directory/result.json
Not started by the default synthetic test suite. Text is intentionally recorded.
"""
import json
import os
from pathlib import Path
import sys
import time
import tkinter as tk

result_path = Path(sys.argv[1])
root = tk.Tk()
root.title("VMware VNC Unicode verification")
root.geometry("1000x650+50+50")
box = tk.Text(root, font=("Noto Sans CJK SC", 18), undo=True, wrap="word", insertontime=0)
box.pack(fill="both", expand=True)


def save(event=None):
    if event is not None and not box.edit_modified():
        return
    text = box.get("1.0", "end-1c")
    result_path.write_text(json.dumps({"text": text, "length": len(text),
                                      "process_id": os.getpid(), "updated": time.time()}), encoding="utf-8")
    box.edit_modified(False)


def select_all(event):
    box.tag_add("sel", "1.0", "end-1c")
    return "break"


box.bind("<<Modified>>", save)
box.bind("<Control-a>", select_all)
root.after(100, box.focus_force)
save()
root.mainloop()
