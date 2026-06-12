#!/usr/bin/env python3
"""A small Windows screenshot tool inspired by Snipaste.

Hotkey: Alt+A
Features: region capture, copy image to clipboard, auto-save PNG, pin image.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import queue
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageDraw, ImageGrab, ImageTk

try:
    import pystray
except ImportError:  # The script still works without tray support during development.
    pystray = None


APP_NAME = "Poly Snipper"
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
            text="Drag to select, Esc to cancel",
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
        self.destroy()
        self.app.finish_capture(crop)


class PinWindow(tk.Toplevel):
    def __init__(self, app: "PolySnipperApp", image: Image.Image, path: Path) -> None:
        super().__init__(app.root)
        self.app = app
        self.image = image
        self.path = path
        self.drag_offset: tuple[int, int] | None = None

        self.title(f"{APP_NAME} - {path.name}")
        self.attributes("-topmost", True)
        self.configure(bg="#111111")

        max_w = int(self.winfo_screenwidth() * 0.65)
        max_h = int(self.winfo_screenheight() * 0.65)
        display = image.copy()
        display.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(display)

        self.image_label = tk.Label(self, image=self.photo, bd=0, bg="#111111")
        self.image_label.pack(side="top")

        toolbar = tk.Frame(self, bg="#202020")
        toolbar.pack(side="bottom", fill="x")
        tk.Button(toolbar, text="Copy", command=self.copy_again).pack(side="left", padx=4, pady=4)
        tk.Button(toolbar, text="Save As", command=self.save_as).pack(side="left", padx=4, pady=4)
        tk.Button(toolbar, text="Close", command=self.destroy).pack(side="right", padx=4, pady=4)

        self.bind_drag(self)
        self.bind_drag(self.image_label)

    def bind_drag(self, widget: tk.Widget) -> None:
        widget.bind("<ButtonPress-1>", self.start_drag)
        widget.bind("<B1-Motion>", self.drag)

    def start_drag(self, event: tk.Event) -> None:
        self.drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def drag(self, event: tk.Event) -> None:
        if not self.drag_offset:
            return
        dx, dy = self.drag_offset
        self.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def copy_again(self) -> None:
        try:
            copy_image_to_clipboard(self.image)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Copy failed: {exc}")

    def save_as(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".png",
            initialfile=self.path.name,
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
        )
        if target:
            self.image.save(target)


class PolySnipperApp:
    def __init__(self) -> None:
        self.events: queue.Queue[str] = queue.Queue()
        self.start_hidden = "--startup" in sys.argv or "--hidden" in sys.argv
        self.tray_icon = None
        self.tray_thread: threading.Thread | None = None
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("320x170")
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
        tk.Label(
            self.root,
            text=APP_NAME,
            font=("Segoe UI", 15, "bold"),
            bg="#f4f4f4",
        ).pack(pady=(14, 4))
        tk.Label(
            self.root,
            text="Alt + A to capture. Selection is copied, saved, and pinned.",
            font=("Segoe UI", 9),
            bg="#f4f4f4",
            wraplength=280,
        ).pack(pady=(0, 10))
        row = tk.Frame(self.root, bg="#f4f4f4")
        row.pack()
        tk.Button(row, text="Capture", width=10, command=self.start_capture).pack(side="left", padx=5)
        tk.Button(row, text="Folder", width=10, command=self.open_folder).pack(side="left", padx=5)
        tk.Button(row, text="Quit", width=10, command=self.quit).pack(side="left", padx=5)
        tk.Label(
            self.root,
            text=f"Saved to {screenshots_dir()}",
            font=("Segoe UI", 8),
            bg="#f4f4f4",
            fg="#555555",
            wraplength=290,
        ).pack(pady=(14, 0))

    def poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "capture":
                self.start_capture()
            elif event == "hotkey_failed":
                messagebox.showwarning(APP_NAME, "Alt+A hotkey is already in use.")
        self.root.after(100, self.poll_events)

    def start_tray(self) -> None:
        if pystray is None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Capture", lambda _icon, _item: self.root.after(0, self.start_capture)),
            pystray.MenuItem("Show", lambda _icon, _item: self.root.after(0, self.show)),
            pystray.MenuItem("Open Folder", lambda _icon, _item: self.root.after(0, self.open_folder)),
            pystray.MenuItem("Quit", lambda _icon, _item: self.root.after(0, self.quit)),
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

    def finish_capture(self, image: Image.Image) -> None:
        path = save_capture(image)
        try:
            copy_image_to_clipboard(image)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Copy failed: {exc}")
        PinWindow(self, image, path)

    def open_folder(self) -> None:
        import os

        os.startfile(screenshots_dir())

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
        print(f"{APP_NAME} currently supports Windows only.", file=sys.stderr)
        return 1
    PolySnipperApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
