#!/usr/bin/env python3

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Wnck", "3.0")
from gi.repository import Gdk, GLib, Gtk, Wnck


INT_RE = re.compile(r"(-?\d+)")


@dataclass(frozen=True)
class BorderStyle:
    red: float
    green: float
    blue: float
    alpha: float
    thickness: int
    radius: int
    inset: int


@dataclass(frozen=True)
class WindowGeometry:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class TargetWindow:
    window_id: str
    geometry: WindowGeometry
    fullscreen: bool


@dataclass(frozen=True)
class ProbeResult:
    target: Optional[TargetWindow]
    reason: str


class DebugLogger:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._last_by_key: dict[str, str] = {}
        self._started_at = time.monotonic()

    def log(self, message: str) -> None:
        if self.enabled:
            elapsed = time.monotonic() - self._started_at
            print(f"[focus-border +{elapsed:0.3f}s] {message}", file=sys.stderr, flush=True)

    def log_change(self, key: str, message: str) -> None:
        if not self.enabled:
            return
        if self._last_by_key.get(key) == message:
            return
        self._last_by_key[key] = message
        self.log(message)


class X11WindowProbe:
    def __init__(self, logger: DebugLogger) -> None:
        self.logger = logger
        self.screen = Wnck.Screen.get_default()

    def describe_active_window(self) -> ProbeResult:
        self.screen.force_update()
        window = self.screen.get_active_window()
        if not window:
            return ProbeResult(target=None, reason="no active window")

        if window.is_minimized():
            return ProbeResult(target=None, reason=f"window 0x{window.get_xid():x} is minimized")

        window_type = window.get_window_type()
        if window_type in (Wnck.WindowType.DESKTOP, Wnck.WindowType.DOCK):
            return ProbeResult(
                target=None,
                reason=f"window 0x{window.get_xid():x} ignored due to type {window_type}",
            )

        wnck_x, wnck_y, wnck_width, wnck_height = window.get_geometry()
        geometry = self._read_outer_geometry(window)
        if not geometry:
            if wnck_width < 1 or wnck_height < 1:
                return ProbeResult(target=None, reason=f"window 0x{window.get_xid():x} has invalid geometry")
            geometry = WindowGeometry(x=wnck_x, y=wnck_y, width=wnck_width, height=wnck_height)

        target = TargetWindow(
            window_id=f"0x{window.get_xid():x}",
            geometry=geometry,
            fullscreen=window.is_fullscreen(),
        )
        return ProbeResult(target=target, reason=f"resolved {target.window_id}")

    def _read_outer_geometry(self, window: Wnck.Window) -> Optional[WindowGeometry]:
        window_xid = window.get_xid()
        client_geometry = self._read_client_geometry(window_xid)
        if not client_geometry:
            return None
        extents = self._read_frame_extents(window_xid)
        if not extents:
            return client_geometry

        left, right, top, bottom, source = extents
        if source == "_GTK_FRAME_EXTENTS":
            x, y, width, height = window.get_geometry()
            if width >= 1 and height >= 1:
                return WindowGeometry(x=x, y=y, width=width, height=height)

        return WindowGeometry(
            x=client_geometry.x - left,
            y=client_geometry.y - top,
            width=max(1, client_geometry.width + left + right),
            height=max(1, client_geometry.height + top + bottom),
        )

    def _read_client_geometry(self, window_xid: int) -> Optional[WindowGeometry]:
        output = self._run(["xwininfo", "-id", f"0x{window_xid:x}"])
        if not output:
            return None

        values: dict[str, int] = {}
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith("Absolute upper-left X:"):
                values["x"] = self._parse_int(line)
            elif line.startswith("Absolute upper-left Y:"):
                values["y"] = self._parse_int(line)
            elif line.startswith("Width:"):
                values["width"] = self._parse_int(line)
            elif line.startswith("Height:"):
                values["height"] = self._parse_int(line)
            elif line.startswith("Map State:"):
                values["mapped"] = 1 if "IsViewable" in line else 0

        if values.get("mapped") != 1:
            return None

        required = ("x", "y", "width", "height")
        if not all(key in values for key in required):
            return None

        return WindowGeometry(
            x=values["x"],
            y=values["y"],
            width=max(1, values["width"]),
            height=max(1, values["height"]),
        )

    def _read_frame_extents(self, window_xid: int) -> Optional[tuple[int, int, int, int, str]]:
        output = self._run(
            ["xprop", "-id", f"0x{window_xid:x}", "_GTK_FRAME_EXTENTS", "_NET_FRAME_EXTENTS"]
        )
        if not output:
            return None

        gtk_extents: Optional[tuple[int, int, int, int, str]] = None
        net_extents: Optional[tuple[int, int, int, int, str]] = None
        for raw_line in output.splitlines():
            line = raw_line.strip()
            values = [int(value) for value in INT_RE.findall(line)]
            if len(values) < 4:
                continue
            if line.startswith("_GTK_FRAME_EXTENTS("):
                left, right, top, bottom = values[-4:]
                gtk_extents = (left, right, top, bottom, "_GTK_FRAME_EXTENTS")
            elif line.startswith("_NET_FRAME_EXTENTS("):
                left, right, top, bottom = values[-4:]
                net_extents = (left, right, top, bottom, "_NET_FRAME_EXTENTS")

        return gtk_extents or net_extents

    def _run(self, args: list[str]) -> Optional[str]:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=0.20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.log_change(f"probe-command:{' '.join(args)}", f"probe command failed: {' '.join(args)}: {exc}")
            return None

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            self.logger.log_change(
                f"probe-command:{' '.join(args)}",
                f"probe command returned {completed.returncode}: {' '.join(args)}" + (f": {stderr}" if stderr else ""),
            )
            return None
        return completed.stdout

    @staticmethod
    def _parse_int(line: str) -> int:
        match = INT_RE.search(line)
        if not match:
            raise ValueError(f"expected integer in line: {line}")
        return int(match.group(1))


