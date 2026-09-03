#!/usr/bin/env python3
"""
Settings GUI for the Risemode smart screen panel: pick a background image
(or default to the live desktop wallpaper) and toggle which sensors are
shown, with a live preview of exactly what panel_render.py would send to
the panel.

Apply just writes panel_render.CONFIG_PATH - the running risemode-screen
service picks it up on its very next frame (get_config() is cached by
mtime and reloads automatically), no restart needed.
"""
import os
import tkinter as tk
from tkinter import colorchooser, filedialog, ttk

from PIL import ImageTk

import panel_render as pr

PREVIEW_HEIGHT = 900  # initial size; the preview pane resizes with the window
PREVIEW_WIDTH = round(pr.WIDTH * PREVIEW_HEIGHT / pr.HEIGHT)
PREVIEW_REFRESH_MS = 1000
BASE_FONT_SIZE = 13
FRAME_PADDING = 18

# Matches install.sh's StartupWMClass= so a dock/taskbar can associate the
# running window with the .desktop entry (and its icon) instead of falling
# back to a generic one.
WM_CLASS = "risemode-settings"
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")


class SettingsApp:
    def __init__(self, root):
        self.root = root
        root.title("Risemode Smart Screen Settings")
        root.geometry("1300x900")

        style = ttk.Style()
        default_font = ("TkDefaultFont", BASE_FONT_SIZE)
        style.configure(".", font=default_font)
        style.configure("TLabelframe.Label", font=("TkDefaultFont", BASE_FONT_SIZE, "bold"))
        style.configure("Heading.TLabel", font=("TkDefaultFont", BASE_FONT_SIZE, "bold"))

        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        config = pr.get_config()

        controls = ttk.Frame(root, padding=18)
        controls.grid(row=0, column=0, sticky="n")
        self.controls = controls

        preview_frame = ttk.Frame(root, padding=FRAME_PADDING)
        preview_frame.grid(row=0, column=1, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(1, weight=1)

        # --- Background ---
        wp_frame = ttk.LabelFrame(controls, text="Background", padding=12)
        wp_frame.pack(fill="x", pady=(0, 16))

        self.wp_mode = tk.StringVar(
            value="custom" if config["wallpaper"] else "desktop"
        )
        self.wp_path = tk.StringVar(value=config["wallpaper"] or "")

        ttk.Radiobutton(
            wp_frame, text="Desktop wallpaper (auto-updates)",
            variable=self.wp_mode, value="desktop", command=self._sync_wp_state,
        ).pack(anchor="w", pady=3)

        custom_row = ttk.Frame(wp_frame)
        custom_row.pack(fill="x", pady=(6, 0))
        ttk.Radiobutton(
            custom_row, text="Custom image:", variable=self.wp_mode,
            value="custom", command=self._sync_wp_state,
        ).pack(side="left")
        self.wp_entry = ttk.Entry(custom_row, textvariable=self.wp_path, width=28)
        self.wp_entry.pack(side="left", padx=6, fill="x", expand=True)
        self.wp_browse = ttk.Button(custom_row, text="Browse...", command=self._browse)
        self.wp_browse.pack(side="left")
        self._sync_wp_state()

        # --- Sensors ---
        sensors_frame = ttk.LabelFrame(controls, text="Sensors", padding=12)
        sensors_frame.pack(fill="x", pady=(0, 16))

        self.sensor_vars = {}
        for key, label in pr.SENSOR_LABELS.items():
            var = tk.BooleanVar(value=config["sensors"].get(key, True))
            self.sensor_vars[key] = var
            ttk.Checkbutton(sensors_frame, text=label, variable=var).pack(anchor="w", pady=3)

        # --- Colors ---
        colors_frame = ttk.LabelFrame(controls, text="Colors", padding=12)
        colors_frame.pack(fill="x", pady=(0, 16))

        self.color_mode = tk.StringVar(value=config.get("color_mode", "custom"))
        mode_row = ttk.Frame(colors_frame)
        mode_row.pack(fill="x", pady=(0, 10))
        for mode, label in pr.COLOR_MODE_LABELS.items():
            ttk.Radiobutton(
                mode_row, text=label, variable=self.color_mode, value=mode,
                command=self._sync_color_mode_state,
            ).pack(anchor="w")

        self.colors = {k: list(config["colors"][k]) for k in pr.COLOR_LABELS}
        self.color_buttons = {}
        for key, label in pr.COLOR_LABELS.items():
            row = ttk.Frame(colors_frame)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=17).pack(side="left")
            btn = tk.Button(row, width=6, relief="solid", borderwidth=1,
                             command=lambda k=key: self._pick_color(k))
            btn.pack(side="left")
            self.color_buttons[key] = btn
        self._sync_color_mode_state()

        # --- Preview ---
        self.preview_heading = ttk.Label(preview_frame, text="Live preview", style="Heading.TLabel")
        self.preview_heading.grid(row=0, column=0, pady=(0, 8))
        self.preview_label = ttk.Label(preview_frame, anchor="center")
        self.preview_label.grid(row=1, column=0, sticky="nsew")

        # --- Apply --- (below the preview, not the controls column)
        self.apply_row = ttk.Frame(preview_frame)
        self.apply_row.grid(row=2, column=0, pady=(8, 0))
        ttk.Button(self.apply_row, text="Apply", command=self._apply).pack(side="left", ipadx=10, ipady=4)
        self.status = ttk.Label(self.apply_row, text="")
        self.status.pack(side="left", padx=10)

        self._preview_frame = preview_frame
        self._last_pil_img = None
        self._preview_size = (PREVIEW_WIDTH, PREVIEW_HEIGHT)
        preview_frame.bind("<Configure>", lambda event: self._on_preview_resize())

        # The controls column doesn't stretch (weight=0), so it can force
        # the preview column to zero width unless minsize reserves it a
        # usable amount up front - compute this from the controls' actual
        # rendered width rather than guessing a fixed number.
        root.update_idletasks()
        min_w = controls.winfo_reqwidth() + 380
        min_h = max(controls.winfo_reqheight() + 2 * FRAME_PADDING, 560)
        root.minsize(min_w, min_h)

        self._tick_preview()

    def _sync_wp_state(self):
        state = "normal" if self.wp_mode.get() == "custom" else "disabled"
        self.wp_entry.configure(state=state)
        self.wp_browse.configure(state=state)

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Choose background image",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.webp"), ("All files", "*.*")],
        )
        if path:
            self.wp_path.set(path)
            self.wp_mode.set("custom")
            self._sync_wp_state()

    @staticmethod
    def _rgb_to_hex(rgb):
        return "#%02x%02x%02x" % tuple(rgb)

    def _set_color_button(self, key, rgb):
        hexcolor = self._rgb_to_hex(rgb)
        self.color_buttons[key].configure(bg=hexcolor, activebackground=hexcolor)

    def _sync_color_mode_state(self):
        # Swatches always show whichever colors are actually in effect -
        # only editable (and only meaningful to click) in "custom" mode.
        # "auto" is kept live-updated by _tick_preview() instead, since it
        # depends on the current background.
        mode = self.color_mode.get()
        for key in self.color_buttons:
            self.color_buttons[key].configure(state="normal" if mode == "custom" else "disabled")
        if mode == "custom":
            for key in self.color_buttons:
                self._set_color_button(key, self.colors[key])

    def _pick_color(self, key):
        rgb, _hexcolor = colorchooser.askcolor(
            color=self._rgb_to_hex(self.colors[key]),
            title=f"Choose {pr.COLOR_LABELS[key]} color",
        )
        if rgb is not None:
            self.colors[key] = [round(c) for c in rgb]
            self._set_color_button(key, self.colors[key])

    def _config_from_widgets(self):
        wallpaper = self.wp_path.get().strip() if self.wp_mode.get() == "custom" else None
        return {
            "wallpaper": wallpaper or None,
            "sensors": {k: v.get() for k, v in self.sensor_vars.items()},
            "colors": {k: list(v) for k, v in self.colors.items()},
            "color_mode": self.color_mode.get(),
        }

    def _apply(self):
        pr.save_config(self._config_from_widgets())
        self.status.configure(text="Applied - panel updates within a second")
        self.root.after(3000, lambda: self.status.configure(text=""))

    def _on_preview_resize(self):
        # Query actual settled geometry rather than trusting a <Configure>
        # event's payload, which can lag one resize behind mid-drag.
        self.root.update_idletasks()
        # winfo_width/height on a padded ttk.Frame includes its own padding,
        # which is otherwise unavailable to its children - subtract it back
        # out along with the heading label's own height.
        avail_w = max(self._preview_frame.winfo_width() - 2 * FRAME_PADDING, 50)
        heading_h = self.preview_heading.winfo_reqheight() + 8
        apply_h = self.apply_row.winfo_reqheight() + 8
        # Capping this to controls' own (fixed) height would line up Apply
        # with its bottom, but then the preview would stop growing when
        # the window is resized taller - fitting the available space
        # takes priority, so this uses preview_frame's actual height
        # (which does grow with the window) even though that means Apply
        # only lines up with the controls column at/near minsize.
        avail_h = max(
            self._preview_frame.winfo_height() - 2 * FRAME_PADDING - heading_h - apply_h, 50
        )

        # Fit the panel's fixed 462x1920 aspect ratio into the available
        # space, whichever axis is the tighter constraint.
        ratio = pr.WIDTH / pr.HEIGHT
        w, h = avail_w, round(avail_w / ratio)
        if h > avail_h:
            h, w = avail_h, round(avail_h * ratio)
        self._preview_size = (max(w, 40), max(h, 40))
        if self._last_pil_img is not None:
            self._show_preview(self._last_pil_img)

    def _show_preview(self, img):
        resized = img.resize(self._preview_size)
        self._preview_photo = ImageTk.PhotoImage(resized)  # keep a reference -
                                                            # tkinter drops the
                                                            # image otherwise
        self.preview_label.configure(image=self._preview_photo)

    def _tick_preview(self):
        config = self._config_from_widgets()
        self._last_pil_img = pr.render_stats_pil(config)
        self._on_preview_resize()  # also re-fits size in case it drifted
        if self.color_mode.get() == "auto":
            auto_colors = pr.get_auto_colors(config["wallpaper"])
            for key, rgb in auto_colors.items():
                self._set_color_button(key, rgb)
        self.root.after(PREVIEW_REFRESH_MS, self._tick_preview)


def main():
    root = tk.Tk(className=WM_CLASS)
    try:
        icon = tk.PhotoImage(file=ICON_PATH)
        root.iconphoto(True, icon)
    except tk.TclError:
        pass  # icon.png missing/unreadable - not fatal, just no window icon
    SettingsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
