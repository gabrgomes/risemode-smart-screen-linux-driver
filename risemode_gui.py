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
FRAME_PADDING = 18   # controls' own outer inset, and the margin Live
                     # preview gets to line its own border up with the
                     # other sections' in horizontal mode (see below)
SECTION_PADDING = 12  # every section LabelFrame's internal border-to-
                      # content padding (Display, Background, Sensors,
                      # Colors, Live preview) - kept as one constant so
                      # they can't drift out of sync with each other
SECTION_GAP = 16  # vertical gap after each section (including the last,
                  # Colors) before whatever comes next

# Matches install.sh's StartupWMClass= so a dock/taskbar can associate the
# running window with the .desktop entry (and its icon) instead of falling
# back to a generic one.
WM_CLASS = "risemode-settings"
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")


class Switch(tk.Canvas):
    """An on/off switch (ttk has no such style built in on Linux) - draws a
    sliding knob on a pill-shaped track, toggled by a click anywhere on it.
    Takes a `variable`/`command` pair like ttk.Checkbutton does, so it drops
    in as a replacement without changing how callers wire it up."""
    WIDTH, HEIGHT = 40, 22
    ON_COLOR = "#4caf50"    # matches the Apply button's own "saved" flash
    OFF_COLOR = "#b0b0b0"
    KNOB_COLOR = "#ffffff"

    def __init__(self, master, variable, command=None):
        # Canvas is a plain Tk widget, not a themed ttk one - it won't pick
        # up the theme's background on its own, so it's looked up explicitly
        # to blend in with the ttk.Frame/LabelFrame it's always placed in.
        bg = ttk.Style().lookup("TFrame", "background") or master.cget("background")
        super().__init__(master, width=self.WIDTH, height=self.HEIGHT,
                          highlightthickness=0, bd=0, bg=bg, cursor="hand2")
        self.variable = variable
        self.command = command
        self.bind("<Button-1>", self._on_click)
        variable.trace_add("write", lambda *_a: self._redraw())
        self._redraw()

    def _on_click(self, _event=None):
        if str(self["state"]) == "disabled":
            return
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()

    def _redraw(self):
        self.delete("all")
        on = bool(self.variable.get())
        color = self.ON_COLOR if on else self.OFF_COLOR
        if str(self["state"]) == "disabled":
            color = "#d5d5d5"
        r = self.HEIGHT / 2
        self.create_oval(0, 0, self.HEIGHT, self.HEIGHT, fill=color, outline=color)
        self.create_oval(self.WIDTH - self.HEIGHT, 0, self.WIDTH, self.HEIGHT,
                          fill=color, outline=color)
        self.create_rectangle(r, 0, self.WIDTH - r, self.HEIGHT, fill=color, outline=color)
        pad = 2
        knob_x = (self.WIDTH - self.HEIGHT) if on else 0
        self.create_oval(knob_x + pad, pad, knob_x + self.HEIGHT - pad, self.HEIGHT - pad,
                          fill=self.KNOB_COLOR, outline="#888888")

    def configure(self, **kwargs):
        # "state" needs a re-draw (disabled renders grayed-out) on top of
        # whatever Canvas itself already does with it - not currently used
        # by any caller, but kept for parity with ttk.Checkbutton's API.
        if "state" in kwargs:
            super().configure(state=kwargs.pop("state"))
            self._redraw()
        if kwargs:
            super().configure(**kwargs)


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

        config = pr.get_config()

        # No bottom padding: each section already ends with its own
        # pady=(0, SECTION_GAP) below it (see Display/Background/Sensors/
        # Colors), so a padded bottom here would stack an extra
        # FRAME_PADDING on top of that just for the last one (Colors) -
        # inflating the gap between it and Live preview beyond what every
        # other inter-section gap actually is, in horizontal mode where
        # they stack.
        controls = ttk.Frame(root, padding=(FRAME_PADDING, FRAME_PADDING, FRAME_PADDING, 0))
        self.controls = controls

        # A LabelFrame, not a plain Frame, so it carries the same bordered,
        # titled look as Display/Background/Sensors/Colors - it was a bare
        # heading + image before, visually lighter-weight than every other
        # section. padding=SECTION_PADDING (not FRAME_PADDING) so its
        # border-to-content inset matches every other section's exactly.
        preview_frame = ttk.LabelFrame(root, text="Live preview", padding=SECTION_PADDING)
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self._preview_frame = preview_frame

        # Inner content holder so _on_preview_resize() can measure exactly
        # the space actually available to the image/Apply button - querying
        # preview_frame's own winfo_height() directly would also include
        # its border and title-label reservation, which isn't part of the
        # padding it was given and can't be read back out reliably.
        preview_body = ttk.Frame(preview_frame)
        preview_body.grid(row=0, column=0, sticky="nsew")
        preview_body.columnconfigure(0, weight=1)
        preview_body.rowconfigure(0, weight=1)
        self._preview_body = preview_body

        # --- Display --- (first: orientation affects every other section's
        # layout, including where the preview itself ends up - see
        # _apply_layout_mode())
        display_frame = ttk.LabelFrame(controls, text="Display", padding=SECTION_PADDING)
        display_frame.pack(fill="x", pady=(0, SECTION_GAP))

        self.orientation = tk.StringVar(value=config.get("orientation", "vertical"))
        self._orientation_by_label = {v: k for k, v in pr.ORIENTATION_LABELS.items()}
        orientation_row = ttk.Frame(display_frame)
        orientation_row.pack(fill="x")
        ttk.Label(orientation_row, text="Orientation:").pack(side="left")
        self.orientation_combo = ttk.Combobox(
            orientation_row, values=list(pr.ORIENTATION_LABELS.values()),
            state="readonly", width=22,
        )
        self.orientation_combo.set(pr.ORIENTATION_LABELS[self.orientation.get()])
        self.orientation_combo.pack(side="left", padx=6, fill="x", expand=True)
        self.orientation_combo.bind("<<ComboboxSelected>>", self._on_orientation_selected)

        # --- Background ---
        wp_frame = ttk.LabelFrame(controls, text="Background", padding=SECTION_PADDING)
        wp_frame.pack(fill="x", pady=(0, SECTION_GAP))

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
        # while a game is actually running - so it's a toggle, not a third
        # mutually-exclusive radio/combobox option.
        game_mode_row = ttk.Frame(wp_frame)
        game_mode_row.pack(fill="x", pady=(10, 3))
        ttk.Label(game_mode_row, text=pr.GAME_MODE_LABEL).pack(side="left")
        Switch(
            game_mode_row, variable=self.game_mode_enabled, command=self._sync_wp_state,
        ).pack(side="right")

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
        sensors_frame = ttk.LabelFrame(controls, text="Sensors", padding=SECTION_PADDING)
        sensors_frame.pack(fill="x", pady=(0, SECTION_GAP))

        self.sensor_vars = {}
        for key, label in pr.SENSOR_LABELS.items():
            var = tk.BooleanVar(value=config["sensors"].get(key, True))
            self.sensor_vars[key] = var
            row = ttk.Frame(sensors_frame)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label).pack(side="left")
            Switch(row, variable=var).pack(side="right")

        # --- Colors ---
        colors_frame = ttk.LabelFrame(controls, text="Colors", padding=SECTION_PADDING)
        colors_frame.pack(fill="x", pady=(0, SECTION_GAP))

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

        # --- Preview --- (the section's own title comes from the
        # LabelFrame itself now, not a separate heading widget)
        self.preview_label = ttk.Label(preview_body, anchor="center")
        self.preview_label.grid(row=0, column=0, sticky="nsew")

        # --- Apply --- (below the preview, not the controls column)
        self.apply_row = ttk.Frame(preview_body)
        self.apply_row.grid(row=1, column=0, pady=(8, 0))
        self.apply_button = tk.Button(
            self.apply_row, text="Apply", command=self._apply, font=default_font,
        )
        self.apply_button.pack(ipadx=10, ipady=0)  # matches wp_browse's height
        self._apply_default_bg = self.apply_button.cget("background")

        self._last_pil_img = None
        self._preview_size = (PREVIEW_WIDTH, PREVIEW_HEIGHT)
        preview_frame.bind("<Configure>", lambda event: self._on_preview_resize())

        self._apply_layout_mode()
        self._tick_preview()

    def _apply_layout_mode(self):
        """Arranges controls and the live preview based on orientation:
        side by side for vertical (controls a fixed-width left column, the
        portrait preview filling the rest), or stacked for horizontal
        (controls a full-width row on top, the wide landscape preview
        filling the rest below it) - a side-by-side landscape preview would
        otherwise be squeezed into a tall, narrow leftover strip next to
        controls instead of the wide one it actually needs."""
        self.root.update_idletasks()
        horizontal = self.orientation.get() == "horizontal"
        if horizontal:
            # "new", not just "nw": controls has to actually stretch to
            # fill the full (now single) column's width - min_w below can
            # exceed controls' own natural width (the 900 floor), and
            # without the "e" it would just sit at that narrower natural
            # width, left-aligned, with the rest of the column empty
            # beside it exactly where the vertical layout's preview column
            # used to be.
            self.controls.grid(row=0, column=0, sticky="new")
            # padx matches controls' own padding, so Live preview's border
            # lines up with Display/Background/Sensors/Colors' borders
            # exactly instead of running flush to the window edges while
            # theirs sit inset inside controls' padding. pady's bottom
            # value mirrors that same inset at the window's bottom edge -
            # the top side needs none, controls' own bottom padding after
            # the Colors section already provides that gap.
            self._preview_frame.grid(
                row=1, column=0, sticky="nsew", padx=FRAME_PADDING, pady=(0, FRAME_PADDING)
            )
            self.root.columnconfigure(0, weight=1)
            self.root.columnconfigure(1, weight=0)
            self.root.rowconfigure(0, weight=0)
            self.root.rowconfigure(1, weight=1)
            min_w = max(self.controls.winfo_reqwidth() + 2 * FRAME_PADDING, 900)
            min_h = self.controls.winfo_reqheight() + 340
        else:
            self.controls.grid(row=0, column=0, sticky="nw")
            # "new", not "nsew": Live preview must NOT stretch vertically
            # to fill row 0 - it has to size itself to its own actual
            # content (an image sized to land it exactly at controls'
            # height, see _on_preview_resize()) and stop there, or grid
            # would stretch it to match row 0's height regardless of that
            # content, right back to overshooting Colors' (the last
            # section) bottom the same way sticky="nsew" here did before.
            # pady's top value matches controls' own top padding, so Live
            # preview's top border lines up with Display's (the first
            # section) instead of starting right at the row's own top
            # edge while Display starts FRAME_PADDING below it. padx's
            # right value gives it the same margin from the window's
            # right edge that controls' own padding gives Display/etc.
            # from the left edge - without it, Live preview's border sat
            # flush against the window edge while theirs sat inset.
            self._preview_frame.grid(
                row=0, column=1, sticky="new", pady=(FRAME_PADDING, 0), padx=(0, FRAME_PADDING)
            )
            self.root.columnconfigure(0, weight=0)
            self.root.columnconfigure(1, weight=1)
            self.root.rowconfigure(0, weight=0)
            self.root.rowconfigure(1, weight=0)
            # The controls column doesn't stretch (weight=0), so it can
            # force the preview column to zero width unless minsize
            # reserves it a usable amount up front.
            min_w = self.controls.winfo_reqwidth() + 380
            min_h = self.controls.winfo_reqheight()
        self.root.minsize(min_w, min_h)

        # minsize only ever grows an existing window, it never shrinks one -
        # so coming from vertical's wide side-by-side layout, horizontal's
        # single narrower column would otherwise leave whatever width the
        # window already had just sitting empty to the right of it (exactly
        # where the preview column used to be). Resize explicitly instead,
        # narrowing for horizontal and widening for vertical only when the
        # current size actually calls for it either way.
        current_w, current_h = self.root.winfo_width(), self.root.winfo_height()
        target_w = min_w if horizontal else max(current_w, min_w)
        target_h = max(current_h, min_h)
        if (target_w, target_h) != (current_w, current_h):
            self.root.geometry(f"{target_w}x{target_h}")

        self._on_preview_resize()

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

    def _on_orientation_selected(self, _event=None):
        self.orientation.set(self._orientation_by_label[self.orientation_combo.get()])
        self._apply_layout_mode()  # re-arranges controls/preview and re-fits the preview

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
            "orientation": self.orientation.get(),
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
        # preview_body already excludes preview_frame's own padding *and*
        # its border/title reservation (that's the point of measuring the
        # inner frame instead of preview_frame directly - see its creation).
        avail_w = max(self._preview_body.winfo_width(), 50)

        if self.orientation.get() == "vertical":
            # In vertical mode Live preview has to match controls' height
            # exactly (see _apply_layout_mode()), so the target here is
            # derived straight from controls' own measured height instead
            # of preview_body's current size - that would be self-
            # reinforcing (a taller image gives preview_frame a taller
            # reqheight, which grows the row to fit it, which then gets
            # measured as "more room available" next time, without ever
            # settling back down to controls' actual height). "chrome" is
            # everything in preview_frame that isn't the image itself
            # (its border, title, padding, the Apply button) - subtracting
            # it, the shared top pady, and Colors' own trailing
            # SECTION_GAP (controls' own reqheight includes that gap
            # *after* Colors, which isn't part of Colors' own border) from
            # controls' height gives the image exactly the room left over
            # after all of that fixed overhead, however big it is.
            chrome = self._preview_frame.winfo_reqheight() - self.preview_label.winfo_reqheight()
            avail_h = max(
                self.controls.winfo_reqheight() - FRAME_PADDING - SECTION_GAP - chrome, 50
            )
        else:
            # Horizontal mode has no such sibling to match - it just fills
            # whatever room row 1 actually gives it (which does grow with
            # the window), so measuring preview_body's own current height
            # is exactly right here.
            apply_h = self.apply_row.winfo_reqheight() + 8
            avail_h = max(self._preview_body.winfo_height() - apply_h, 50)

        # Fit the chosen orientation's logical canvas aspect ratio (portrait
        # 462x1920, or landscape 1920x462) into the available space,
        # whichever axis is the tighter constraint - the preview always
        # shows render_stats_pil()'s own upright logical image, never the
        # physical panel buffer's rotated layout.
        canvas_w, canvas_h = pr.CANVAS_SIZES[self.orientation.get()]
        ratio = canvas_w / canvas_h
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
