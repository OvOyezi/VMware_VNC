"""Manual live-input fixture for the selected Linux graphical guest.

Usage: python3 input_linux.py RESULT_PATH TOKEN

Gtk 3 / PyGObject is preferred when already installed. Tkinter is used as a
fallback. This fixture is not started by the normal synthetic checks.
"""

import json
import os
import sys


def _load_gtk3():
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk

        return Gtk, Gdk
    except (ImportError, ValueError):
        return None


class GtkFixture:
    WIDTH = 1000
    HEIGHT = 650

    def __init__(self, Gtk, Gdk, result_path, token):
        self.Gtk = Gtk
        self.Gdk = Gdk
        self.result_path = result_path
        self.temporary_path = result_path + "." + str(os.getpid()) + ".tmp"
        self.token = token
        self.counts = {
            "mouse_down": 0,
            "mouse_up": 0,
            "mouse_move": 0,
            "wheel": 0,
            "key_down": 0,
            "key_up": 0,
        }
        self.button_clicks = 0
        self.wheel_delta = 0
        self.color = "green"
        self.drag = None
        self.dragging = False
        self.last_drag_point = None
        self.canvas_xy = [10, 10]

        self.window = Gtk.Window(title="VNC input verification - " + token)
        self.window.set_default_size(self.WIDTH, self.HEIGHT)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.set_keep_above(True)
        self.window.connect("delete-event", self.on_delete)
        self.window.connect("configure-event", self.on_window_configure)

        grid = Gtk.Grid(column_spacing=18, row_spacing=12, margin=16)
        grid.set_column_homogeneous(False)
        grid.set_hexpand(True)
        grid.set_vexpand(True)
        self.window.add(grid)

        text_scroll = Gtk.ScrolledWindow()
        text_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        text_scroll.set_hexpand(True)
        text_scroll.set_vexpand(True)
        self.text_view = Gtk.TextView()
        self.text_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.text_buffer = self.text_view.get_buffer()
        self.text_buffer.connect("changed", self.on_text_changed)
        text_scroll.add(self.text_view)
        grid.attach(text_scroll, 0, 0, 1, 1)

        right = Gtk.Grid(row_spacing=12)
        right.set_size_request(355, -1)
        right.set_hexpand(False)
        right.set_vexpand(True)
        grid.attach(right, 1, 0, 1, 1)

        self.toggle_button = Gtk.Button(label="Toggle canvas red / green")
        self.toggle_button.connect("clicked", self.on_toggle)
        right.attach(self.toggle_button, 0, 0, 1, 1)

        self.canvas_host = Gtk.Fixed()
        self.canvas_host.set_size_request(355, 205)
        self.canvas_host.set_hexpand(True)
        self.canvas_host.set_vexpand(False)
        right.attach(self.canvas_host, 0, 1, 1, 1)
        self.canvas = Gtk.EventBox()
        self.canvas.set_size_request(330, 180)
        self.canvas.set_visible_window(True)
        self.canvas.get_style_context().add_class("vnc-input-canvas")
        self.canvas_css_provider = Gtk.CssProvider()
        self.canvas.get_style_context().add_provider(
            self.canvas_css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.canvas_label = Gtk.Label(label="DRAG THIS CANVAS")
        self.canvas_label.set_halign(Gtk.Align.START)
        self.canvas_label.set_valign(Gtk.Align.START)
        self.canvas_label.set_margin_start(24)
        self.canvas_label.set_margin_top(22)
        self.canvas_label.get_style_context().add_class("vnc-input-canvas-label")
        self.canvas_label.get_style_context().add_provider(
            self.canvas_css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.canvas.add(self.canvas_label)
        self._update_canvas_style()
        self.canvas.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.KEY_PRESS_MASK
            | Gdk.EventMask.KEY_RELEASE_MASK
        )
        self.canvas.connect("size-allocate", self.on_canvas_allocate)
        self.canvas_host.put(self.canvas, self.canvas_xy[0], self.canvas_xy[1])

        wheel_scroll = Gtk.ScrolledWindow()
        wheel_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        wheel_scroll.set_hexpand(True)
        wheel_scroll.set_vexpand(True)
        self.wheel_view = Gtk.TextView()
        self.wheel_view.set_editable(False)
        self.wheel_view.set_cursor_visible(False)
        self.wheel_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.wheel_view.get_buffer().set_text(
            "\n".join("Scrollable wheel test line {:02d}".format(i) for i in range(1, 81))
        )
        wheel_scroll.add(self.wheel_view)
        right.attach(wheel_scroll, 0, 2, 1, 1)

        self._tracked_widgets = [
            self.text_view,
            self.toggle_button,
            self.canvas,
            self.wheel_view,
        ]
        for widget in self._tracked_widgets:
            widget.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK
                | Gdk.EventMask.BUTTON_RELEASE_MASK
                | Gdk.EventMask.POINTER_MOTION_MASK
                | Gdk.EventMask.SCROLL_MASK
                | Gdk.EventMask.KEY_PRESS_MASK
                | Gdk.EventMask.KEY_RELEASE_MASK
            )
            widget.connect("button-press-event", self.on_mouse_down)
            widget.connect("button-release-event", self.on_mouse_up)
            widget.connect("motion-notify-event", self.on_mouse_move)
            widget.connect("key-press-event", self.on_key_down)
            widget.connect("key-release-event", self.on_key_up)
        self.text_view.connect("key-press-event", self.on_text_key_down)
        self.wheel_view.connect("scroll-event", self.on_scroll)

        self.window.show_all()
        Gtk.main_iteration_do(False)
        self.write_report()

    def _read_main_text(self):
        start, end = self.text_buffer.get_bounds()
        return self.text_buffer.get_text(start, end, True)

    def _control_bounds(self, widget):
        if widget is self.canvas:
            host_bounds = self._control_bounds(self.canvas_host)
            return {
                "x": host_bounds["x"] + self.canvas_xy[0],
                "y": host_bounds["y"] + self.canvas_xy[1],
                "width": int(widget.get_allocated_width()),
                "height": int(widget.get_allocated_height()),
            }
        width = int(widget.get_allocated_width())
        height = int(widget.get_allocated_height())
        top = widget.get_toplevel()
        if not widget.get_realized() or top is None or top.get_window() is None:
            return {"x": 0, "y": 0, "width": width, "height": height}

        translated = widget.translate_coordinates(top, 0, 0)
        if translated is None:
            local_x, local_y = 0, 0
        elif len(translated) == 3:
            if not translated[0]:
                local_x, local_y = 0, 0
            else:
                local_x, local_y = translated[1], translated[2]
        else:
            local_x, local_y = translated

        origin = top.get_window().get_origin()
        # PyGObject can expose gboolean as bool or int; the final two
        # fields are the native origin in both binding variants.
        origin_x, origin_y = origin[-2:]
        return {
            "x": int(origin_x + local_x),
            "y": int(origin_y + local_y),
            "width": width,
            "height": height,
        }

    def _report(self):
        return {
            "schema_version": 1,
            "token": self.token,
            "process_id": os.getpid(),
            "text": self._read_main_text(),
            "color": self.color,
            "button_clicks": self.button_clicks,
            "counts": dict(self.counts),
            "wheel_delta": self.wheel_delta,
            "drag": self.drag,
            "controls": {
                "text": self._control_bounds(self.text_view),
                "button": self._control_bounds(self.toggle_button),
                "canvas": self._control_bounds(self.canvas),
                "wheel": self._control_bounds(self.wheel_view),
            },
        }

    def write_report(self):
        data = json.dumps(self._report(), ensure_ascii=False, separators=(",", ":"))
        with open(self.temporary_path, "w", encoding="utf-8", newline="") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(self.temporary_path, self.result_path)

    def on_text_changed(self, _buffer):
        self.write_report()

    def on_toggle(self, _button):
        self.button_clicks += 1
        self.color = "red" if self.color == "green" else "green"
        self._update_canvas_style()
        self.write_report()

    def _update_canvas_style(self):
        background = (
            "rgb(20, 107, 46)" if self.color == "green" else "rgb(184, 20, 26)"
        )
        css = (
            ".vnc-input-canvas { background-color: " + background + "; }\n"
            ".vnc-input-canvas-label { color: #ffffff; }"
        )
        self.canvas_css_provider.load_from_data(css.encode("utf-8"))
        self.canvas.queue_draw()

    def on_canvas_allocate(self, _widget, _allocation):
        self.write_report()

    @staticmethod
    def _root_point(event):
        return int(round(event.x_root)), int(round(event.y_root))

    def on_mouse_down(self, widget, event):
        self.counts["mouse_down"] += 1
        if widget is self.canvas and event.button == 1:
            point = self._root_point(event)
            self.dragging = True
            self.last_drag_point = point
            self.drag = {"start": {"x": point[0], "y": point[1]}, "end": None}
        self.write_report()
        return False

    def on_mouse_up(self, widget, event):
        self.counts["mouse_up"] += 1
        if widget is self.canvas and self.dragging:
            point = self._root_point(event)
            self.drag["end"] = {"x": point[0], "y": point[1]}
            self.dragging = False
            self.last_drag_point = None
        self.write_report()
        return False

    def on_mouse_move(self, widget, event):
        self.counts["mouse_move"] += 1
        if widget is self.canvas and self.dragging:
            point = self._root_point(event)
            delta_x = point[0] - self.last_drag_point[0]
            delta_y = point[1] - self.last_drag_point[1]
            allocation = self.canvas_host.get_allocation()
            max_x = max(0, allocation.width - self.canvas.get_allocated_width())
            max_y = max(0, allocation.height - self.canvas.get_allocated_height())
            self.canvas_xy[0] = max(0, min(max_x, self.canvas_xy[0] + delta_x))
            self.canvas_xy[1] = max(0, min(max_y, self.canvas_xy[1] + delta_y))
            self.canvas_host.move(self.canvas, self.canvas_xy[0], self.canvas_xy[1])
            self.last_drag_point = point
        self.write_report()
        return False

    def on_scroll(self, _widget, event):
        self.counts["wheel"] += 1
        delta = 0
        if event.direction == self.Gdk.ScrollDirection.UP:
            delta = 120
        elif event.direction == self.Gdk.ScrollDirection.DOWN:
            delta = -120
        elif event.direction == self.Gdk.ScrollDirection.LEFT:
            delta = -120
        elif event.direction == self.Gdk.ScrollDirection.RIGHT:
            delta = 120
        elif event.direction == self.Gdk.ScrollDirection.SMOOTH:
            _smooth, delta_x, delta_y = event.get_scroll_deltas()
            movement = delta_y if delta_y else delta_x
            delta = int(round(-movement * 120))
        self.wheel_delta += delta
        self.write_report()
        return False

    def on_key_down(self, _widget, _event):
        self.counts["key_down"] += 1
        self.write_report()
        return False

    def on_key_up(self, _widget, _event):
        self.counts["key_up"] += 1
        self.write_report()
        return False

    def on_text_key_down(self, _widget, event):
        is_control = bool(event.state & self.Gdk.ModifierType.CONTROL_MASK)
        if is_control and event.keyval in (self.Gdk.KEY_a, self.Gdk.KEY_A):
            self.text_buffer.select_range(
                self.text_buffer.get_end_iter(), self.text_buffer.get_start_iter()
            )
            return True
        return False

    def on_window_configure(self, _window, _event):
        self.write_report()
        return False

    def on_delete(self, _window, _event):
        self.write_report()
        self.Gtk.main_quit()
        return False


