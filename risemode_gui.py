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
import tkinter as tk
from tkinter import filedialog, ttk

from PIL import ImageTk

import panel_render as pr

PREVIEW_HEIGHT = 700
PREVIEW_WIDTH = round(pr.WIDTH * PREVIEW_HEIGHT / pr.HEIGHT)
PREVIEW_REFRESH_MS = 1000


class SettingsApp:
    def __init__(self, root):
        self.root = root
        root.title("Risemode Smart Screen Settings")
        root.resizable(False, False)

        config = pr.get_config()

        controls = ttk.Frame(root, padding=12)
        controls.grid(row=0, column=0, sticky="n")
        preview_frame = ttk.Frame(root, padding=12)
        preview_frame.grid(row=0, column=1, sticky="n")

        # --- Background ---
        wp_frame = ttk.LabelFrame(controls, text="Background", padding=8)
        wp_frame.pack(fill="x", pady=(0, 12))

        self.wp_mode = tk.StringVar(
            value="custom" if config["wallpaper"] else "desktop"
        )
        self.wp_path = tk.StringVar(value=config["wallpaper"] or "")

        ttk.Radiobutton(
            wp_frame, text="Desktop wallpaper (auto-updates)",
            variable=self.wp_mode, value="desktop", command=self._sync_wp_state,
        ).pack(anchor="w")

        custom_row = ttk.Frame(wp_frame)
        custom_row.pack(fill="x", pady=(4, 0))
        ttk.Radiobutton(
            custom_row, text="Custom image:", variable=self.wp_mode,
            value="custom", command=self._sync_wp_state,
        ).pack(side="left")
        self.wp_entry = ttk.Entry(custom_row, textvariable=self.wp_path, width=26)
        self.wp_entry.pack(side="left", padx=4, fill="x", expand=True)
        self.wp_browse = ttk.Button(custom_row, text="Browse...", command=self._browse)
        self.wp_browse.pack(side="left")
        self._sync_wp_state()

        # --- Sensors ---
        sensors_frame = ttk.LabelFrame(controls, text="Sensors", padding=8)
        sensors_frame.pack(fill="x", pady=(0, 12))

        self.sensor_vars = {}
        for key, label in pr.SENSOR_LABELS.items():
            var = tk.BooleanVar(value=config["sensors"].get(key, True))
            self.sensor_vars[key] = var
            ttk.Checkbutton(sensors_frame, text=label, variable=var).pack(anchor="w")

        # --- Apply ---
        apply_row = ttk.Frame(controls)
        apply_row.pack(fill="x")
        ttk.Button(apply_row, text="Apply", command=self._apply).pack(side="left")
        self.status = ttk.Label(apply_row, text="")
        self.status.pack(side="left", padx=8)

        # --- Preview ---
        ttk.Label(preview_frame, text="Live preview").pack()
        self.preview_label = ttk.Label(preview_frame)
        self.preview_label.pack()

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

    def _config_from_widgets(self):
        wallpaper = self.wp_path.get().strip() if self.wp_mode.get() == "custom" else None
        return {
            "wallpaper": wallpaper or None,
            "sensors": {k: v.get() for k, v in self.sensor_vars.items()},
        }

    def _apply(self):
        pr.save_config(self._config_from_widgets())
        self.status.configure(text="Applied - panel updates within a second")
        self.root.after(3000, lambda: self.status.configure(text=""))

    def _tick_preview(self):
        img = pr.render_stats_pil(self._config_from_widgets())
        img = img.resize((PREVIEW_WIDTH, PREVIEW_HEIGHT))
        self._preview_photo = ImageTk.PhotoImage(img)  # keep a reference -
                                                        # tkinter drops the
                                                        # image otherwise
        self.preview_label.configure(image=self._preview_photo)
        self.root.after(PREVIEW_REFRESH_MS, self._tick_preview)


def main():
    root = tk.Tk()
    SettingsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