class BorderWindow(Gtk.Window):
    def __init__(self, style: BorderStyle, logger: DebugLogger) -> None:
        super().__init__(type=Gtk.WindowType.POPUP)
        self.style = style
        self.logger = logger
        self.geometry = WindowGeometry(0, 0, 1, 1)

        self.set_title("Focus Border")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_app_paintable(True)
        self.stick()

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.connect("draw", self._on_draw)
        self.connect("realize", self._on_realize)

    def update_geometry(self, geometry: WindowGeometry) -> None:
        self.geometry = geometry
        self._apply_geometry()
        self.queue_draw()

    def expected_overlay_bounds(self) -> tuple[int, int, int, int]:
        outset = self.style.thickness + self.style.inset
        x = self.geometry.x - outset
        y = self.geometry.y - outset
        width = self.geometry.width + (outset * 2)
        height = self.geometry.height + (outset * 2)
        return x, y, width, height

    def needs_geometry_sync(self) -> bool:
        _x, _y, width, height = self.expected_overlay_bounds()
        allocation = self.get_allocation()
        return allocation.width != width or allocation.height != height

    def _apply_geometry(self) -> None:
        x, y, width, height = self.expected_overlay_bounds()

        self.move(x, y)
        self.resize(width, height)

        gdk_window = self.get_window()
        if gdk_window:
            gdk_window.move_resize(x, y, width, height)
            self._apply_input_passthrough(gdk_window)

    def _on_realize(self, *_args: object) -> None:
        gdk_window = self.get_window()
        if not gdk_window:
            return

        self._apply_geometry()
        self._apply_input_passthrough(gdk_window)

    @staticmethod
    def _apply_input_passthrough(gdk_window: Gdk.Window) -> None:
        gdk_window.set_pass_through(True)
        gdk_window.input_shape_combine_region(cairo.Region(), 0, 0)

    def _on_draw(self, _widget: Gtk.Widget, context: cairo.Context) -> bool:
        allocation = self.get_allocation()
        width = allocation.width
        height = allocation.height

        context.set_operator(cairo.OPERATOR_SOURCE)
        context.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        context.paint()

        thickness = max(1, self.style.thickness)
        radius = max(0, self.style.radius)
        inset = thickness / 2.0
        draw_width = max(1.0, width - thickness)
        draw_height = max(1.0, height - thickness)

        context.set_operator(cairo.OPERATOR_OVER)
        context.set_source_rgba(self.style.red, self.style.green, self.style.blue, self.style.alpha)
        context.set_line_width(thickness)
        self._rounded_rectangle(context, inset, inset, draw_width, draw_height, radius)
        context.stroke()
        return True

    @staticmethod
    def _rounded_rectangle(
        context: cairo.Context, x: float, y: float, width: float, height: float, radius: float
    ) -> None:
        radius = min(radius, width / 2.0, height / 2.0)
        if radius <= 0:
            context.rectangle(x, y, width, height)
            return

        context.new_sub_path()
        context.arc(x + width - radius, y + radius, radius, -1.5708, 0.0)
        context.arc(x + width - radius, y + height - radius, radius, 0.0, 1.5708)
        context.arc(x + radius, y + height - radius, radius, 1.5708, 3.1416)
        context.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
        context.close_path()


