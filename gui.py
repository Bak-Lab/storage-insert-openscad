#!/usr/bin/env python3
"""
Tkinter GUI for the Storage Insert Drawer Generator.

Uses a grid-based layout model where cells can be merged into arbitrary
shapes (rectangles, L-shapes, T-shapes, etc.).  Provides a live 2D preview
with draggable dividers, click-to-select, and right-click merge/split.
"""

import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from math import gcd
from functools import reduce
from pathlib import Path

from generate import generate_scad_grid, find_openscad, export_stl


# ── Grid helpers ──────────────────────────────────────────────────────
# cell_info: dict[int, set[tuple[int,int]]]
#   Maps cell-id → set of (row, col) grid positions belonging to that cell.

def make_uniform_grid(n_rows, n_cols):
    """Create a uniform grid with all 1×1 cells."""
    grid = []
    cell_info = {}
    cid = 0
    for r in range(n_rows):
        row = []
        for c in range(n_cols):
            row.append(cid)
            cell_info[cid] = {(r, c)}
            cid += 1
        grid.append(row)
    return grid, cell_info, cid


def make_default_storage_grid():
    """Default storage insert: 2×3 grid with top-left 2 cols merged."""
    n_rows, n_cols = 2, 3
    col_widths = [1.0, 1.0, 1.0]
    row_heights = [1.0, 1.0]
    grid = [[0, 0, 1],
            [2, 3, 4]]
    cell_info = {
        0: {(0, 0), (0, 1)},
        1: {(0, 2)},
        2: {(1, 0)},
        3: {(1, 1)},
        4: {(1, 2)},
    }
    return n_rows, n_cols, col_widths, row_heights, grid, cell_info, 5


def old_layout_to_grid(layout):
    """Convert old list[list[float]] layout to grid model."""
    n_rows = len(layout)
    row_lengths = [len(r) for r in layout]

    def lcm(a, b):
        return a * b // gcd(a, b)

    n_cols = reduce(lcm, row_lengths)
    if n_cols > 20:
        n_cols = max(row_lengths)

    col_widths = [1.0] * n_cols
    row_heights = [1.0] * n_rows
    grid = [[0] * n_cols for _ in range(n_rows)]
    cell_info = {}
    next_id = 0

    for r, row in enumerate(layout):
        k = len(row)
        cols_per = n_cols / k
        c_start = 0
        for i in range(k):
            c_end = round((i + 1) * cols_per)
            cid = next_id
            next_id += 1
            cell_info[cid] = {(r, c) for c in range(c_start, c_end)}
            for c in range(c_start, c_end):
                grid[r][c] = cid
            c_start = c_end

    return n_rows, n_cols, col_widths, row_heights, grid, cell_info, next_id


# ── LayoutEditor ──────────────────────────────────────────────────────