class TkFixture:
    WIDTH = 1000
    HEIGHT = 650

    def __init__(self, tk, ttk, result_path, token):
        self.tk = tk
        self.ttk = ttk
        self.result_path = result_path
        self.temporary_path = result_path + "." + str(os.getpid()) + ".tmp"
        self.token = token
        self.counts = {
            "mouse_down": 0,
            "mouse_up": 0,
            "mouse_move": 0,
            "wheel": 0,
            "key_down": 0,
            "key_up": 0,
        }
        self.button_clicks = 0
        self.wheel_delta = 0
        self.color = "green"
        self.drag = None
        self.dragging = False
        self.drag_offset = (0, 0)
        self.canvas_xy = [8, 8]

        self.root = tk.Tk()
        self.root.title("VNC input verification - " + token)
        self.root.geometry("1000x650")
        self.root.minsize(1000, 650)
        self.root.attributes("-topmost", True)
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)

        outer = ttk.Frame(self.root, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=3, minsize=560)
        outer.columnconfigure(1, weight=2, minsize=350)

        text_frame = ttk.Frame(outer)
        text_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self.text_box = tk.Text(
            text_frame, wrap="word", undo=True, font=("TkDefaultFont", 14)
        )
        self.text_box.grid(row=0, column=0, sticky="nsew")
        text_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.text_box.yview)
        text_scroll.grid(row=0, column=1, sticky="ns")
        self.text_box.configure(yscrollcommand=text_scroll.set)
        self.text_box.bind("<<Modified>>", self.on_text_modified, add="+")

        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)
        self.toggle_button = ttk.Button(
            right, text="Toggle canvas red / green", command=self.on_toggle
        )
        self.toggle_button.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        self.canvas_host = tk.Frame(right, width=360, height=220, bd=1, relief="solid")
        self.canvas_host.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.canvas_host.grid_propagate(False)
        self.canvas = tk.Canvas(
            self.canvas_host,
            background=self.color,
            highlightthickness=1,
            highlightbackground="#303030",
        )
        self.canvas.place(x=self.canvas_xy[0], y=self.canvas_xy[1], width=330, height=180)
        self.canvas.bind("<Configure>", self.on_canvas_configure, add="+")
        self.canvas.create_text(165, 90, text="DRAG THIS CANVAS", fill="white", font=("TkDefaultFont", 15))

        wheel_frame = ttk.Frame(right)
        wheel_frame.grid(row=2, column=0, sticky="nsew")
        wheel_frame.rowconfigure(0, weight=1)
        wheel_frame.columnconfigure(0, weight=1)
        self.wheel_list = tk.Listbox(wheel_frame, height=14, font=("TkDefaultFont", 11))
        self.wheel_list.grid(row=0, column=0, sticky="nsew")
        wheel_scroll = ttk.Scrollbar(wheel_frame, orient="vertical", command=self.wheel_list.yview)
        wheel_scroll.grid(row=0, column=1, sticky="ns")
        self.wheel_list.configure(yscrollcommand=wheel_scroll.set)
        for line in range(1, 81):
            self.wheel_list.insert("end", "Scrollable wheel test line {:02d}".format(line))

        self._tracked_widgets = [
            self.text_box,
            self.toggle_button,
            self.canvas,
            self.wheel_list,
            text_scroll,
            wheel_scroll,
        ]
        for widget in self._tracked_widgets:
            for button_number in (1, 2, 3):
                widget.bind(
                    "<ButtonPress-{}>".format(button_number),
                    self.on_mouse_down,
                    add="+",
                )
                widget.bind(
                    "<ButtonRelease-{}>".format(button_number),
                    self.on_mouse_up,
                    add="+",
                )
            widget.bind("<Motion>", self.on_mouse_move, add="+")
            widget.bind("<KeyPress>", self.on_key_down, add="+")
            widget.bind("<KeyRelease>", self.on_key_up, add="+")
        self.wheel_list.bind("<Button-4>", self.on_wheel, add="+")
        self.wheel_list.bind("<Button-5>", self.on_wheel, add="+")
        self.wheel_list.bind("<MouseWheel>", self.on_wheel, add="+")
        self.root.bind("<Configure>", self.on_root_configure, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = max(0, (screen_width - self.WIDTH) // 2)
        y = max(0, (screen_height - self.HEIGHT) // 2)
        self.root.geometry("{}x{}+{}+{}".format(self.WIDTH, self.HEIGHT, x, y))
        self.root.after_idle(self.write_report)

    def _text_value(self):
        return self.text_box.get("1.0", "end-1c")

    @staticmethod
    def _bounds(widget):
        return {
            "x": int(widget.winfo_rootx()),
            "y": int(widget.winfo_rooty()),
            "width": int(widget.winfo_width()),
            "height": int(widget.winfo_height()),
        }

    def _report(self):
        return {
            "schema_version": 1,
            "token": self.token,
            "process_id": os.getpid(),
            "text": self._text_value(),
            "color": self.color,
            "button_clicks": self.button_clicks,
            "counts": dict(self.counts),
            "wheel_delta": self.wheel_delta,
            "drag": self.drag,
            "controls": {
                "text": self._bounds(self.text_box),
                "button": self._bounds(self.toggle_button),
                "canvas": self._bounds(self.canvas),
                "wheel": self._bounds(self.wheel_list),
            },
        }

    def write_report(self):
        data = json.dumps(self._report(), ensure_ascii=False, separators=(",", ":"))
        with open(self.temporary_path, "w", encoding="utf-8", newline="") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(self.temporary_path, self.result_path)

    def on_text_modified(self, _event):
        if self.text_box.edit_modified():
            self.text_box.edit_modified(False)
            self.write_report()

    def on_toggle(self):
        self.button_clicks += 1
        self.color = "red" if self.color == "green" else "green"
        self.canvas.configure(background=self.color)
        self.write_report()

    def on_mouse_down(self, event):
        if event.num not in (4, 5):
            self.counts["mouse_down"] += 1
            if event.widget is self.canvas and event.num == 1:
                point = (int(event.x_root), int(event.y_root))
                self.dragging = True
                self.drag_offset = (
                    point[0] - self.canvas.winfo_rootx(),
                    point[1] - self.canvas.winfo_rooty(),
                )
                self.drag = {"start": {"x": point[0], "y": point[1]}, "end": None}
        self.write_report()

    def on_mouse_up(self, event):
        if event.num not in (4, 5):
            self.counts["mouse_up"] += 1
            if self.dragging:
                self.drag["end"] = {"x": int(event.x_root), "y": int(event.y_root)}
                self.dragging = False
        self.write_report()

    def on_mouse_move(self, event):
        self.counts["mouse_move"] += 1
        if self.dragging:
            host_x = self.canvas_host.winfo_rootx()
            host_y = self.canvas_host.winfo_rooty()
            max_x = max(0, self.canvas_host.winfo_width() - self.canvas.winfo_width())
            max_y = max(0, self.canvas_host.winfo_height() - self.canvas.winfo_height())
            x = max(0, min(max_x, int(event.x_root) - self.drag_offset[0] - host_x))
            y = max(0, min(max_y, int(event.y_root) - self.drag_offset[1] - host_y))
            self.canvas_xy = [x, y]
            self.canvas.place_configure(x=x, y=y)
        self.write_report()

    def on_wheel(self, event):
        self.counts["wheel"] += 1
        if getattr(event, "num", None) == 4:
            delta = 120
        elif getattr(event, "num", None) == 5:
            delta = -120
        else:
            delta = int(getattr(event, "delta", 0))
        self.wheel_delta += delta
        self.write_report()

    def on_key_down(self, event):
        self.counts["key_down"] += 1
        self.write_report()
        if (
            event.widget is self.text_box
            and event.keysym.lower() == "a"
            and (event.state & 0x4)
        ):
            self.text_box.tag_add("sel", "1.0", "end-1c")
            self.text_box.mark_set("insert", "end-1c")
            self.text_box.see("insert")
            return "break"
        return None

    def on_key_up(self, _event):
        self.counts["key_up"] += 1
        self.write_report()

    def on_root_configure(self, event):
        if event.widget is self.root:
            self.write_report()

    def on_canvas_configure(self, _event):
        self.write_report()

    def on_close(self):
        self.write_report()
        self.root.destroy()


def main(argv=None):
    if argv is None:
        argv = sys.argv
    if len(argv) != 3 or not argv[2]:
        print("Usage: python3 input_linux.py RESULT_PATH TOKEN", file=sys.stderr)
        return 2

    result_path = os.path.abspath(argv[1])
    token = argv[2]
    gtk = _load_gtk3()
    if gtk is not None:
        GtkFixture(gtk[0], gtk[1], result_path, token)
        gtk[0].main()
        return 0

    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as error:
        print("Gtk 3/PyGObject is unavailable and tkinter could not be imported: {}".format(error), file=sys.stderr)
        return 1

    fixture = TkFixture(tk, ttk, result_path, token)
    fixture.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
