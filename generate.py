#!/usr/bin/env python3

"""
Storage Insert Generator

Generates parametric storage insert style drawer organizers using OpenSCAD,
with export to STL (or any OpenSCAD-supported format).

Usage:
    python generate.py --width 300 --length 400 --depth 50 --wall 2
    python generate.py --width 300 --length 400 --depth 50 --wall 2 --layout grid 3 2
    python generate.py --width 300 --length 400 --depth 50 --wall 2 --layout custom --config layout.json
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def find_openscad() -> str:
    """Locate the OpenSCAD binary."""
    # Common paths
    candidates = [
        "openscad",
        "/usr/bin/openscad",
        "/usr/local/bin/openscad",
        "/snap/bin/openscad",
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
    ]
    for c in candidates:
        if shutil.which(c):
            return c
    return ""


def generate_scad(
    width: float,
    length: float,
    depth: float,
    wall: float,
    corner_radius: float,
    layout: list[list[float]],
) -> str:
    """
    Generate OpenSCAD code for a storage insert drawer organizer.

    Parameters
    ----------
    width : outer width (X) in mm
    length : outer length (Y) in mm
    depth : outer depth (Z) in mm
    wall : wall / divider thickness in mm
    corner_radius : fillet radius on outer corners
    layout : 2D list of relative cell proportions.
             Each row is a horizontal band; values within a row
             define the relative widths of compartments in that band.
             Example: [[2, 1], [1, 1, 1]] -> top row split 2:1,
             bottom row split 1:1:1.
    """

    # Precompute band heights (rows share height equally by default)
    num_rows = len(layout)

    lines = [
        "// ===== Auto-generated storage insert drawer =====",
        f"// Outer dims: {width} x {length} x {depth} mm, wall: {wall} mm",
        "",
        "/* [Dimensions] */",
        f"outer_w = {width};",
        f"outer_l = {length};",
        f"outer_d = {depth};",
        f"wall    = {wall};",
        f"cr      = {corner_radius};",
        f"num_rows = {num_rows};",
        "",
        "$fn = 40;",
        "",
        "// Rounded-rectangle helper",
        "module rrect(w, l, h, r) {",
        "    r_ = min(r, w/2, l/2);",
        "    translate([r_, r_, 0])",
        "        minkowski() {",
        "            cube([w - 2*r_, l - 2*r_, h/2]);",
        "            cylinder(r=r_, h=h/2);",
        "        }",
        "}",
        "",
        "// Main insert",
        "module insert() {",
        "    difference() {",
        "        // Outer shell",
        "        rrect(outer_w, outer_l, outer_d, cr);",
        "",
        "        // Hollow interior",
        "        translate([wall, wall, wall])",
        "            rrect(outer_w - 2*wall, outer_l - 2*wall, outer_d, max(cr - wall, 0.01));",
        "    }",
        "",
    ]

    # --- Divider generation ---
    inner_w = width - 2 * wall
    inner_l = length - 2 * wall

    # Horizontal dividers (between rows)
    band_height = inner_l / num_rows
    for i in range(1, num_rows):
        y = wall + i * band_height - wall / 2
        lines.append(f"    // Horizontal divider row {i}")
        lines.append(f"    translate([wall, {y:.4f}, 0])")
        lines.append(f"        cube([{inner_w:.4f}, wall, outer_d]);")
        lines.append("")

    # Vertical dividers within each row
    for row_idx, row in enumerate(layout):
        total_parts = sum(row)
        num_cells = len(row)
        y_start = wall + row_idx * band_height
        if row_idx > 0:
            y_start += wall / 2
        row_inner_l = band_height
        if row_idx > 0:
            row_inner_l -= wall / 2
        if row_idx < num_rows - 1:
            row_inner_l -= wall / 2

        cumulative = 0.0
        for cell_idx in range(num_cells - 1):
            cumulative += row[cell_idx]
            x = wall + (cumulative / total_parts) * inner_w - wall / 2
            lines.append(
                f"    // Vertical divider row {row_idx}, after cell {cell_idx}"
            )
            lines.append(f"    translate([{x:.4f}, {y_start:.4f}, 0])")
            lines.append(f"        cube([wall, {row_inner_l:.4f}, outer_d]);")
            lines.append("")

    lines.append("}")
    lines.append("")
    lines.append("insert();")
    lines.append("")

    return "\n".join(lines)


def generate_scad_grid(
    width: float,
    length: float,
    depth: float,
    wall: float,
    corner_radius: float,
    n_rows: int,
    n_cols: int,
    col_widths: list[float],
    row_heights: list[float],
    grid: list[list[int]],
    gap: float = 0.0,
    stacking: bool = False,
    stack_height: float = 2.0,
) -> str:
    """
    Generate OpenSCAD code for a grid-based layout with merged cells.

    The grid is a 2D array of cell IDs.  Adjacent grid positions with the
    same ID belong to the same compartment (merged cell).  Dividers are
    generated only along edges where the cell ID changes.

    gap : clearance subtracted from each side of the outer perimeter
          so the insert fits the drawer cavity.
    stacking : if True, add interlocking lip (top) and groove (bottom)
               so inserts can stack vertically.
    stack_height : height of the stacking lip/groove in mm.
    """

    # Apply gap to outer dimensions
    width = width - 2 * gap
    length = length - 2 * gap

    inner_w = width - 2 * wall
    inner_l = length - 2 * wall
    total_cw = sum(col_widths) or 1
    total_rh = sum(row_heights) or 1

    # Grid line positions in mm
    col_x = [wall]
    for cw in col_widths:
        col_x.append(col_x[-1] + (cw / total_cw) * inner_w)
    row_y = [wall]
    for rh in row_heights:
        row_y.append(row_y[-1] + (rh / total_rh) * inner_l)

    lines = [
        "// ===== Auto-generated storage insert drawer =====",
        f"// Outer dims: {width} x {length} x {depth} mm, wall: {wall} mm, gap: {gap} mm",
        f"// Grid: {n_rows} rows x {n_cols} cols",
        "",
        "/* [Dimensions] */",
        f"outer_w = {width};",
        f"outer_l = {length};",
        f"outer_d = {depth};",
        f"wall    = {wall};",
        f"cr      = {corner_radius};",
    ]

    if stacking:
        # Bowl-style nesting:
        # Top: elliptical quarter-round scoops the inner wall.
        #   Horizontal radius = wall (spans full wall thickness).
        #   Vertical radius = 2*wall (how far inner wall drops from top).
        #   At outer face: wall stays at full height.
        #   At inner face: wall drops by 2*wall, creating the receiving bowl.
        # Bottom: outer wall steps inward. Convex quarter-round transition.
        half_wall = round(wall / 2, 4)
        step_inset = round(half_wall + gap, 4)
        cr_step  = f"max(cr - {step_inset}, 0.01)"
        r_h = wall             # horizontal semi-axis (wall thickness)
        r_v = 2 * wall         # vertical semi-axis (drop depth)
        lines += [
            f"stack_h  = {stack_height};",
        ]

    lines += [
        "",
        "$fn = 40;",
        "",
        "module rrect(w, l, h, r) {",
        "    r_ = min(r, w/2, l/2);",
        "    translate([r_, r_, 0])",
        "        minkowski() {",
        "            cube([w - 2*r_, l - 2*r_, h/2]);",
        "            cylinder(r=r_, h=h/2);",
        "        }",
        "}",
        "",
        "// Main insert",
        "module insert() {",
        "    difference() {",
        "        // Outer shell",
        "        rrect(outer_w, outer_l, outer_d, cr);",
        "",
        "        // Hollow interior",
        "        translate([wall, wall, wall])",
        "            rrect(outer_w - 2*wall, outer_l - 2*wall, outer_d, max(cr - wall, 0.01));",
        "    }",
        "",
    ]

    # Collect divider centerline segments for 2D offset approach
    # (offset naturally rounds all corners: L-shapes, T-junctions, etc.)
    h_segs = []  # (x_start, x_end, y_center)
    for r_b in range(1, n_rows):
        c = 0
        while c < n_cols:
            if grid[r_b - 1][c] != grid[r_b][c]:
                c_start = c
                while c < n_cols and grid[r_b - 1][c] != grid[r_b][c]:
                    c += 1
                h_segs.append((col_x[c_start], col_x[c], row_y[r_b]))
            else:
                c += 1

    v_segs = []  # (x_center, y_start, y_end)
    for c_b in range(1, n_cols):
        r = 0
        while r < n_rows:
            if grid[r][c_b - 1] != grid[r][c_b]:
                r_start = r
                while r < n_rows and grid[r][c_b - 1] != grid[r][c_b]:
                    r += 1
                v_segs.append((col_x[c_b], row_y[r_start], row_y[r]))
            else:
                r += 1

    if h_segs or v_segs:
        lines.append("    // Dividers with rounded corners")
        lines.append("    linear_extrude(height=outer_d)")
        lines.append("        offset(r=wall/2) {")
        for xs, xe, yc in h_segs:
            lines.append(f"            translate([{xs:.4f}, {yc - 0.005:.4f}])")
            lines.append(f"                square([{xe - xs:.4f}, 0.01]);")
        for xc, ys, ye in v_segs:
            lines.append(f"            translate([{xc - 0.005:.4f}, {ys:.4f}])")
            lines.append(f"                square([0.01, {ye - ys:.4f}]);")
        lines.append("        }")
        lines.append("")

    lines.append("}")
    lines.append("")

    if stacking:
        N = 8  # segments for quarter-circle/ellipse approximation
        fr_step = step_inset   # bottom convex round radius

        lines.append("// Bowl-style stacking with rounded transitions")
        lines.append("difference() {")
        lines.append("    insert();")
        lines.append("")

        # ── Top: elliptical quarter-round scoops inner wall ──
        # Ellipse: h-radius = wall (spans wall), v-radius = 2*wall (drop).
        # t=0:   inset=0 (outer face), z=outer_d (top)
        # t=π/2: inset=wall (inner face), z=outer_d - 2*wall
        lines.append(f"    // Top bowl scoop: h_r={r_h}, v_r={r_v} ({N} segments)")
        for i in range(N):
            t0 = math.pi / 2 * i / N
            t1 = math.pi / 2 * (i + 1) / N
            in0 = round(r_h * math.sin(t0), 4)
            z0  = round(depth - r_v * (1 - math.cos(t0)), 4)
            in1 = round(r_h * math.sin(t1), 4)
            z1  = round(depth - r_v * (1 - math.cos(t1)), 4)
            lines.append(f"    hull() {{")
            lines.append(f"        translate([{in0}, {in0}, {z0}])")
            lines.append(f"            rrect(outer_w - {round(2*in0,4)}, outer_l - {round(2*in0,4)},"
                          f" 0.01, max(cr - {in0}, 0.01));")
            lines.append(f"        translate([{in1}, {in1}, {z1}])")
            lines.append(f"            rrect(outer_w - {round(2*in1,4)}, outer_l - {round(2*in1,4)},"
                          f" 0.01, max(cr - {in1}, 0.01));")
            lines.append(f"    }}")
        lines.append("")

        # ── Bottom step: ring removal with convex quarter-round ──
        # Remove ring = cube - (step_column ∪ round_segments)
        lines.append(f"    // Bottom step with convex round")
        lines.append(f"    difference() {{")
        lines.append(f"        cube([outer_w, outer_l, {round(stack_height + fr_step, 4)}]);")
        lines.append(f"        union() {{")
        # Straight step column
        lines.append(f"            translate([{step_inset}, {step_inset}, -0.01])")
        lines.append(f"                rrect(outer_w - {round(2*step_inset,4)},"
                      f" outer_l - {round(2*step_inset,4)},"
                      f" {round(stack_height + 0.02, 4)}, {cr_step});")
        # Quarter-circle in (inset, z) space:
        #   center = (0, stack_height)
        #   θ=0  → (step_inset,  stack_height)             = step corner
        #   θ=π/2→ (0,           stack_height + fr_step)   = full outer
        lines.append(f"            // Convex round ({N} segments)")
        for i in range(N):
            a0 = math.pi / 2 * i / N
            a1 = math.pi / 2 * (i + 1) / N
            in0 = round(step_inset * math.cos(a0), 4)
            z0  = round(stack_height + fr_step * math.sin(a0), 4)
            in1 = round(step_inset * math.cos(a1), 4)
            z1  = round(stack_height + fr_step * math.sin(a1), 4)
            lines.append(f"            hull() {{")
            lines.append(f"                translate([{in0}, {in0}, {z0}])")
            lines.append(f"                    rrect(outer_w - {round(2*in0,4)},"
                          f" outer_l - {round(2*in0,4)},"
                          f" 0.01, max(cr - {in0}, 0.01));")
            lines.append(f"                translate([{in1}, {in1}, {z1}])")
            lines.append(f"                    rrect(outer_w - {round(2*in1,4)},"
                          f" outer_l - {round(2*in1,4)},"
                          f" 0.01, max(cr - {in1}, 0.01));")
            lines.append(f"            }}")
        lines.append(f"        }}")
        lines.append(f"    }}")
        lines.append("}")
    else:
        lines.append("insert();")

    lines.append("")

    return "\n".join(lines)


def default_layout() -> list[list[float]]:
    """Classic storage insert: large compartment + 2 small on top, 3 equal on bottom."""
    return [
        [2, 1],
        [1, 1, 1],
    ]


def grid_layout(cols: int, rows: int) -> list[list[float]]:
    """Uniform grid of cols x rows equal compartments."""
    return [[1.0] * cols for _ in range(rows)]


def load_custom_layout(path: str) -> list[list[float]]:
    """
    Load a layout from a JSON file.

    Expected format:
    {
        "layout": [[2, 1], [1, 1, 1]]
    }
    """
    with open(path, "r") as f:
        data = json.load(f)
    layout = data.get("layout")
    if not layout or not isinstance(layout, list):
        raise ValueError("JSON must contain a 'layout' key with a 2D list of numbers")
    for row in layout:
        if not isinstance(row, list) or not all(isinstance(v, (int, float)) for v in row):
            raise ValueError(f"Each row must be a list of numbers, got: {row}")
        if any(v <= 0 for v in row):
            raise ValueError("All proportions must be positive")
    return layout


def export_stl(scad_code: str, output_path: str, openscad_bin: str) -> None:
    """Write SCAD to a temp file, invoke OpenSCAD to export."""
    with tempfile.NamedTemporaryFile(
        suffix=".scad", mode="w", delete=False
    ) as tmp:
        tmp.write(scad_code)
        tmp_path = tmp.name

    try:
        cmd = [openscad_bin, "-o", output_path, tmp_path]
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"OpenSCAD stderr:\n{result.stderr}", file=sys.stderr)
            raise RuntimeError(f"OpenSCAD exited with code {result.returncode}")
        print(f"Exported → {output_path}")
    finally:
        os.unlink(tmp_path)


def main():
    parser = argparse.ArgumentParser(
        description="Generate a storage insert drawer organizer (OpenSCAD → STL)"
    )
    parser.add_argument(
        "--width", type=float, required=True, help="Outer width in mm (X axis)"
    )
    parser.add_argument(
        "--length", type=float, required=True, help="Outer length in mm (Y axis)"
    )
    parser.add_argument(
        "--depth", type=float, required=True, help="Outer depth in mm (Z axis)"
    )
    parser.add_argument(
        "--wall", type=float, default=2.0, help="Wall / divider thickness in mm (default: 2)"
    )
    parser.add_argument(
        "--corner-radius",
        type=float,
        default=3.0,
        help="Outer corner fillet radius in mm (default: 3)",
    )
    parser.add_argument(
        "--layout",
        nargs="+",
        default=["storage"],
        help=(
            "Layout mode: 'storage' (default), 'grid COLS ROWS', "
            "or 'custom PATH_TO_JSON'"
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default="drawer_insert.stl",
        help="Output file path (default: drawer_insert.stl)",
    )
    parser.add_argument(
        "--scad-only",
        action="store_true",
        help="Only write the .scad file, skip STL export",
    )
    parser.add_argument(
        "--scad-output",
        type=str,
        default="drawer_insert.scad",
        help="Path for the .scad file (default: drawer_insert.scad)",
    )

    args = parser.parse_args()

    # --- Resolve layout ---
    mode = args.layout[0].lower()
    if mode == "storage":
        layout = default_layout()
    elif mode == "grid":
        if len(args.layout) != 3:
            parser.error("grid layout requires: --layout grid COLS ROWS")
        cols, rows = int(args.layout[1]), int(args.layout[2])
        layout = grid_layout(cols, rows)
    elif mode == "custom":
        if len(args.layout) != 2:
            parser.error("custom layout requires: --layout custom PATH_TO_JSON")
        layout = load_custom_layout(args.layout[1])
    else:
        parser.error(f"Unknown layout mode: {mode}")

    # --- Validate ---
    if args.width <= 0 or args.length <= 0 or args.depth <= 0:
        parser.error("Dimensions must be positive")
    if args.wall <= 0:
        parser.error("Wall thickness must be positive")
    min_dim = min(args.width, args.length)
    max_walls = max(len(row) for row in layout)
    if args.wall * (max_walls + 1) >= min_dim:
        parser.error(
            f"Wall thickness too large for the number of compartments "
            f"({max_walls + 1} walls × {args.wall} mm ≥ {min_dim} mm)"
        )

    # --- Generate ---
    scad_code = generate_scad(
        width=args.width,
        length=args.length,
        depth=args.depth,
        wall=args.wall,
        corner_radius=args.corner_radius,
        layout=layout,
    )

    # Always save the .scad file
    scad_path = Path(args.scad_output)
    scad_path.write_text(scad_code)
    print(f"Wrote OpenSCAD source → {scad_path}")

    if args.scad_only:
        return

    # --- Export ---
    openscad_bin = find_openscad()
    if not openscad_bin:
        print(
            "WARNING: OpenSCAD not found in PATH. "
            "Install it (https://openscad.org) or use --scad-only.",
            file=sys.stderr,
        )
        print("Skipping STL export. You can render manually with:")
        print(f"  openscad -o {args.output} {scad_path}")
        return

    export_stl(scad_code, args.output, openscad_bin)


if __name__ == "__main__":
    main()