class LayoutEditor(tk.Frame):
    """Controls for grid-based layout: grid size buttons, presets, info."""

    def __init__(self, parent, on_add_row=None, on_remove_row=None,
                 on_add_col=None, on_remove_col=None,
                 on_preset=None, on_load_json=None):
        super().__init__(parent)
        self._cbs = {
            "add_row": on_add_row, "remove_row": on_remove_row,
            "add_col": on_add_col, "remove_col": on_remove_col,
        }
        self._on_preset = on_preset
        self._on_load_json = on_load_json

        self._info_label = tk.Label(self, text="2 rows \u00d7 3 cols",
                                    font=("sans-serif", 10, "bold"), anchor="w")
        self._info_label.pack(fill="x", pady=(0, 4))

        btn = tk.Frame(self)
        btn.pack(fill="x", pady=(0, 6))
        tk.Button(btn, text="+ Row", width=7,
                  command=lambda: self._fire("add_row")).pack(side="left", padx=2)
        tk.Button(btn, text="\u2212 Row", width=7,
                  command=lambda: self._fire("remove_row")).pack(side="left", padx=2)
        ttk.Separator(btn, orient="vertical").pack(side="left", fill="y", padx=4)
        tk.Button(btn, text="+ Col", width=7,
                  command=lambda: self._fire("add_col")).pack(side="left", padx=2)
        tk.Button(btn, text="\u2212 Col", width=7,
                  command=lambda: self._fire("remove_col")).pack(side="left", padx=2)

        pf = tk.Frame(self)
        pf.pack(fill="x", pady=(0, 6))
        tk.Label(pf, text="Preset:").pack(side="left", padx=(0, 4))
        tk.Button(pf, text="Storage Insert",
                  command=lambda: self._fire_preset("storage")).pack(side="left", padx=2)
        tk.Button(pf, text="Grid 3\u00d73",
                  command=lambda: self._fire_preset("grid3x3")).pack(side="left", padx=2)
        tk.Button(pf, text="Load JSON",
                  command=self._load_json).pack(side="left", padx=2)

        self._count_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._count_var, fg="#555", anchor="w",
                 wraplength=300, justify="left").pack(fill="x", pady=(4, 0))

    def update_info(self, n_rows, n_cols, n_compartments):
        self._info_label.config(text=f"{n_rows} rows \u00d7 {n_cols} cols")
        self._count_var.set(
            f"{n_compartments} compartments\n"
            "Tip: Click cells in preview to select, Ctrl+click for multi,\n"
            "right-click to merge / split."
        )

    def _fire(self, action):
        cb = self._cbs.get(action)
        if cb:
            cb()

    def _fire_preset(self, name):
        if self._on_preset:
            self._on_preset(name)

    def _load_json(self):
        path = filedialog.askopenfilename(
            title="Load Layout JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path and self._on_load_json:
            self._on_load_json(path)


# ── PreviewCanvas ─────────────────────────────────────────────────────

class PreviewCanvas(tk.Canvas):
    """2D top-down preview with grid-based layout and draggable dividers.

    Cells can have arbitrary shapes (any connected set of grid slots).
    """

    GRAB_RADIUS = 8

    def __init__(self, parent, on_change=None, **kwargs):
        kwargs.setdefault("bg", "#f5f5f0")
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(parent, **kwargs)
        self.on_change = on_change

        # Physical dimensions (mm)
        self._width = 300.0
        self._length = 400.0
        self._wall = 2.0

        # Grid model
        self._n_rows = 0
        self._n_cols = 0
        self._col_widths: list[float] = []
        self._row_heights: list[float] = []
        self._grid: list[list[int]] = []
        self._cell_info: dict[int, set[tuple[int, int]]] = {}
        self._next_cell_id = 0

        # Selection: set of cell_ids
        self._selected_cells: set[int] = set()

        self.load_default_storage()

        # Divider hit-zones
        self._dividers: list[tuple] = []
        # Grid-line screen positions for hit-testing
        self._col_x_scr: list[float] = []
        self._row_y_scr: list[float] = []

        # Drag state
        self._dragging = None

        # Cached transform
        self._ox = self._oy = 0.0
        self._scale = 1.0

        # Bindings
        self.bind("<Configure>", lambda _: self.redraw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Motion>", self._on_hover)
        self.bind("<ButtonPress-3>", self._on_right_click)

        # Context menu
        self._ctx_menu = tk.Menu(self, tearoff=0)
        self._ctx_menu.add_command(label="Merge Selected Cells",
                                   command=self._merge_selected)
        self._ctx_menu.add_command(label="Split Cell",
                                   command=self._split_selected)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Add Row", command=self.add_row)
        self._ctx_menu.add_command(label="Add Column", command=self.add_col)
        self._ctx_menu.add_command(label="Remove Last Row",
                                   command=self.remove_row)
        self._ctx_menu.add_command(label="Remove Last Column",
                                   command=self.remove_col)

    # ── Grid model methods ──

    def _alloc_id(self):
        cid = self._next_cell_id
        self._next_cell_id += 1
        return cid

    def load_default_storage(self):
        (self._n_rows, self._n_cols, self._col_widths, self._row_heights,
         self._grid, self._cell_info, self._next_cell_id
        ) = make_default_storage_grid()
        self._selected_cells.clear()

    def load_uniform_grid(self, n_rows, n_cols):
        self._n_rows = n_rows
        self._n_cols = n_cols
        self._col_widths = [1.0] * n_cols
        self._row_heights = [1.0] * n_rows
        self._grid, self._cell_info, self._next_cell_id = \
            make_uniform_grid(n_rows, n_cols)
        self._selected_cells.clear()

    def load_from_old_layout(self, layout):
        (self._n_rows, self._n_cols, self._col_widths, self._row_heights,
         self._grid, self._cell_info, self._next_cell_id
        ) = old_layout_to_grid(layout)
        self._selected_cells.clear()

    def load_grid_data(self, data):
        self._n_rows = data["n_rows"]
        self._n_cols = data["n_cols"]
        self._col_widths = data["col_widths"][:]
        self._row_heights = data["row_heights"][:]
        self._grid = [row[:] for row in data["grid"]]
        cells = data["cells"]
        self._cell_info = {
            int(k): {tuple(pos) for pos in v}
            for k, v in cells.items()
        }
        self._next_cell_id = (max(self._cell_info.keys()) + 1
                              if self._cell_info else 0)
        self._selected_cells.clear()

    def get_grid_data(self):
        return {
            "n_rows": self._n_rows,
            "n_cols": self._n_cols,
            "col_widths": self._col_widths[:],
            "row_heights": self._row_heights[:],
            "grid": [row[:] for row in self._grid],
            "cells": {
                str(k): [list(pos) for pos in sorted(v)]
                for k, v in self._cell_info.items()
            },
        }

    def add_row(self):
        r = self._n_rows
        self._n_rows += 1
        self._row_heights.append(1.0)
        new_row = []
        for c in range(self._n_cols):
            cid = self._alloc_id()
            new_row.append(cid)
            self._cell_info[cid] = {(r, c)}
        self._grid.append(new_row)
        self._selected_cells.clear()
        self.after_idle(self.redraw)
        self._notify_change()

    def remove_row(self):
        if self._n_rows <= 1:
            return
        r = self._n_rows - 1
        for c in range(self._n_cols):
            cid = self._grid[r][c]
            if cid in self._cell_info:
                self._cell_info[cid].discard((r, c))
                if not self._cell_info[cid]:
                    del self._cell_info[cid]
                    self._selected_cells.discard(cid)
        self._grid.pop()
        self._n_rows -= 1
        self._row_heights.pop()
        self.after_idle(self.redraw)
        self._notify_change()

    def add_col(self):
        c = self._n_cols
        self._n_cols += 1
        self._col_widths.append(1.0)
        for r in range(self._n_rows):
            cid = self._alloc_id()
            self._grid[r].append(cid)
            self._cell_info[cid] = {(r, c)}
        self._selected_cells.clear()
        self.after_idle(self.redraw)
        self._notify_change()

    def remove_col(self):
        if self._n_cols <= 1:
            return
        c = self._n_cols - 1
        for r in range(self._n_rows):
            cid = self._grid[r][c]
            if cid in self._cell_info:
                self._cell_info[cid].discard((r, c))
                if not self._cell_info[cid]:
                    del self._cell_info[cid]
                    self._selected_cells.discard(cid)
            self._grid[r].pop()
        self._n_cols -= 1
        self._col_widths.pop()
        self.after_idle(self.redraw)
        self._notify_change()

    def update_dims(self, width, length, wall):
        self._width = width
        self._length = length
        self._wall = wall
        self.after_idle(self.redraw)

    @property
    def n_compartments(self):
        return len(self._cell_info)

    def _notify_change(self):
        if self.on_change:
            self.on_change()

    # ── Coordinate helpers ──

    def _sx(self, v):
        return self._ox + v * self._scale

    def _sy(self, v):
        return self._oy + v * self._scale

    def _inv_sx(self, px):
        return (px - self._ox) / self._scale if self._scale else 0

    def _inv_sy(self, py):
        return (py - self._oy) / self._scale if self._scale else 0

    # ── Drawing ──

    def redraw(self):
        self.delete("all")
        cw = self.winfo_width()
        ch = self.winfo_height()
        if cw < 10 or ch < 10:
            return

        pad = 20
        self._scale = min((cw - 2 * pad) / self._width,
                          (ch - 2 * pad) / self._length)
        self._ox = (cw - self._width * self._scale) / 2
        self._oy = (ch - self._length * self._scale) / 2

        sx, sy = self._sx, self._sy
        wall = self._wall
        inner_w = self._width - 2 * wall
        inner_l = self._length - 2 * wall
        total_cw = sum(self._col_widths) or 1
        total_rh = sum(self._row_heights) or 1

        # Grid positions (model coords)
        col_x = []
        cum = 0.0
        for w in self._col_widths:
            col_x.append(wall + (cum / total_cw) * inner_w)
            cum += w
        col_x.append(wall + inner_w)

        row_y = []
        cum = 0.0
        for h in self._row_heights:
            row_y.append(wall + (cum / total_rh) * inner_l)
            cum += h
        row_y.append(wall + inner_l)

        # Store screen-coords of grid lines for hit testing
        self._col_x_scr = [sx(x) for x in col_x]
        self._row_y_scr = [sy(y) for y in row_y]

        # Prune stale selections
        self._selected_cells &= set(self._cell_info.keys())

        # Outer box
        self.create_rectangle(
            sx(0), sy(0), sx(self._width), sy(self._length),
            outline="#333", width=max(wall * self._scale, 1.5), fill="#e8dcc8",
        )

        # ── Cell fills (per-slot) ──
        colors = ["#f7e8c8", "#c8daf7", "#c8f7d5", "#f7c8c8",
                  "#e8c8f7", "#f7f0c8", "#c8f0f7", "#f0c8d8"]
        color_map = {}
        ci = 0
        for r in range(self._n_rows):
            for c in range(self._n_cols):
                cid = self._grid[r][c]
                if cid not in color_map:
                    color_map[cid] = ci
                    ci += 1
                selected = cid in self._selected_cells
                fill = "#a0c4ff" if selected else colors[color_map[cid] % len(colors)]
                self.create_rectangle(
                    sx(col_x[c]), sy(row_y[r]),
                    sx(col_x[c + 1]), sy(row_y[r + 1]),
                    fill=fill, outline="",
                )

        # ── Selection outline (follows cell shape) ──
        for cid in self._selected_cells:
            if cid not in self._cell_info:
                continue
            for (pr, pc) in self._cell_info[cid]:
                x0s = sx(col_x[pc])
                y0s = sy(row_y[pr])
                x1s = sx(col_x[pc + 1])
                y1s = sy(row_y[pr + 1])
                # Top
                if pr == 0 or self._grid[pr - 1][pc] != cid:
                    self.create_line(x0s, y0s, x1s, y0s,
                                     fill="#2060cc", width=2.5, dash=(5, 3))
                # Bottom
                if pr == self._n_rows - 1 or self._grid[pr + 1][pc] != cid:
                    self.create_line(x0s, y1s, x1s, y1s,
                                     fill="#2060cc", width=2.5, dash=(5, 3))
                # Left
                if pc == 0 or self._grid[pr][pc - 1] != cid:
                    self.create_line(x0s, y0s, x0s, y1s,
                                     fill="#2060cc", width=2.5, dash=(5, 3))
                # Right
                if pc == self._n_cols - 1 or self._grid[pr][pc + 1] != cid:
                    self.create_line(x1s, y0s, x1s, y1s,
                                     fill="#2060cc", width=2.5, dash=(5, 3))

        # ── Dimension labels (one per cell, at centroid) ──
        drawn_labels = set()
        for r in range(self._n_rows):
            for c in range(self._n_cols):
                cid = self._grid[r][c]
                if cid in drawn_labels:
                    continue
                drawn_labels.add(cid)
                positions = self._cell_info.get(cid, set())
                if not positions:
                    continue
                # Centroid in screen coords
                cx = sum(
                    (sx(col_x[pc]) + sx(col_x[pc + 1])) / 2
                    for (_, pc) in positions
                ) / len(positions)
                cy = sum(
                    (sy(row_y[pr]) + sy(row_y[pr + 1])) / 2
                    for (pr, _) in positions
                ) / len(positions)
                # Bounding rect for label
                rows_s = {pr for pr, pc in positions}
                cols_s = {pc for pr, pc in positions}
                rspan = max(rows_s) - min(rows_s) + 1
                cspan = max(cols_s) - min(cols_s) + 1
                if rspan * cspan == len(positions):
                    # Rectangular cell: show W×H
                    w_mm = col_x[max(cols_s) + 1] - col_x[min(cols_s)]
                    h_mm = row_y[max(rows_s) + 1] - row_y[min(rows_s)]
                    label = f"{w_mm:.0f}\u00d7{h_mm:.0f}"
                else:
                    # Non-rectangular: show total area
                    area = sum(
                        (col_x[pc + 1] - col_x[pc]) * (row_y[pr + 1] - row_y[pr])
                        for (pr, pc) in positions
                    )
                    label = f"{area:.0f} mm\u00b2"
                self.create_text(cx, cy, text=label,
                                 font=("sans-serif", 8), fill="#555")

        # ── Dividers ──
        self._dividers.clear()
        wall_px = max(wall * self._scale, 2)

        # Horizontal dividers
        for r_b in range(1, self._n_rows):
            y = row_y[r_b]
            c = 0
            while c < self._n_cols:
                if self._grid[r_b - 1][c] != self._grid[r_b][c]:
                    c_start = c
                    while (c < self._n_cols
                           and self._grid[r_b - 1][c] != self._grid[r_b][c]):
                        c += 1
                    self.create_line(sx(col_x[c_start]), sy(y),
                                     sx(col_x[c]), sy(y),
                                     fill="#446", width=wall_px)
                    self._dividers.append(
                        ("h", r_b, sy(y), sx(col_x[c_start]), sx(col_x[c])))
                else:
                    c += 1

        # Vertical dividers
        for c_b in range(1, self._n_cols):
            x = col_x[c_b]
            r = 0
            while r < self._n_rows:
                if self._grid[r][c_b - 1] != self._grid[r][c_b]:
                    r_start = r
                    while (r < self._n_rows
                           and self._grid[r][c_b - 1] != self._grid[r][c_b]):
                        r += 1
                    self.create_line(sx(x), sy(row_y[r_start]),
                                     sx(x), sy(row_y[r]),
                                     fill="#446", width=wall_px)
                    self._dividers.append(
                        ("v", c_b, sx(x), sy(row_y[r_start]), sy(row_y[r])))
                else:
                    r += 1

        # Dimension annotations
        self.create_text(
            sx(self._width / 2), sy(-2) - 6,
            text=f"{self._width:.0f} mm",
            font=("sans-serif", 9, "bold"), fill="#333",
        )
        self.create_text(
            sx(self._width) + 14, sy(self._length / 2),
            text=f"{self._length:.0f} mm",
            font=("sans-serif", 9, "bold"), fill="#333", angle=90,
        )

        # Hint
        self.create_text(
            cw / 2, ch - 8,
            text="Click to select \u00b7 Ctrl+click multi \u00b7 "
                 "Right-click: merge/split \u00b7 Drag dividers to resize",
            font=("sans-serif", 7), fill="#999",
        )

    # ── Hit testing ──

    def _hit_divider(self, mx, my):
        gr = self.GRAB_RADIUS
        for d in self._dividers:
            if d[0] == "h":
                _, boundary, sy_val, sx_l, sx_r = d
                if sx_l <= mx <= sx_r and abs(my - sy_val) <= gr:
                    return d
            else:
                _, boundary, sx_val, sy_t, sy_b = d
                if sy_t <= my <= sy_b and abs(mx - sx_val) <= gr:
                    return d
        return None

    def _hit_cell(self, mx, my):
        """Look up which cell the mouse is over via grid coordinates."""
        col = None
        for c in range(self._n_cols):
            if (c + 1 < len(self._col_x_scr)
                    and self._col_x_scr[c] <= mx <= self._col_x_scr[c + 1]):
                col = c
                break
        row = None
        for r in range(self._n_rows):
            if (r + 1 < len(self._row_y_scr)
                    and self._row_y_scr[r] <= my <= self._row_y_scr[r + 1]):
                row = r
                break
        if row is not None and col is not None:
            return self._grid[row][col]
        return None

    # ── Mouse handlers ──

    def _on_hover(self, event):
        hit = self._hit_divider(event.x, event.y)
        if hit:
            self.config(cursor="sb_v_double_arrow" if hit[0] == "h"
                        else "sb_h_double_arrow")
        else:
            self.config(cursor="")

    def _on_press(self, event):
        hit = self._hit_divider(event.x, event.y)
        if hit:
            self._dragging = hit
            return

        cell = self._hit_cell(event.x, event.y)
        ctrl = event.state & 0x4
        if cell is not None:
            if ctrl:
                self._selected_cells ^= {cell}
            else:
                if self._selected_cells == {cell}:
                    self._selected_cells.clear()
                else:
                    self._selected_cells = {cell}
        else:
            self._selected_cells.clear()
        self.redraw()

    def _on_drag(self, event):
        if not self._dragging:
            return
        d = self._dragging
        min_prop = 0.1

        if d[0] == "h":
            boundary = d[1]
            model_y = self._inv_sy(event.y)
            total_rh = sum(self._row_heights)
            inner_l = self._length - 2 * self._wall
            cum_before = sum(self._row_heights[:boundary - 1])
            cum_after = sum(self._row_heights[:boundary + 1])
            y_top = self._wall + (cum_before / total_rh) * inner_l
            y_bot = self._wall + (cum_after / total_rh) * inner_l
            min_h = (y_bot - y_top) * 0.05
            model_y = max(y_top + min_h, min(y_bot - min_h, model_y))
            frac = (model_y - self._wall) / inner_l
            new_cum = frac * total_rh
            old_cum = sum(self._row_heights[:boundary])
            delta = new_cum - old_cum
            new_top = self._row_heights[boundary - 1] + delta
            new_bot = self._row_heights[boundary] - delta
            if new_top >= min_prop and new_bot >= min_prop:
                self._row_heights[boundary - 1] = round(new_top, 3)
                self._row_heights[boundary] = round(new_bot, 3)
                self.redraw()

        elif d[0] == "v":
            boundary = d[1]
            model_x = self._inv_sx(event.x)
            total_cw = sum(self._col_widths)
            inner_w = self._width - 2 * self._wall
            cum_before = sum(self._col_widths[:boundary - 1])
            cum_after = sum(self._col_widths[:boundary + 1])
            x_left = self._wall + (cum_before / total_cw) * inner_w
            x_right = self._wall + (cum_after / total_cw) * inner_w
            min_w = (x_right - x_left) * 0.05
            model_x = max(x_left + min_w, min(x_right - min_w, model_x))
            frac = (model_x - self._wall) / inner_w
            new_cum = frac * total_cw
            old_cum = sum(self._col_widths[:boundary])
            delta = new_cum - old_cum
            new_left = self._col_widths[boundary - 1] + delta
            new_right = self._col_widths[boundary] - delta
            if new_left >= min_prop and new_right >= min_prop:
                self._col_widths[boundary - 1] = round(new_left, 3)
                self._col_widths[boundary] = round(new_right, 3)
                self.redraw()

    def _on_release(self, event):
        if self._dragging:
            self._dragging = None
            self._notify_change()

    def _on_right_click(self, event):
        cell = self._hit_cell(event.x, event.y)
        if cell is None:
            return
        if cell not in self._selected_cells:
            self._selected_cells = {cell}
            self.redraw()

        can_merge = self._can_merge()
        can_split = self._can_split()

        self._ctx_menu.entryconfigure("Merge Selected Cells",
                                      state="normal" if can_merge else "disabled")
        self._ctx_menu.entryconfigure("Split Cell",
                                      state="normal" if can_split else "disabled")
        self._ctx_menu.entryconfigure("Remove Last Row",
                                      state="normal" if self._n_rows > 1 else "disabled")
        self._ctx_menu.entryconfigure("Remove Last Column",
                                      state="normal" if self._n_cols > 1 else "disabled")
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    # ── Merge / Split ──

    def _can_merge(self):
        """Selected cells can merge if ≥2 and the union of their grid
        positions forms a connected region (each cell touches at least
        one other selected cell via a shared grid edge)."""
        sel = self._selected_cells
        if len(sel) < 2:
            return False
        for cid in sel:
            if cid not in self._cell_info:
                return False

        # Collect all positions
        all_positions = set()
        for cid in sel:
            all_positions |= self._cell_info[cid]

        # Flood-fill from any position; must reach all
        start = next(iter(all_positions))
        visited = set()
        stack = [start]
        while stack:
            pos = stack.pop()
            if pos in visited:
                continue
            visited.add(pos)
            r, c = pos
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                nb = (r + dr, c + dc)
                if nb in all_positions and nb not in visited:
                    stack.append(nb)
        return visited == all_positions

    def _merge_selected(self):
        if not self._can_merge():
            return
        all_positions = set()
        for cid in self._selected_cells:
            all_positions |= self._cell_info[cid]
        for cid in list(self._selected_cells):
            del self._cell_info[cid]
        new_cid = self._alloc_id()
        self._cell_info[new_cid] = all_positions
        for (r, c) in all_positions:
            self._grid[r][c] = new_cid
        self._selected_cells.clear()
        self.redraw()
        self._notify_change()

    def _can_split(self):
        return any(
            cid in self._cell_info and len(self._cell_info[cid]) > 1
            for cid in self._selected_cells
        )

    def _split_selected(self):
        changed = False
        for cid in list(self._selected_cells):
            if cid not in self._cell_info:
                continue
            positions = self._cell_info[cid]
            if len(positions) <= 1:
                continue
            del self._cell_info[cid]
            for (r, c) in positions:
                new_cid = self._alloc_id()
                self._grid[r][c] = new_cid
                self._cell_info[new_cid] = {(r, c)}
            changed = True
        if changed:
            self._selected_cells.clear()
            self.redraw()
            self._notify_change()


# ── App ───────────────────────────────────────────────────────────────

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Storage Insert Drawer Generator")
        self.geometry("950x650")
        self.minsize(750, 500)

        # --- Left panel ---
        left = tk.Frame(self, width=340)
        left.pack(side="left", fill="y", padx=8, pady=8)
        left.pack_propagate(False)

        topbar = tk.Frame(left)
        topbar.pack(fill="x", pady=(0, 6))
        tk.Label(topbar, text="Storage Insert Generator",
                 font=("sans-serif", 13, "bold")).pack(side="left", padx=(2, 8))
        tk.Button(topbar, text="?", width=2, command=self._show_help,
                  relief="groove", bg="#f7f7e0").pack(side="right", padx=2)

        self.unit_var = tk.StringVar(value="mm")
        unit_frame = tk.LabelFrame(left, text="Units", padx=6, pady=2)
        unit_frame.pack(fill="x", pady=(0, 6))
        for u in ("mm", "inch"):
            tk.Radiobutton(unit_frame, text=u, variable=self.unit_var,
                           value=u, command=self._on_unit_change
                           ).pack(side="left", padx=6)

        self._dim_labels = ["Width (X):", "Length (Y):", "Depth (Z):",
                            "Wall:", "Corner R:", "Gap:"]
        self._dim_vars = [tk.DoubleVar() for _ in range(6)]
        self._dim_defaults_mm = [300, 400, 50, 2.0, 3.0, 0.5]
        for v, d in zip(self._dim_vars, self._dim_defaults_mm):
            v.set(d)
        self._dim_ranges_mm = [(10, 1000), (10, 1000), (5, 300),
                               (0.5, 20), (0, 50), (0, 5)]
        self._dim_ranges_in = [(0.5, 40), (0.5, 40), (0.25, 12),
                               (0.02, 1), (0, 2), (0, 0.2)]

        self._dim_frame = tk.LabelFrame(left, text="Dimensions (mm)",
                                        padx=6, pady=4)
        self._dim_frame.pack(fill="x", pady=(0, 6))
        self._dim_spinboxes = []
        for i, (label_text, var) in enumerate(
                zip(self._dim_labels, self._dim_vars)):
            lo, hi = self._dim_ranges_mm[i]
            row = tk.Frame(self._dim_frame)
            row.pack(fill="x", padx=4, pady=3)
            tk.Label(row, text=label_text, width=12, anchor="w").pack(
                side="left")
            sp = tk.Spinbox(row, from_=lo, to=hi,
                            increment=1 if hi > 50 else 0.5,
                            width=8, textvariable=var,
                            relief="groove", bg="#f7f7f7")
            sp.pack(side="left", padx=4)
            var.trace_add("write", lambda *_a: self._on_dim_change())
            self._dim_spinboxes.append(sp)
        tk.Button(self._dim_frame, text="Reset to Defaults",
                  command=self._reset_defaults, relief="ridge",
                  bg="#e0f7e7").pack(pady=(6, 0))

        # --- Stacking option ---
        stack_frame = tk.LabelFrame(left, text="Stacking", padx=6, pady=4)
        stack_frame.pack(fill="x", pady=(0, 6))
        self.stack_var = tk.BooleanVar(value=False)
        tk.Checkbutton(stack_frame, text="Enable stacking geometry",
                       variable=self.stack_var,
                       command=self._on_dim_change).pack(anchor="w")
        lip_row = tk.Frame(stack_frame)
        lip_row.pack(fill="x", padx=4, pady=3)
        tk.Label(lip_row, text="Step height:", width=12, anchor="w").pack(
            side="left")
        self.stack_height_var = tk.DoubleVar(value=2.0)
        self._stack_height_sp = tk.Spinbox(
            lip_row, from_=0.5, to=10, increment=0.5, width=8,
            textvariable=self.stack_height_var,
            relief="groove", bg="#f7f7f7")
        self._stack_height_sp.pack(side="left", padx=4)
        tk.Label(lip_row, text="mm").pack(side="left")
        self.stack_height_var.trace_add("write", lambda *_a: self._on_dim_change())

        layout_frame = tk.LabelFrame(left, text="Compartment Layout",
                                     padx=6, pady=4)
        layout_frame.pack(fill="both", expand=True, pady=(0, 6))

        self.layout_editor = LayoutEditor(
            layout_frame,
            on_add_row=self._on_add_row,
            on_remove_row=self._on_remove_row,
            on_add_col=self._on_add_col,
            on_remove_col=self._on_remove_col,
            on_preset=self._on_preset,
            on_load_json=self._on_load_json,
        )
        self.layout_editor.pack(fill="both", expand=True, padx=4, pady=4)

        btn_frame = tk.Frame(left)
        btn_frame.pack(fill="x", pady=(0, 4))
        tk.Button(btn_frame, text="Export .scad", command=self._export_scad,
                  bg="#e0e7f7", relief="groove").pack(fill="x", pady=2)
        tk.Button(btn_frame, text="Export .stl", command=self._export_stl,
                  bg="#e0e7f7", relief="groove").pack(fill="x", pady=2)
        tk.Button(btn_frame, text="Save Layout JSON",
                  command=self._save_layout_json,
                  bg="#f7f7e0", relief="groove").pack(fill="x", pady=2)

        # --- Right panel ---
        right = tk.Frame(self)
        right.pack(side="right", fill="both", expand=True, padx=(0, 8), pady=8)

        tk.Label(right, text="Preview (top-down)",
                 font=("sans-serif", 10, "bold")).pack(anchor="w")
        self.preview = PreviewCanvas(right, on_change=self._on_preview_change)
        self.preview.pack(fill="both", expand=True)

        tk.Button(right, text="Show 3D Preview",
                  command=self._show_3d_preview,
                  bg="#e0f7ff", relief="groove").pack(fill="x", pady=(8, 0))

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(self, textvariable=self.status_var, bd=1, relief="sunken",
                 anchor="w").pack(side="bottom", fill="x")

        self.after(100, self._refresh_all)

    # ── LayoutEditor callbacks ──

    def _on_add_row(self):
        self.preview.add_row()

    def _on_remove_row(self):
        self.preview.remove_row()

    def _on_add_col(self):
        self.preview.add_col()

    def _on_remove_col(self):
        self.preview.remove_col()

    def _on_preset(self, name):
        if name == "storage":
            self.preview.load_default_storage()
        elif name == "grid3x3":
            self.preview.load_uniform_grid(3, 3)
        self.preview.after_idle(self.preview.redraw)
        self._refresh_all()

    def _on_load_json(self, path):
        try:
            with open(path) as f:
                data = json.load(f)
            if "grid" in data:
                self.preview.load_grid_data(data["grid"])
            elif "layout" in data:
                layout = data["layout"]
                if not isinstance(layout, list) or not layout:
                    raise ValueError("'layout' must be a non-empty list")
                for row in layout:
                    if (not isinstance(row, list) or not row
                            or any(not isinstance(v, (int, float)) or v <= 0
                                   for v in row)):
                        raise ValueError("Each row must be a list of "
                                         "positive numbers")
                self.preview.load_from_old_layout(layout)
            else:
                messagebox.showerror("Error",
                                     "JSON must contain 'grid' or 'layout'")
                return
            self.preview.after_idle(self.preview.redraw)
            self._refresh_all()
        except Exception as e:
            messagebox.showerror("Invalid File", str(e))

    def _on_preview_change(self):
        self._refresh_info()

    # ── Dimension changes ──

    def _on_dim_change(self):
        if not hasattr(self, "preview") or not hasattr(self, "status_var"):
            return
        p = self._get_dims_mm()
        if p:
            self.preview.update_dims(p["width"], p["length"], p["wall"])
            self._update_status()

    def _get_dims_mm(self):
        try:
            vals = [v.get() for v in self._dim_vars]
            if self.unit_var.get() == "inch":
                vals = [v * 25.4 for v in vals]
            return {"width": vals[0], "length": vals[1], "depth": vals[2],
                    "wall": vals[3], "corner_radius": vals[4],
                    "gap": vals[5]}
        except tk.TclError:
            return None

    def _refresh_all(self):
        p = self._get_dims_mm()
        if p:
            self.preview.update_dims(p["width"], p["length"], p["wall"])
        self._refresh_info()

    def _refresh_info(self):
        if hasattr(self, "layout_editor") and hasattr(self, "preview"):
            self.layout_editor.update_info(
                self.preview._n_rows, self.preview._n_cols,
                self.preview.n_compartments,
            )
        self._update_status()

    def _update_status(self):
        if not hasattr(self, "status_var") or not hasattr(self, "preview"):
            return
        p = self._get_dims_mm()
        if not p:
            return
        unit = self.unit_var.get()
        vals = [v.get() for v in self._dim_vars]
        self.status_var.set(
            f"{vals[0]:.2f} \u00d7 {vals[1]:.2f} \u00d7 {vals[2]:.2f} {unit}"
            f"  |  Wall: {vals[3]:.2f}, Gap: {vals[5]:.2f} {unit}"
            f"  |  {self.preview._n_rows}\u00d7{self.preview._n_cols} grid, "
            f"{self.preview.n_compartments} compartments"
        )

    # ── Unit conversion ──

    def _on_unit_change(self):
        old_unit = getattr(self, "_last_unit", "mm")
        new_unit = self.unit_var.get()
        if old_unit == new_unit:
            return
        vals_mm = [v.get() * 25.4 if old_unit == "inch" else v.get()
                   for v in self._dim_vars]
        for i, (sp, var) in enumerate(
                zip(self._dim_spinboxes, self._dim_vars)):
            if new_unit == "inch":
                lo, hi = self._dim_ranges_in[i]
                sp.config(from_=lo, to=hi,
                          increment=0.05 if hi <= 2 else 0.1)
                var.set(round(vals_mm[i] / 25.4, 3))
            else:
                lo, hi = self._dim_ranges_mm[i]
                sp.config(from_=lo, to=hi,
                          increment=1 if hi > 50 else 0.5)
                var.set(round(vals_mm[i], 3))
        self._dim_frame.config(text=f"Dimensions ({new_unit})")
        self._last_unit = new_unit
        self._on_dim_change()

    # ── Export / Generate ──

    def _generate_scad_code(self):
        p = self._get_dims_mm()
        if not p:
            messagebox.showerror("Error", "Invalid dimension values")
            return None
        return generate_scad_grid(
            width=p["width"], length=p["length"], depth=p["depth"],
            wall=p["wall"], corner_radius=p["corner_radius"],
            n_rows=self.preview._n_rows, n_cols=self.preview._n_cols,
            col_widths=self.preview._col_widths,
            row_heights=self.preview._row_heights,
            grid=self.preview._grid,
            gap=p["gap"],
            stacking=self.stack_var.get(),
            stack_height=self.stack_height_var.get(),
        )

    def _export_scad(self):
        code = self._generate_scad_code()
        if not code:
            return
        path = filedialog.asksaveasfilename(
            title="Save OpenSCAD File", defaultextension=".scad",
            filetypes=[("OpenSCAD files", "*.scad"), ("All files", "*.*")],
            initialfile="drawer_insert.scad",
        )
        if path:
            Path(path).write_text(code)
            self.status_var.set(f"Saved \u2192 {path}")

    def _export_stl(self):
        openscad_bin = find_openscad()
        if not openscad_bin:
            messagebox.showerror(
                "OpenSCAD Not Found",
                "OpenSCAD is not installed or not in PATH.\n\n"
                "Install it:\n  sudo apt install openscad\n\n"
                "Or export as .scad and render manually.",
            )
            return
        code = self._generate_scad_code()
        if not code:
            return
        path = filedialog.asksaveasfilename(
            title="Export STL", defaultextension=".stl",
            filetypes=[("STL files", "*.stl"), ("All files", "*.*")],
            initialfile="drawer_insert.stl",
        )
        if not path:
            return
        self.status_var.set("Rendering STL\u2026 (this may take a moment)")
        self.update_idletasks()
        try:
            export_stl(code, path, openscad_bin)
            self.status_var.set(f"Exported STL \u2192 {path}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))
            self.status_var.set("STL export failed")

    def _save_layout_json(self):
        path = filedialog.asksaveasfilename(
            title="Save Layout JSON", defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile="layout.json",
        )
        if not path:
            return
        data = {"grid": self.preview.get_grid_data()}
        Path(path).write_text(json.dumps(data, indent=4) + "\n")
        self.status_var.set(f"Layout saved \u2192 {path}")

    def _show_3d_preview(self):
        try:
            import trimesh
            import pyglet  # noqa: F401
        except ImportError:
            messagebox.showerror(
                "Missing Dependency",
                "This feature requires trimesh and pyglet.\n\n"
                "Install with:\n  pip install trimesh pyglet",
            )
            return
        code = self._generate_scad_code()
        if not code:
            return
        import subprocess
        import tempfile
        import os
        openscad_bin = find_openscad()
        if not openscad_bin:
            messagebox.showerror("OpenSCAD Not Found",
                                 "OpenSCAD is not installed or not in PATH.")
            return
        with tempfile.NamedTemporaryFile(suffix=".scad", delete=False) as f:
            f.write(code.encode("utf-8"))
            scad_path = f.name
        stl_path = scad_path.replace(".scad", ".stl")
        try:
            result = subprocess.run(
                [openscad_bin, "-o", stl_path, scad_path],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                messagebox.showerror("OpenSCAD Error", result.stderr)
                return
            mesh = trimesh.load(stl_path)
            mesh.show()
        except Exception as e:
            messagebox.showerror("3D Preview Error", str(e))
        finally:
            for p in (scad_path, stl_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _reset_defaults(self):
        for v, d in zip(self._dim_vars, self._dim_defaults_mm):
            if self.unit_var.get() == "inch":
                v.set(round(d / 25.4, 3))
            else:
                v.set(d)
        self.stack_var.set(False)
        self.stack_height_var.set(2.0)
        self._on_dim_change()

    def _show_help(self):
        messagebox.showinfo("Help",
            "Storage Insert Generator\n\n"
            "1. Choose units (mm/inch) at the top.\n"
            "2. Enter dimensions and wall/corner thickness.\n"
            "3. Adjust grid size with + Row / \u2212 Row / + Col / \u2212 Col.\n"
            "4. Click cells in the preview to select them.\n"
            "   Ctrl+click to select multiple.\n"
            "5. Right-click to merge adjacent cells (any shape!) or split.\n"
            "6. Drag dividers to adjust column/row proportions.\n"
            "7. Use Export buttons to save as .scad or .stl.\n"
        )


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