class FocusBorderApp(Gtk.Application):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(application_id="io.github.sander.FocusBorder")
        self.args = args
        self.logger = DebugLogger(args.debug)
        self.style = BorderStyle(
            red=args.color[0],
            green=args.color[1],
            blue=args.color[2],
            alpha=args.alpha,
            thickness=args.thickness,
            radius=args.radius,
            inset=args.inset,
        )
        self.probe = X11WindowProbe(self.logger)
        self.border_window: Optional[BorderWindow] = None
        self.last_target: Optional[TargetWindow] = None
        self.last_status: Optional[str] = None

    def do_activate(self) -> None:
        if not self.border_window:
            self.border_window = BorderWindow(self.style, self.logger)
            self.add_window(self.border_window)
            self.logger.log("created overlay window")
        GLib.idle_add(self._start_refresh_loop)

    def _start_refresh_loop(self) -> bool:
        self._refresh()
        GLib.timeout_add(self.args.poll_ms, self._refresh)
        return False

    def _refresh(self) -> bool:
        if not self.border_window:
            return True

        result = self.probe.describe_active_window()
        target = result.target
        if not target:
            self._hide_border(result.reason)
            return True

        if target.fullscreen:
            self._hide_border(f"{target.window_id} is fullscreen")
            return True

        if not self.border_window.get_visible():
            self.border_window.show_all()

        if target != self.last_target or self.border_window.needs_geometry_sync():
            self.border_window.update_geometry(target.geometry)
            self.last_target = target
            self._set_status(self._format_target_status(target))
        return True

    def _hide_border(self, reason: str) -> None:
        if self.border_window and self.border_window.get_visible():
            self.border_window.hide()
        self.last_target = None
        self._set_status(f"hidden: {reason}")

    @staticmethod
    def _format_target_status(target: TargetWindow) -> str:
        return (
            f"showing border for {target.window_id} at "
            f"{target.geometry.x},{target.geometry.y} "
            f"{target.geometry.width}x{target.geometry.height}"
        )

    def _set_status(self, status: str) -> None:
        if status != self.last_status:
            self.logger.log(status)
            self.last_status = status


def parse_color(value: str) -> tuple[float, float, float]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise argparse.ArgumentTypeError("color must be a 6-digit hex value like ff4d4d")

    try:
        red = int(value[0:2], 16) / 255.0
        green = int(value[2:4], 16) / 255.0
        blue = int(value[4:6], 16) / 255.0
    except ValueError as exc:
        raise argparse.ArgumentTypeError("color must be hexadecimal") from exc

    return red, green, blue


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a click-through border overlay around the active X11 window."
    )
    parser.add_argument("--color", type=parse_color, default=parse_color("ff5f57"))
    parser.add_argument("--alpha", type=float, default=0.95)
    parser.add_argument("--thickness", type=int, default=4)
    parser.add_argument("--radius", type=int, default=10)
    parser.add_argument("--inset", type=int, default=0)
    parser.add_argument("--poll-ms", type=int, default=50)
    parser.add_argument("--install-autostart", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser


def install_autostart() -> int:
    source = Path(__file__).with_name("io.github.sander.FocusBorder.desktop")
    target_dir = Path.home() / ".config" / "autostart"
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target_dir / source.name)
    print(f"Installed autostart entry to {target_dir / source.name}")
    return 0


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if not (0.0 <= args.alpha <= 1.0):
        parser.error("--alpha must be between 0.0 and 1.0")
    if args.thickness < 1:
        parser.error("--thickness must be at least 1")
    if args.radius < 0:
        parser.error("--radius cannot be negative")
    if args.inset < 0:
        parser.error("--inset cannot be negative")
    if args.poll_ms < 16:
        parser.error("--poll-ms must be at least 16")


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    validate_args(args, parser)

    if args.install_autostart:
        return install_autostart()

    if not os.environ.get("DISPLAY"):
        print("DISPLAY is not set; this app needs an X11 session.", file=sys.stderr)
        return 1

    if args.debug:
        print(
            f"[focus-border] starting with DISPLAY={os.environ.get('DISPLAY')} "
            f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE', '')}",
            file=sys.stderr,
            flush=True,
        )

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = FocusBorderApp(args)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
