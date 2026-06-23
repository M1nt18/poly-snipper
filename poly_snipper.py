#!/usr/bin/env python3
"""A small Windows screenshot tool inspired by Snipaste.

Hotkey: Alt+A
Features: region capture, copy image to clipboard, auto-save PNG, annotation editor.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable

from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageTk

try:
    import pystray
except ImportError:  # The script still works without tray support during development.
    pystray = None


APP_NAME = "Poly Snipper"
APP_VERSION = "0.1.8"
RELEASES_API = "https://api.github.com/repos/M1nt18/poly-snipper/releases/latest"
LATEST_INSTALLER_URL = "https://github.com/M1nt18/poly-snipper/releases/latest/download/PolySnipperSetup.exe"
HOTKEY_ID = 0x504F4C59
MOD_ALT = 0x0001
VK_A = 0x41
WM_HOTKEY = 0x0312

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.CloseClipboard.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


@dataclass(frozen=True)
class VirtualScreen:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


def get_virtual_screen() -> VirtualScreen:
    return VirtualScreen(
        left=user32.GetSystemMetrics(76),
        top=user32.GetSystemMetrics(77),
        width=user32.GetSystemMetrics(78),
        height=user32.GetSystemMetrics(79),
    )


def screenshots_dir() -> Path:
    path = Path.home() / "Pictures" / "PolySnips"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_capture(image: Image.Image) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = screenshots_dir() / f"snip-{stamp}.png"
    image.save(path)
    return path


def parse_version(value: str) -> tuple[int, ...]:
    clean = value.strip().lstrip("vV")
    parts: list[int] = []
    for part in clean.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits or "0"))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer_version(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def copy_image_to_clipboard(image: Image.Image) -> None:
    output = io.BytesIO()
    image.convert("RGB").save(output, "BMP")
    dib = output.getvalue()[14:]
    output.close()

    GMEM_MOVEABLE = 0x0002
    CF_DIB = 8
    h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
    if not h_global:
        raise OSError("GlobalAlloc failed")

    locked = kernel32.GlobalLock(h_global)
    if not locked:
        kernel32.GlobalFree(h_global)
        raise OSError("GlobalLock failed")

    ctypes.memmove(locked, dib, len(dib))
    kernel32.GlobalUnlock(h_global)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(h_global)
        raise OSError("OpenClipboard failed")

    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_DIB, h_global):
            kernel32.GlobalFree(h_global)
            raise OSError("SetClipboardData failed")
        h_global = None
    finally:
        user32.CloseClipboard()


def grab_virtual_screen(screen: VirtualScreen) -> Image.Image:
    try:
        image = ImageGrab.grab(all_screens=True)
    except TypeError:
        image = ImageGrab.grab(bbox=(screen.left, screen.top, screen.right, screen.bottom))
    if image.size != (screen.width, screen.height):
        image = ImageGrab.grab(bbox=(screen.left, screen.top, screen.right, screen.bottom))
    return image.convert("RGB")


class HotkeyListener(threading.Thread):
    def __init__(self, events: queue.Queue[str]) -> None:
        super().__init__(daemon=True)
        self.events = events
        self.thread_id: int | None = None

    def run(self) -> None:
        self.thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT, VK_A):
            self.events.put("hotkey_failed")
            return

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.events.put("capture")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self) -> None:
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)


class CaptureOverlay(tk.Toplevel):
    def __init__(self, app: "PolySnipperApp", screen: VirtualScreen, image: Image.Image) -> None:
        super().__init__(app.root)
        self.app = app
        self.screen = screen
        self.image = image
        self.start: tuple[int, int] | None = None
        self.rect_id: int | None = None
        self.label_id: int | None = None
        self.mask_ids: list[int] = []

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{screen.width}x{screen.height}+{screen.left}+{screen.top}")
        self.configure(cursor="crosshair")

        self.photo = ImageTk.PhotoImage(image)
        self.canvas = tk.Canvas(self, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.mask_ids = [
            self.canvas.create_rectangle(0, 0, screen.width, screen.height, fill="#000000", stipple="gray50", outline=""),
            self.canvas.create_rectangle(0, 0, 0, 0, fill="#000000", stipple="gray50", outline=""),
            self.canvas.create_rectangle(0, 0, 0, 0, fill="#000000", stipple="gray50", outline=""),
            self.canvas.create_rectangle(0, 0, 0, 0, fill="#000000", stipple="gray50", outline=""),
        ]
        self.help_id = self.canvas.create_text(
            16,
            16,
            text="拖动选择区域，Esc 取消",
            fill="#ffffff",
            anchor="nw",
            font=("Segoe UI", 13),
        )

        self.bind("<Escape>", lambda _event: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.focus_force()

    def cancel(self) -> None:
        self.destroy()

    def on_press(self, event: tk.Event) -> None:
        self.start = (int(event.x), int(event.y))
        self.update_masks(event.x, event.y, event.x, event.y)
        self.rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#35d4ff",
            width=2,
        )
        self.label_id = self.canvas.create_text(
            event.x + 8,
            event.y - 24,
            text="",
            fill="#ffffff",
            anchor="nw",
            font=("Segoe UI", 11, "bold"),
        )

    def on_drag(self, event: tk.Event) -> None:
        if not self.start or not self.rect_id:
            return
        x0, y0 = self.start
        x1 = max(0, min(self.screen.width, int(event.x)))
        y1 = max(0, min(self.screen.height, int(event.y)))
        self.update_masks(x0, y0, x1, y1)
        self.canvas.coords(self.rect_id, x0, y0, x1, y1)
        if self.label_id:
            width = abs(x1 - x0)
            height = abs(y1 - y0)
            self.canvas.coords(self.label_id, min(x0, x1) + 8, min(y0, y1) - 24)
            self.canvas.itemconfigure(self.label_id, text=f"{width} x {height}")

    def update_masks(self, x0: int, y0: int, x1: int, y1: int) -> None:
        left, right = sorted((max(0, min(self.screen.width, int(x0))), max(0, min(self.screen.width, int(x1)))))
        top, bottom = sorted((max(0, min(self.screen.height, int(y0))), max(0, min(self.screen.height, int(y1)))))
        regions = [
            (0, 0, self.screen.width, top),
            (0, bottom, self.screen.width, self.screen.height),
            (0, top, left, bottom),
            (right, top, self.screen.width, bottom),
        ]
        for mask_id, coords in zip(self.mask_ids, regions):
            self.canvas.coords(mask_id, *coords)

    def on_release(self, event: tk.Event) -> None:
        if not self.start:
            self.cancel()
            return
        x0, y0 = self.start
        x1 = max(0, min(self.screen.width, int(event.x)))
        y1 = max(0, min(self.screen.height, int(event.y)))
        left, right = sorted((x0, x1))
        top, bottom = sorted((y0, y1))
        if right - left < 4 or bottom - top < 4:
            self.cancel()
            return
        crop = self.image.crop((left, top, right, bottom))
        origin = (self.screen.left + left, self.screen.top + top)
        self.destroy()
        self.app.finish_capture(crop, origin)


class EditorWindow(tk.Toplevel):
    def __init__(
        self,
        app: "PolySnipperApp",
        image: Image.Image,
        path: Path,
        origin: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(app.root)
        self.app = app
        self.image = image
        self.path = path
        self.tool = tk.StringVar(value="pen")
        self.color = tk.StringVar(value="#ff2d2d")
        self.stroke_width = tk.IntVar(value=3)
        self.text_value = tk.StringVar(value="文字")
        self.items: list[dict] = []
        self.active_canvas_item: int | None = None
        self.active_data: dict | None = None
        self.pen_points: list[tuple[float, float]] = []
        self.selected_index: int | None = None
        self.moving_index: int | None = None
        self.move_last: tuple[float, float] | None = None
        self.clipboard_after_id: str | None = None

        self.title(f"{APP_NAME} - {path.name}")
        self.attributes("-topmost", True)
        self.configure(bg="#191919")

        max_w = int(self.winfo_screenwidth() * 0.82)
        max_h = int(self.winfo_screenheight() * 0.72)
        display = image.copy()
        display.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
        self.scale = display.width / image.width
        self.photo = ImageTk.PhotoImage(display)

        self.canvas = tk.Canvas(
            self,
            width=display.width,
            height=display.height,
            highlightthickness=0,
            bg="#111111",
            cursor="crosshair",
        )
        self.canvas.pack(side="top")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        toolbar = tk.Frame(self, bg="#202020")
        toolbar.pack(side="bottom", fill="x")
        for label, tool in [
            ("移动", "move"),
            ("画笔", "pen"),
            ("矩形", "rect"),
            ("圆圈", "ellipse"),
            ("箭头", "arrow"),
            ("马赛克", "mosaic"),
            ("文字", "text"),
        ]:
            tk.Radiobutton(
                toolbar,
                text=label,
                value=tool,
                variable=self.tool,
                indicatoron=False,
                width=6,
                height=1,
                bg="#303030",
                fg="#ffffff",
                selectcolor="#155e75",
                font=("Microsoft YaHei UI", 10, "bold"),
            ).pack(side="left", padx=(4, 0), pady=4)
        self.tool.trace_add("write", lambda *_args: self.on_tool_changed())
        tk.Entry(toolbar, textvariable=self.text_value, width=14).pack(side="left", padx=6, pady=4)
        for color in ["#ff2d2d", "#ffd400", "#2dd4bf", "#ffffff", "#111111"]:
            tk.Button(
                toolbar,
                width=2,
                bg=color,
                activebackground=color,
                command=lambda value=color: self.color.set(value),
            ).pack(side="left", padx=(0, 3), pady=4)
        tk.Button(toolbar, text="-", width=3, command=self.decrease_width).pack(side="left", padx=(4, 0), pady=4)
        tk.Button(toolbar, text="+", width=3, command=self.increase_width).pack(side="left", padx=(0, 6), pady=4)
        tk.Button(toolbar, text="撤销", command=self.undo).pack(side="left", padx=4, pady=4)
        tk.Button(toolbar, text="复制", command=self.copy_again).pack(side="left", padx=4, pady=4)
        tk.Button(toolbar, text="另存为", command=self.save_as).pack(side="left", padx=4, pady=4)
        tk.Button(toolbar, text="关闭", command=self.close_editor).pack(side="right", padx=4, pady=4)

        self.bind("<Control-z>", lambda _event: self.undo())
        self.protocol("WM_DELETE_WINDOW", self.close_editor)
        if origin is not None:
            self.position_near_capture(origin)

    def position_near_capture(self, origin: tuple[int, int]) -> None:
        self.update_idletasks()
        screen = get_virtual_screen()
        win_w = max(self.winfo_reqwidth(), self.winfo_width())
        win_h = max(self.winfo_reqheight(), self.winfo_height())
        max_x = max(screen.left, screen.left + screen.width - win_w)
        max_y = max(screen.top, screen.top + screen.height - win_h)
        x = min(max(origin[0], screen.left), max_x)
        y = min(max(origin[1], screen.top), max_y)
        x = max(0, int(x))
        y = max(0, int(y))
        self.geometry(f"+{x}+{y}")

    def canvas_to_image(self, x: float, y: float) -> tuple[float, float]:
        return x / self.scale, y / self.scale

    def image_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return x * self.scale, y * self.scale

    def scaled_width(self, width: int | None = None) -> int:
        return max(1, round((width or self.stroke_width.get()) * self.scale))

    def schedule_clipboard_update(self) -> None:
        if self.clipboard_after_id is not None:
            self.after_cancel(self.clipboard_after_id)
        self.clipboard_after_id = self.after(180, self.copy_current_to_clipboard_silent)

    def copy_current_to_clipboard_silent(self) -> None:
        self.clipboard_after_id = None
        try:
            copy_image_to_clipboard(self.render_image())
        except OSError:
            pass

    def on_tool_changed(self) -> None:
        self.canvas.configure(cursor="fleur" if self.tool.get() == "move" else "crosshair")
        self.active_data = None
        self.active_canvas_item = None
        self.moving_index = None
        self.move_last = None

    def on_press(self, event: tk.Event) -> None:
        tool = self.tool.get()
        color = self.color.get()
        width = self.stroke_width.get()
        x, y = self.canvas_to_image(event.x, event.y)
        if tool == "move":
            self.selected_index = self.find_item_at(x, y)
            self.moving_index = self.selected_index
            self.move_last = (x, y) if self.moving_index is not None else None
            self.redraw_canvas()
            return "break"
        if tool == "text":
            text = self.text_value.get().strip() or "文字"
            item = {"type": "text", "x": x, "y": y, "text": text, "color": color, "width": width}
            self.items.append(item)
            self.selected_index = len(self.items) - 1
            self.draw_item(item)
            self.draw_selection()
            self.schedule_clipboard_update()
            return "break"
        self.selected_index = None
        self.active_data = {"type": tool, "x0": x, "y0": y, "x1": x, "y1": y, "color": color, "width": width}
        if tool in {"pen", "mosaic"}:
            self.pen_points = [(x, y)]
            self.active_data["points"] = self.pen_points
            if tool == "mosaic":
                brush = self.scaled_width(max(8, width * 4))
                self.active_canvas_item = self.canvas.create_line(
                    event.x,
                    event.y,
                    event.x,
                    event.y,
                    fill="#ffffff",
                    width=brush,
                    capstyle="round",
                    smooth=True,
                    stipple="gray50",
                )
            else:
                self.active_canvas_item = self.canvas.create_line(
                    event.x,
                    event.y,
                    event.x,
                    event.y,
                    fill=color,
                    width=self.scaled_width(width),
                    capstyle="round",
                    smooth=True,
                )
        else:
            self.active_canvas_item = self.draw_item(self.active_data, temporary=True)
        return "break"

    def on_drag(self, event: tk.Event) -> None:
        if self.tool.get() == "move":
            if self.moving_index is None or self.move_last is None:
                return "break"
            x, y = self.canvas_to_image(event.x, event.y)
            last_x, last_y = self.move_last
            self.move_item(self.items[self.moving_index], x - last_x, y - last_y)
            self.move_last = (x, y)
            self.redraw_canvas()
            return "break"
        if not self.active_data or not self.active_canvas_item:
            return "break"
        tool = self.active_data["type"]
        x, y = self.canvas_to_image(event.x, event.y)
        self.active_data["x1"] = x
        self.active_data["y1"] = y
        if tool in {"pen", "mosaic"}:
            self.pen_points.append((x, y))
            coords: list[float] = []
            for px, py in self.pen_points:
                cx, cy = self.image_to_canvas(px, py)
                coords.extend([cx, cy])
            self.canvas.coords(self.active_canvas_item, *coords)
        else:
            x0, y0 = self.image_to_canvas(self.active_data["x0"], self.active_data["y0"])
            x1, y1 = self.image_to_canvas(x, y)
            self.canvas.coords(self.active_canvas_item, x0, y0, x1, y1)
        return "break"

    def on_release(self, _event: tk.Event) -> None:
        if self.tool.get() == "move":
            self.moving_index = None
            self.move_last = None
            self.schedule_clipboard_update()
            return "break"
        if self.active_data:
            self.items.append(self.active_data)
            self.selected_index = len(self.items) - 1
        self.active_data = None
        self.active_canvas_item = None
        self.pen_points = []
        self.redraw_canvas()
        self.schedule_clipboard_update()
        return "break"

    def draw_item(self, item: dict, temporary: bool = False) -> int:
        item_type = item["type"]
        color = item.get("color", "#ff2d2d")
        width = self.scaled_width(item.get("width"))
        if item_type == "text":
            x, y = self.image_to_canvas(item["x"], item["y"])
            return self.canvas.create_text(
                x,
                y,
                text=item["text"],
                fill=color,
                anchor="nw",
                font=("Segoe UI", max(10, round(22 * self.scale)), "bold"),
            )
        x0, y0 = self.image_to_canvas(item["x0"], item["y0"])
        x1, y1 = self.image_to_canvas(item["x1"], item["y1"])
        if item_type == "rect":
            return self.canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=width)
        if item_type == "ellipse":
            return self.canvas.create_oval(x0, y0, x1, y1, outline=color, width=width)
        if item_type == "arrow":
            return self.canvas.create_line(x0, y0, x1, y1, fill=color, width=width, arrow="last")
        if item_type == "mosaic":
            if not temporary:
                self.draw_canvas_mosaic(item)
                return self.draw_mosaic_outline(item)
            return 0
        if item_type == "pen":
            coords: list[float] = []
            for px, py in item.get("points", []):
                cx, cy = self.image_to_canvas(px, py)
                coords.extend([cx, cy])
            if len(coords) < 4:
                coords = [x0, y0, x1 + 1, y1 + 1]
            return self.canvas.create_line(*coords, fill=color, width=width, capstyle="round", smooth=True)
        raise ValueError(f"Unknown item type: {item_type}")

    def undo(self) -> None:
        if not self.items:
            return
        self.items.pop()
        self.redraw_canvas()
        self.schedule_clipboard_update()

    def redraw_canvas(self) -> None:
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        for item in self.items:
            self.draw_item(item)
        self.draw_selection()

    def draw_selection(self) -> None:
        if self.selected_index is None or self.selected_index >= len(self.items):
            return
        bbox = self.item_bbox(self.items[self.selected_index])
        if bbox is None:
            return
        x0, y0 = self.image_to_canvas(bbox[0], bbox[1])
        x1, y1 = self.image_to_canvas(bbox[2], bbox[3])
        self.canvas.create_rectangle(
            x0,
            y0,
            x1,
            y1,
            outline="#38bdf8",
            dash=(4, 3),
            width=1,
        )

    def find_item_at(self, x: float, y: float) -> int | None:
        for index in range(len(self.items) - 1, -1, -1):
            if self.hit_item(self.items[index], x, y):
                return index
        return None

    def hit_item(self, item: dict, x: float, y: float) -> bool:
        width = max(6.0, float(item.get("width", 3)) + 5.0)
        item_type = item["type"]
        if item_type in {"rect", "ellipse"}:
            bbox = self.item_bbox(item)
            if bbox is None:
                return False
            x0, y0, x1, y1 = bbox
            return x0 - width <= x <= x1 + width and y0 - width <= y <= y1 + width
        if item_type == "arrow":
            return self.distance_to_segment(x, y, item["x0"], item["y0"], item["x1"], item["y1"]) <= width
        if item_type == "pen":
            points = item.get("points", [])
            if len(points) == 1:
                px, py = points[0]
                return abs(px - x) <= width and abs(py - y) <= width
            return any(
                self.distance_to_segment(x, y, x0, y0, x1, y1) <= width
                for (x0, y0), (x1, y1) in zip(points, points[1:])
            )
        if item_type == "mosaic":
            brush = max(8.0, float(item.get("width", 3)) * 4.0)
            points = item.get("points", [])
            if len(points) == 1:
                px, py = points[0]
                return self.distance_to_segment(x, y, px, py, px, py) <= brush
            return any(
                self.distance_to_segment(x, y, x0, y0, x1, y1) <= brush
                for (x0, y0), (x1, y1) in zip(points, points[1:])
            )
        if item_type == "text":
            bbox = self.item_bbox(item)
            if bbox is None:
                return False
            x0, y0, x1, y1 = bbox
            return x0 - width <= x <= x1 + width and y0 - width <= y <= y1 + width
        return False

    def item_bbox(self, item: dict) -> tuple[float, float, float, float] | None:
        item_type = item["type"]
        pad = max(4.0, float(item.get("width", 3)) + 3.0)
        if item_type in {"rect", "ellipse", "arrow"}:
            x0, x1 = sorted((float(item["x0"]), float(item["x1"])))
            y0, y1 = sorted((float(item["y0"]), float(item["y1"])))
            return x0 - pad, y0 - pad, x1 + pad, y1 + pad
        if item_type in {"pen", "mosaic"}:
            points = item.get("points", [])
            if not points:
                return None
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            extra = max(pad, float(item.get("width", 3)) * 4.0 if item_type == "mosaic" else pad)
            return min(xs) - extra, min(ys) - extra, max(xs) + extra, max(ys) + extra
        if item_type == "text":
            text = item.get("text", "")
            width = max(24.0, len(text) * max(12.0, float(item.get("width", 3)) * 7.0))
            height = max(18.0, float(item.get("width", 3)) * 9.0)
            x0 = float(item["x"])
            y0 = float(item["y"])
            return x0 - pad, y0 - pad, x0 + width + pad, y0 + height + pad
        return None

    def distance_to_segment(self, px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> float:
        import math

        dx = x1 - x0
        dy = y1 - y0
        if dx == 0 and dy == 0:
            return math.hypot(px - x0, py - y0)
        t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)))
        nearest_x = x0 + t * dx
        nearest_y = y0 + t * dy
        return math.hypot(px - nearest_x, py - nearest_y)

    def move_item(self, item: dict, dx: float, dy: float) -> None:
        if item["type"] == "text":
            item["x"] += dx
            item["y"] += dy
            return
        if item["type"] in {"pen", "mosaic"}:
            item["points"] = [(x + dx, y + dy) for x, y in item.get("points", [])]
            return
        item["x0"] += dx
        item["y0"] += dy
        item["x1"] += dx
        item["y1"] += dy

    def mosaic_brush_width(self, item: dict) -> int:
        return max(8, int(item.get("width", 3)) * 4)

    def mosaic_mask(self, item: dict, size: tuple[int, int]) -> Image.Image:
        mask = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mask)
        points = item.get("points", [])
        brush = self.mosaic_brush_width(item)
        if len(points) == 1:
            x, y = points[0]
            radius = brush / 2
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
        elif len(points) > 1:
            draw.line(points, fill=255, width=brush, joint="curve")
            radius = brush / 2
            for x, y in points:
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
        return mask

    def apply_mosaic_stroke(self, image: Image.Image, item: dict) -> None:
        mask = self.mosaic_mask(item, image.size)
        bbox = mask.getbbox()
        if bbox is None:
            return
        x0, y0, x1, y1 = bbox
        region = image.crop(bbox)
        block = max(4, self.mosaic_brush_width(item) // 2)
        small_w = max(1, region.width // block)
        small_h = max(1, region.height // block)
        mosaic = region.resize((small_w, small_h), Image.Resampling.BILINEAR).resize(region.size, Image.Resampling.NEAREST)
        image.paste(mosaic, (x0, y0), mask.crop(bbox))

    def draw_canvas_mosaic(self, item: dict) -> None:
        preview = self.render_image(up_to_item=item)
        self.apply_mosaic_stroke(preview, item)
        display = preview.resize(
            (
                max(1, round(preview.width * self.scale)),
                max(1, round(preview.height * self.scale)),
            ),
            Image.Resampling.LANCZOS,
        )
        photo = ImageTk.PhotoImage(display)
        item["_preview_photo"] = photo
        self.canvas.create_image(0, 0, image=photo, anchor="nw")

    def draw_mosaic_outline(self, item: dict) -> int:
        points = item.get("points", [])
        if not points:
            return 0
        coords: list[float] = []
        for px, py in points:
            cx, cy = self.image_to_canvas(px, py)
            coords.extend([cx, cy])
        if len(coords) < 4:
            x, y = coords
            brush = self.scaled_width(self.mosaic_brush_width(item))
            return self.canvas.create_oval(
                x - brush / 2,
                y - brush / 2,
                x + brush / 2,
                y + brush / 2,
                outline="#ffffff",
                dash=(3, 3),
                width=1,
            )
        return self.canvas.create_line(
            *coords,
            fill="#ffffff",
            width=self.scaled_width(self.mosaic_brush_width(item)),
            capstyle="round",
            smooth=True,
            stipple="gray50",
        )

    def increase_width(self) -> None:
        self.stroke_width.set(min(12, self.stroke_width.get() + 1))

    def decrease_width(self) -> None:
        self.stroke_width.set(max(1, self.stroke_width.get() - 1))

    def render_image(self, up_to_item: dict | None = None) -> Image.Image:
        rendered = self.image.copy().convert("RGB")
        draw = ImageDraw.Draw(rendered)
        for item in self.items:
            if item is up_to_item:
                break
            color = item.get("color", "#ff2d2d")
            width = int(item.get("width", 3))
            if item["type"] == "rect":
                draw.rectangle((item["x0"], item["y0"], item["x1"], item["y1"]), outline=color, width=width)
            elif item["type"] == "ellipse":
                draw.ellipse((item["x0"], item["y0"], item["x1"], item["y1"]), outline=color, width=width)
            elif item["type"] == "arrow":
                draw.line((item["x0"], item["y0"], item["x1"], item["y1"]), fill=color, width=width)
                self.draw_arrow_head(draw, item, color, width)
            elif item["type"] == "mosaic":
                self.apply_mosaic_stroke(rendered, item)
                draw = ImageDraw.Draw(rendered)
            elif item["type"] == "pen":
                points = item.get("points", [])
                if len(points) > 1:
                    draw.line(points, fill=color, width=width, joint="curve")
            elif item["type"] == "text":
                font = self.get_font(max(12, width * 8))
                draw.text((item["x"], item["y"]), item["text"], fill=color, font=font)
        return rendered

    def draw_arrow_head(self, draw: ImageDraw.ImageDraw, item: dict, color: str, width: int) -> None:
        import math

        x0, y0, x1, y1 = item["x0"], item["y0"], item["x1"], item["y1"]
        angle = math.atan2(y1 - y0, x1 - x0)
        length = max(12, width * 5)
        spread = math.pi / 7
        points = [(x1, y1)]
        for sign in (1, -1):
            px = x1 - length * math.cos(angle - sign * spread)
            py = y1 - length * math.sin(angle - sign * spread)
            points.append((px, py))
        draw.polygon(points, fill=color)

    def get_font(self, size: int) -> ImageFont.ImageFont:
        for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    def copy_again(self) -> None:
        try:
            copy_image_to_clipboard(self.render_image())
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"复制失败：{exc}")

    def close_editor(self) -> None:
        if self.clipboard_after_id is not None:
            self.after_cancel(self.clipboard_after_id)
            self.clipboard_after_id = None
        self.copy_current_to_clipboard_silent()
        self.destroy()

    def save_as(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".png",
            initialfile=self.path.name,
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
        )
        if target:
            self.render_image().save(target)


class PolySnipperApp:
    def __init__(self) -> None:
        self.events: queue.Queue[str] = queue.Queue()
        self.start_hidden = "--startup" in sys.argv or "--hidden" in sys.argv
        self.tray_icon = None
        self.tray_thread: threading.Thread | None = None
        self.update_in_progress = False
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("360x176")
        self.root.resizable(False, False)

        self.listener = HotkeyListener(self.events)
        self.build_control_window()
        self.root.protocol("WM_DELETE_WINDOW", self.hide if pystray else self.quit)
        self.root.bind("<Unmap>", self.on_root_unmap)
        self.listener.start()
        self.start_tray()
        if self.start_hidden:
            self.root.after(250, self.root.withdraw)
        self.root.after(100, self.poll_events)

    def build_control_window(self) -> None:
        self.root.configure(bg="#f4f4f4")
        self.status_text = tk.StringVar(value="就绪")
        tk.Label(
            self.root,
            text=f"{APP_NAME} {APP_VERSION}",
            font=("Segoe UI", 15, "bold"),
            bg="#f4f4f4",
        ).pack(pady=(14, 4))
        tk.Label(
            self.root,
            text="Alt + A 截图。截图后可标注、复制、保存，并保持窗口置顶。",
            font=("Segoe UI", 9),
            bg="#f4f4f4",
            wraplength=280,
        ).pack(pady=(0, 10))
        row = tk.Frame(self.root, bg="#f4f4f4")
        row.pack()
        self.capture_button = self.make_control_button(row, "截图", self.start_capture)
        self.folder_button = self.make_control_button(row, "目录", self.open_folder)
        self.update_button = self.make_control_button(row, "↻ 更新", lambda: self.check_for_updates(manual=True), accent=True)
        self.quit_button = self.make_control_button(row, "退出", self.quit)
        tk.Label(
            self.root,
            text=f"保存目录：{screenshots_dir()}",
            font=("Segoe UI", 8),
            bg="#f4f4f4",
            fg="#555555",
            wraplength=290,
        ).pack(pady=(12, 0))
        tk.Label(
            self.root,
            textvariable=self.status_text,
            font=("Segoe UI", 8),
            bg="#f4f4f4",
            fg="#0f766e",
            wraplength=320,
        ).pack(pady=(4, 0))

    def make_control_button(
        self,
        parent: tk.Widget,
        text: str,
        command: Callable[[], None],
        accent: bool = False,
    ) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            width=7,
            command=command,
            relief="flat",
            bd=0,
            bg="#0f766e" if accent else "#ffffff",
            fg="#ffffff" if accent else "#111827",
            activebackground="#115e59" if accent else "#e5e7eb",
            activeforeground="#ffffff" if accent else "#111827",
            font=("Segoe UI", 9, "bold" if accent else "normal"),
            cursor="hand2",
            highlightthickness=1,
            highlightbackground="#cbd5e1",
            padx=6,
            pady=5,
        )
        button.pack(side="left", padx=4)
        return button

    def poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "capture":
                self.start_capture()
            elif event == "hotkey_failed":
                messagebox.showwarning(APP_NAME, "Alt + A 快捷键已被占用。")
        self.root.after(100, self.poll_events)

    def start_tray(self) -> None:
        if pystray is None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("截图", lambda _icon, _item: self.root.after(0, self.start_capture)),
            pystray.MenuItem("显示", lambda _icon, _item: self.root.after(0, self.show)),
            pystray.MenuItem("打开目录", lambda _icon, _item: self.root.after(0, self.open_folder)),
            pystray.MenuItem("检查更新", lambda _icon, _item: self.root.after(0, lambda: self.check_for_updates(manual=True))),
            pystray.MenuItem("退出", lambda _icon, _item: self.root.after(0, self.quit)),
        )
        self.tray_icon = pystray.Icon(APP_NAME, self.make_tray_image(), APP_NAME, menu)
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()

    def make_tray_image(self) -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 10, 56, 54), radius=8, fill="#1f2937", outline="#35d4ff", width=3)
        draw.line((18, 23, 46, 23), fill="#ffffff", width=4)
        draw.line((18, 35, 38, 35), fill="#ffffff", width=4)
        draw.rectangle((43, 34, 49, 40), fill="#35d4ff")
        return image

    def hide(self) -> None:
        self.root.withdraw()

    def on_root_unmap(self, event: tk.Event) -> None:
        if event.widget is self.root and pystray and self.root.state() == "iconic":
            self.root.after_idle(self.root.withdraw)

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def start_capture(self) -> None:
        self.root.withdraw()
        self.root.after(120, self._open_overlay)

    def _open_overlay(self) -> None:
        screen = get_virtual_screen()
        image = grab_virtual_screen(screen)
        CaptureOverlay(self, screen, image)

    def finish_capture(self, image: Image.Image, origin: tuple[int, int] | None = None) -> None:
        path = save_capture(image)
        try:
            copy_image_to_clipboard(image)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"复制失败：{exc}")
        EditorWindow(self, image, path, origin)

    def open_folder(self) -> None:
        import os

        os.startfile(screenshots_dir())

    def check_for_updates(self, manual: bool = False) -> None:
        if self.update_in_progress:
            if manual:
                self.status_text.set("正在检查，请稍等...")
            return
        self.update_in_progress = True
        self.status_text.set("正在检查最新版本...")
        self.update_button.configure(state="disabled", text="检查中")
        if manual:
            self.show()
        threading.Thread(target=self._check_for_updates_worker, args=(manual,), daemon=True).start()

    def _check_for_updates_worker(self, manual: bool) -> None:
        try:
            req = urllib.request.Request(RELEASES_API, headers={"User-Agent": f"PolySnipper/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=20) as response:
                release = json.loads(response.read().decode("utf-8"))
            latest_tag = release.get("tag_name", "")
            if not latest_tag:
                raise RuntimeError("GitHub 没有返回最新版本号。")
            if not is_newer_version(latest_tag, APP_VERSION):
                if manual:
                    self.root.after(0, lambda: messagebox.showinfo(APP_NAME, f"已是最新版本。当前版本：{APP_VERSION}", parent=self.root))
                self.root.after(0, lambda: self.status_text.set(f"已是最新版本：{APP_VERSION}"))
                return
            installer_url = self.find_installer_asset(release) or LATEST_INSTALLER_URL
            self.root.after(0, lambda: self.prompt_update(latest_tag, installer_url))
        except Exception as exc:
            self.root.after(0, lambda: self.status_text.set("检查更新失败"))
            if manual:
                self.root.after(0, lambda: messagebox.showerror(APP_NAME, f"检查更新失败：\n{exc}", parent=self.root))
        finally:
            self.root.after(0, self.finish_update_check)

    def finish_update_check(self) -> None:
        self.update_in_progress = False
        self.update_button.configure(state="normal", text="↻ 更新")

    def find_installer_asset(self, release: dict) -> str | None:
        for asset in release.get("assets", []):
            if asset.get("name") == "PolySnipperSetup.exe":
                return asset.get("browser_download_url")
        return None

    def prompt_update(self, latest_tag: str, installer_url: str) -> None:
        self.show()
        self.status_text.set(f"发现新版本：{latest_tag.lstrip('vV')}")
        ok = messagebox.askyesno(
            APP_NAME,
            f"发现新版本 {latest_tag.lstrip('vV')}。\n\n现在下载并安装吗？",
            parent=self.root,
        )
        if ok:
            threading.Thread(target=self._download_and_install_update, args=(latest_tag, installer_url), daemon=True).start()

    def _download_and_install_update(self, latest_tag: str, installer_url: str) -> None:
        try:
            self.root.after(0, lambda: self.status_text.set("正在下载更新..."))
            target = Path(tempfile.gettempdir()) / f"PolySnipperSetup-{latest_tag.lstrip('vV')}.exe"
            req = urllib.request.Request(installer_url, headers={"User-Agent": f"PolySnipper/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=120) as response:
                data = response.read()
            target.write_bytes(data)
            self.root.after(0, lambda: self.launch_update_installer(target))
        except Exception as exc:
            self.root.after(0, lambda: messagebox.showerror(APP_NAME, f"更新下载失败：\n{exc}", parent=self.root))

    def launch_update_installer(self, installer_path: Path) -> None:
        messagebox.showinfo(APP_NAME, "即将启动安装器。安装时 Poly Snipper 会自动关闭。", parent=self.root)
        subprocess.Popen([str(installer_path)])
        self.quit()

    def quit(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self.listener.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    if sys.platform != "win32":
        print(f"{APP_NAME} 当前仅支持 Windows。", file=sys.stderr)
        return 1
    PolySnipperApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
