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

        self.wp_mode = tk.StringVar(value=config["background_mode"])
        self.wp_path = tk.StringVar(value=config["wallpaper"] or "")
        self.game_mode_enabled = tk.BooleanVar(value=config.get("game_mode_enabled", False))
        self.game_api_key = tk.StringVar(value=config.get("steamgriddb_api_key", ""))

        # Source: desktop wallpaper vs. custom image - a combobox rather than
        # radio buttons since it's a single either/or choice, not a set of
        # independent options worth showing all at once.
        self._wp_mode_by_label = {v: k for k, v in pr.BACKGROUND_MODE_LABELS.items()}
        self._wp_mode_row = ttk.Frame(wp_frame)
        self._wp_mode_row.pack(fill="x", pady=3)
        ttk.Label(self._wp_mode_row, text="Source:").pack(side="left")
        self.wp_mode_combo = ttk.Combobox(
            self._wp_mode_row, values=list(pr.BACKGROUND_MODE_LABELS.values()),
            state="readonly", width=26,
        )
        self.wp_mode_combo.set(pr.BACKGROUND_MODE_LABELS[self.wp_mode.get()])
        self.wp_mode_combo.pack(side="left", padx=6, fill="x", expand=True)
        self.wp_mode_combo.bind("<<ComboboxSelected>>", self._on_wp_mode_selected)

        # Only shown/packed at all in "custom" mode - not just disabled -
        # since it's meaningless otherwise.
        self.custom_row = ttk.Frame(wp_frame)
        self.wp_entry = ttk.Entry(self.custom_row, textvariable=self.wp_path, width=28)
        self.wp_entry.pack(side="left", fill="x", expand=True)
        self.wp_browse = ttk.Button(self.custom_row, text="Browse...", command=self._browse)
        self.wp_browse.pack(side="left", padx=(6, 0))

        # Game Mode is independent of the desktop/custom choice above - it
        # overlays a poster on top of whichever of those is picked, only
        # while a game is actually running - so it's a checkbox, not a
        # third mutually-exclusive radio option.
        ttk.Checkbutton(
            wp_frame, text=pr.GAME_MODE_LABEL,
            variable=self.game_mode_enabled, command=self._sync_wp_state,
        ).pack(anchor="w", pady=(10, 3))

        game_key_row = ttk.Frame(wp_frame)
        game_key_row.pack(fill="x", padx=(20, 0))
        ttk.Label(game_key_row, text="SteamGridDB API key:").pack(side="left")
        self.game_key_entry = ttk.Entry(
            game_key_row, textvariable=self.game_api_key, width=22, show="*"
        )
        self.game_key_entry.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Label(
            wp_frame, text="Get a free key at steamgriddb.com/profile/preferences",
            foreground="#888888", padding=(20, 0, 0, 0),
        ).pack(anchor="w")
        self.game_status_label = ttk.Label(
            wp_frame, text="", foreground="#888888", padding=(20, 4, 0, 0),
        )
        self.game_status_label.pack(anchor="w")

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
        self._color_mode_by_label = {v: k for k, v in pr.COLOR_MODE_LABELS.items()}
        mode_row = ttk.Frame(colors_frame)
        mode_row.pack(fill="x", pady=(0, 10))
        ttk.Label(mode_row, text="Mode:").pack(side="left")
        self.color_mode_combo = ttk.Combobox(
            mode_row, values=list(pr.COLOR_MODE_LABELS.values()),
            state="readonly", width=22,
        )
        self.color_mode_combo.set(pr.COLOR_MODE_LABELS[self.color_mode.get()])
        self.color_mode_combo.pack(side="left", padx=6, fill="x", expand=True)
        self.color_mode_combo.bind("<<ComboboxSelected>>", self._on_color_mode_selected)

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
        self.apply_button = tk.Button(
            self.apply_row, text="Apply", command=self._apply, font=default_font,
        )
        self.apply_button.pack(ipadx=10, ipady=0)  # matches wp_browse's height
        self._apply_default_bg = self.apply_button.cget("background")

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

    def _on_wp_mode_selected(self, _event=None):
        self.wp_mode.set(self._wp_mode_by_label[self.wp_mode_combo.get()])
        self._sync_wp_state()

    def _sync_wp_state(self):
        if self.wp_mode.get() == "custom":
            # pack()'s default behavior appends to the end of whatever's
            # currently packed - re-showing this after being hidden would
            # otherwise drop it below the Game Mode controls instead of
            # back under the Source row where it belongs.
            self.custom_row.pack(fill="x", pady=(6, 0), after=self._wp_mode_row)
        else:
            self.custom_row.pack_forget()
        game_state = "normal" if self.game_mode_enabled.get() else "disabled"
        self.game_key_entry.configure(state=game_state)
        if not self.game_mode_enabled.get():
            self.game_status_label.configure(text="")

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Choose background image",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.webp"), ("All files", "*.*")],
        )
        if path:
            self.wp_path.set(path)
            self.wp_mode.set("custom")
            self.wp_mode_combo.set(pr.BACKGROUND_MODE_LABELS["custom"])
            self._sync_wp_state()

    @staticmethod
    def _rgb_to_hex(rgb):
        return "#%02x%02x%02x" % tuple(rgb)

    def _set_color_button(self, key, rgb):
        hexcolor = self._rgb_to_hex(rgb)
        self.color_buttons[key].configure(bg=hexcolor, activebackground=hexcolor)

    def _on_color_mode_selected(self, _event=None):
        self.color_mode.set(self._color_mode_by_label[self.color_mode_combo.get()])
        self._sync_color_mode_state()

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
        mode = self.wp_mode.get()
        wallpaper = self.wp_path.get().strip() if mode == "custom" else None
        return {
            "wallpaper": wallpaper or None,
            "background_mode": mode,
            "game_mode_enabled": self.game_mode_enabled.get(),
            "steamgriddb_api_key": self.game_api_key.get().strip(),
            "sensors": {k: v.get() for k, v in self.sensor_vars.items()},
            "colors": {k: list(v) for k, v in self.colors.items()},
            "color_mode": self.color_mode.get(),
        }

    def _apply(self):
        pr.save_config(self._config_from_widgets())
        # Flash the button itself green rather than showing a status
        # message next to it - keeps it perfectly centered under the
        # preview at all times instead of shifting/reflowing for text.
        self.apply_button.configure(background="#4caf50", activebackground="#4caf50")
        self.root.after(600, lambda: self.apply_button.configure(
            background=self._apply_default_bg, activebackground=self._apply_default_bg,
        ))

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
        bg_path = pr.resolve_background_path(config)
        if self.color_mode.get() == "auto":
            auto_colors = pr.get_auto_colors(bg_path)
            for key, rgb in auto_colors.items():
                self._set_color_button(key, rgb)
        if self.game_mode_enabled.get():
            appid = pr.get_running_game_appid()
            if appid and bg_path and os.path.dirname(bg_path) == pr.HERO_CACHE_DIR:
                self.game_status_label.configure(
                    text=f"Game detected (AppID {appid}) - showing its hero art"
                )
            else:
                fallback = "custom image" if self.wp_mode.get() == "custom" else "desktop wallpaper"
                self.game_status_label.configure(
                    text=f"No game detected - showing {fallback}"
                )
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
