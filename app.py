import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import uuid
import ctypes
import threading
import urllib.parse
import urllib.request
import zipfile
import subprocess
import tempfile
from collections import deque
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from types import SimpleNamespace

# PyInstaller puts Qt in a private directory.  Register it before importing
# PySide so Windows can resolve QtCore.pyd and Qt6Core.dll dependencies.
if getattr(sys, "frozen", False):
    _bundle = Path(sys._MEIPASS)
    os.add_dll_directory(str(_bundle))
    os.add_dll_directory(str(_bundle / "PySide6"))
    os.add_dll_directory(str(_bundle / "shiboken6"))

import keyboard
import numpy as np
import sounddevice as sd
import av
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QUrl, Qt, Signal, QObject, QTimer
from PySide6.QtGui import (
    QAction, QBrush, QColor, QFont, QFontMetrics, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QPolygonF, QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QInputDialog, QMessageBox, QPushButton, QSlider, QSpinBox, QDoubleSpinBox, QStackedWidget, QVBoxLayout,
    QWidget, QScrollArea, QMenu, QStyle, QStyledItemDelegate, QSizePolicy,
)

APP_NAME = "EchoDeck"
MIC_OFF_LABEL = "（不使用麥克風）"
BUILD_TAG = "v2.4"
FORMATS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus", ".webm"}
CONFIG_PATH = Path(os.getenv("APPDATA", str(Path.home()))) / APP_NAME / "settings.json"


# Games commonly read the physical keyboard scan-code stream rather than the
# virtual-key stream used by high-level automation libraries.  SendInput with
# KEYEVENTF_SCANCODE is therefore used for normal keyboard PTT keys (such as
# K/V/F8). Mouse buttons retain the keyboard-library fallback below.
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


# The union must contain the largest member (MOUSEINPUT) or sizeof(INPUT) is
# 32 instead of 40 on 64-bit Windows and SendInput rejects every call.
class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUT_UNION)]


def send_scan_code(key: str, is_key_up: bool) -> bool:
    """Send a hardware-style keyboard event; returns False for unsupported names."""
    key = key.strip().lower()
    # A single printable character is the reliable path for game PTT keys.
    if len(key) != 1:
        return False
    vk = ctypes.windll.user32.VkKeyScanW(ord(key)) & 0xFF
    scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    if not scan:
        return False
    flags = 0x0008 | (0x0002 if is_key_up else 0)  # SCANCODE | KEYUP
    event = _INPUT(1, _INPUT_UNION(_KEYBDINPUT(0, scan, flags, 0, None)))
    return bool(ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)))


_MOUSE_FLAGS = {  # name: (down, up, mouseData)
    "left": (0x0002, 0x0004, 0), "right": (0x0008, 0x0010, 0),
    "middle": (0x0020, 0x0040, 0), "x1": (0x0080, 0x0100, 1), "x2": (0x0080, 0x0100, 2),
}


def send_mouse_click(button: str, hold: float = 0.03) -> None:
    """Press, hold for `hold` seconds, release. Games poll input once per frame,
    so a zero-length press (down+up in the same instant) is usually ignored."""
    down, up, data = _MOUSE_FLAGS.get(button, _MOUSE_FLAGS["left"])
    for flag, wait in ((down, hold), (up, 0)):
        event = _INPUT(0, _INPUT_UNION(mi=_MOUSEINPUT(0, 0, data, flag, 0, None)))
        ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))
        if wait:
            time.sleep(wait)


_MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, left/right Win


def wait_for_modifiers_released(timeout: float = 0.8) -> bool:
    """Wait (briefly) until the user has physically let go of Shift/Ctrl/Alt/Win.

    A global hotkey such as Alt+1 fires while Alt is still held. If the PTT key
    were injected right then, the game would see Alt+K instead of K and ignore
    it. Returns True when no modifier is held any more."""
    try:
        down = lambda: any(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS)
        deadline = time.perf_counter() + timeout
        while down() and time.perf_counter() < deadline:
            time.sleep(0.01)
        return not down()
    except Exception:
        return True


def hotkey_log(message: str) -> None:
    """Tiny diagnostic log: %APPDATA%\\EchoDeck\\hotkey.log (kept under ~100 KB)."""
    try:
        path = CONFIG_PATH.parent / "hotkey.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 100_000:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + message + "\n")
    except Exception:
        pass


def vk_is_down(vk: int) -> bool:
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def modifiers_held() -> bool:
    return any(vk_is_down(vk) for vk in _MODIFIER_VKS)


_NAMED_VKS = {
    "ctrl": (0x11,), "left ctrl": (0xA2,), "right ctrl": (0xA3,),
    "alt": (0x12,), "left alt": (0xA4,), "right alt": (0xA5,), "alt gr": (0xA5,),
    "shift": (0x10,), "left shift": (0xA0,), "right shift": (0xA1,),
    "windows": (0x5B, 0x5C), "left windows": (0x5B,), "right windows": (0x5C,),
    "space": (0x20,), "enter": (0x0D,), "tab": (0x09,), "esc": (0x1B,), "backspace": (0x08,),
    "delete": (0x2E,), "insert": (0x2D,), "home": (0x24,), "end": (0x23,),
    "page up": (0x21,), "page down": (0x22,), "up": (0x26,), "down": (0x28,),
    "left": (0x25,), "right": (0x27,), "caps lock": (0x14,), "num lock": (0x90,),
    "scroll lock": (0x91,), "print screen": (0x2C,), "pause": (0x13,),
    "+": (0xBB, 0x6B), "plus": (0xBB, 0x6B), "-": (0xBD, 0x6D), "minus": (0xBD, 0x6D),
}


def hotkey_vks(names):
    """['ctrl','alt','space'] -> [(0x11,), (0x12,), (0x20,)] for GetAsyncKeyState
    polling, or None if any key cannot be mapped (caller falls back to the
    keyboard library for that hotkey)."""
    result = []
    for name in names:
        name = name.strip().lower()
        if name in _NAMED_VKS:
            result.append(_NAMED_VKS[name])
        elif len(name) > 1 and name[0] == "f" and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
            result.append((0x6F + int(name[1:]),))
        elif len(name) == 1:
            try:
                vk = ctypes.windll.user32.VkKeyScanW(ord(name)) & 0xFF
            except Exception:
                return None
            if vk in (0, 0xFF):
                return None
            result.append((vk,))
        else:
            return None
    return result or None


def key_is_down(name: str):
    """Does Windows currently see this single-character key as held?
    Returns None when it cannot be told (multi-character names, API error)."""
    name = (name or "").strip().lower()
    if len(name) != 1:
        return None
    try:
        vk = ctypes.windll.user32.VkKeyScanW(ord(name)) & 0xFF
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return None


def send_mouse_button(button: str, is_down: bool) -> None:
    """Press or release a mouse button without the paired half, so it can be
    held down across an arbitrary duration (used for the PTT key and the
    hold-key feature, both of which press now and release later)."""
    down, up, data = _MOUSE_FLAGS.get(button, _MOUSE_FLAGS["left"])
    event = _INPUT(0, _INPUT_UNION(mi=_MOUSEINPUT(0, 0, data, down if is_down else up, 0, None)))
    ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))


def send_key_state(name: str, is_down: bool) -> bool:
    """Hold or release a keyboard key, preferring the scan-code path (see
    send_scan_code). Returns True if the scan-code path handled it."""
    # send_scan_code's 2nd argument means "key UP", the opposite of is_down.
    if send_scan_code(name, not is_down):
        return True
    if is_down:
        keyboard.press(name)
    else:
        keyboard.release(name)
    return False


def target_kind_name(value, default="key:v"):
    """Split a 'key:x' / 'mouse:x' stored target into (kind, name), falling
    back to treating a legacy plain key name (no prefix) as a keyboard key."""
    value = value or default
    kind, sep, name = value.partition(":")
    if not sep:
        return "key", value
    return kind, name


def send_key_tap(name: str, hold: float = 0.03) -> None:
    if send_scan_code(name, False):
        time.sleep(hold)
        send_scan_code(name, True)
    else:
        keyboard.press(name)
        time.sleep(hold)
        keyboard.release(name)


MODIFIERS = ["ctrl", "alt", "shift", "windows"]
KEY_DISPLAY = {"windows": "Win", "+": "+"}
MOUSE_BUTTONS = [
    (Qt.MouseButton.LeftButton, "left", "滑鼠左鍵"),
    (Qt.MouseButton.RightButton, "right", "滑鼠右鍵"),
    (Qt.MouseButton.MiddleButton, "middle", "滑鼠中鍵"),
    (Qt.MouseButton.BackButton, "x1", "滑鼠側鍵 1（後退）"),
    (Qt.MouseButton.ForwardButton, "x2", "滑鼠側鍵 2（前進）"),
]


def hotkey_names(text):
    """'ctrl+alt+plus' -> ['ctrl', 'alt', '+'] (the + key is stored as 'plus')."""
    text = (text or "").strip().lower()
    if not text:
        return []
    if text == "+":
        return ["+"]
    text = text.replace("++", "+plus")
    return ["+" if p == "plus" else p for p in (s.strip() for s in text.split("+")) if p]


def hotkey_text(names):
    return "+".join("plus" if n == "+" else n for n in names)


def key_label(name):
    return KEY_DISPLAY.get(name, name.capitalize() if len(name) > 1 else name.upper())


def hotkey_display(text):
    return " + ".join(key_label(n) for n in hotkey_names(text))


def target_display(value):
    kind, _, name = (value or "").partition(":")
    if kind == "mouse":
        for _btn, key, label in MOUSE_BUTTONS:
            if key == name:
                return label
    if kind == "key" and name:
        return "鍵盤：" + key_label(name)
    return "尚未選擇"


def _listen_style():
    return T.get("LISTEN_QSS", "border-color: #ff9bb0; background: #2a2036;")


# ---- vector icons (drawn in code, so no image files need to be shipped) ----
def restyle(widget):
    """Re-apply the stylesheet after objectName / property changes."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def app_icon_pixmap(size=256):
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    bg = QLinearGradient(0, 0, s, s)
    bg.setColorAt(0.0, QColor("#9a8cff"))
    bg.setColorAt(1.0, QColor("#4a3fc2"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(bg))
    p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.23, s * 0.23)
    # five "echo" sound bars; the outer ones fade like an echo
    heights = [0.26, 0.48, 0.72, 0.48, 0.26]
    alphas = [150, 215, 255, 215, 150]
    bar_w, gap = s * 0.095, s * 0.058
    total = 5 * bar_w + 4 * gap
    x = (s - total) / 2
    for h, a in zip(heights, alphas):
        bar_h = s * h
        color = QColor(255, 255, 255, a)
        p.setBrush(QBrush(color))
        p.drawRoundedRect(QRectF(x, (s - bar_h) / 2, bar_w, bar_h), bar_w / 2, bar_w / 2)
        x += bar_w + gap
    p.end()
    return pix


def make_app_icon():
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_icon_pixmap(size))
    return icon


def _nav_pixmap(kind, color, size=44):
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 48.0, size / 48.0)
    c = QColor(color)
    pen = QPen(c, 3.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "music":
        p.setBrush(QBrush(c))
        p.drawEllipse(QPointF(15, 35), 6.5, 5.2)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(21, 34), QPointF(21, 11))
        path = QPainterPath(QPointF(21, 11))
        path.cubicTo(28, 13, 34, 16, 33, 25)
        p.drawPath(path)
    elif kind == "keys":
        p.drawRoundedRect(QRectF(5, 12, 38, 24), 5, 5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(c))
        for row_y in (18, 24.5):
            for key_x in (10, 17.5, 25, 32.5):
                p.drawRoundedRect(QRectF(key_x, row_y, 5, 3.4), 1, 1)
        p.drawRoundedRect(QRectF(15, 30, 18, 3), 1.2, 1.2)
    elif kind == "mouse":
        p.setBrush(QBrush(c))
        pen.setWidthF(2.2)
        p.setPen(pen)
        arrow = QPainterPath(QPointF(13, 8))
        for x, y in [(13, 35), (20, 29), (25, 40), (30, 38), (25, 27), (34, 27)]:
            arrow.lineTo(x, y)
        arrow.closeSubpath()
        p.drawPath(arrow)
    elif kind == "pulse":
        path = QPainterPath(QPointF(4, 26))
        for x, y in [(15, 26), (20, 12), (28, 38), (33, 26), (44, 26)]:
            path.lineTo(x, y)
        p.drawPath(path)
    elif kind == "radio":
        p.drawRoundedRect(QRectF(5, 18, 38, 23), 6, 6)
        p.drawLine(QPointF(30, 18), QPointF(39, 6))
        p.drawEllipse(QPointF(16.5, 29.5), 5.2, 5.2)
        p.drawLine(QPointF(27, 26), QPointF(37, 26))
        p.drawLine(QPointF(27, 33), QPointF(37, 33))
    elif kind == "play":
        p.drawRoundedRect(QRectF(5, 11, 38, 26), 9, 9)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(c))
        p.drawPolygon(QPolygonF([QPointF(20, 18), QPointF(20, 30), QPointF(30.5, 24)]))
    elif kind == "gear":
        import math
        p.drawEllipse(QPointF(24, 24), 11, 11)
        p.drawEllipse(QPointF(24, 24), 4.2, 4.2)
        for i in range(8):
            a = math.radians(i * 45)
            p.drawLine(QPointF(24 + 11 * math.cos(a), 24 + 11 * math.sin(a)),
                       QPointF(24 + 17 * math.cos(a), 24 + 17 * math.sin(a)))
    p.end()
    return pix


def nav_icon(kind):
    icon = QIcon()
    icon.addPixmap(_nav_pixmap(kind, T.get("NAV_OFF", "#8d92b0")), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(_nav_pixmap(kind, T.get("NAV_ON", "#ffffff")), QIcon.Mode.Normal, QIcon.State.On)
    return icon


class HotkeyEdit(QPushButton):
    """Click once -> 'press keys...'; press a combo -> shown live; release -> saved."""
    changed = Signal(str)
    capture_state = Signal(bool)
    _key_event = Signal(str, bool)

    def __init__(self, value=""):
        super().__init__()
        self.setObjectName("hotkeyEdit")
        self.value = value
        self.listening = False
        self._hook = None
        self._pressed = set()
        self._seq = []
        self._key_event.connect(self._on_key)
        self.clicked.connect(self._toggle)
        self._refresh()

    def set_value(self, value):
        self.value = value
        self._refresh()

    def clear_value(self):
        self._stop_listening()
        self.value = ""
        self._refresh()
        self.changed.emit("")
        self.capture_state.emit(False)

    def _refresh(self):
        if self.listening:
            live = " + ".join(key_label(n) for n in self._seq)
            self.setText(live + " …" if live else "請按下按鍵…（可組合，例如 Ctrl + Alt + P）")
            self.setStyleSheet(_listen_style())
        else:
            self.setText(hotkey_display(self.value) or "未設定（點一下設定）")
            self.setStyleSheet("")

    def _toggle(self):
        if self.listening:
            self._stop_listening(); self._refresh(); self.capture_state.emit(False)
            return
        self.listening = True
        self._pressed.clear(); self._seq.clear()
        self.capture_state.emit(True)
        self._hook = keyboard.hook(lambda e: self._key_event.emit(e.name or "", e.event_type == "down"))
        self._refresh()

    def _stop_listening(self):
        if self._hook is not None:
            try:
                keyboard.unhook(self._hook)
            except Exception:
                pass
            self._hook = None
        self.listening = False

    def _on_key(self, name, down):
        if not self.listening or not name:
            return
        if down:
            if name not in self._pressed:
                self._pressed.add(name)
                if name not in self._seq:
                    self._seq.append(name)
            self._refresh()
        else:
            self._pressed.discard(name)
            if not self._pressed and self._seq:
                order = sorted(self._seq, key=lambda n: (MODIFIERS.index(n) if n in MODIFIERS else len(MODIFIERS),
                                                         self._seq.index(n)))
                self._stop_listening()
                self.value = hotkey_text(order)
                self._refresh()
                self.changed.emit(self.value)
                self.capture_state.emit(False)

    # While listening, Space/Enter/Tab must be recorded, not "click" the button.
    def keyPressEvent(self, event):
        if self.listening:
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if self.listening:
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def focusNextPrevChild(self, nxt):
        return False if self.listening else super().focusNextPrevChild(nxt)

    def focusOutEvent(self, event):
        if self.listening:
            self._stop_listening(); self._refresh(); self.capture_state.emit(False)
        super().focusOutEvent(event)


class ClickTargetEdit(QPushButton):
    """Click once to arm; the next keyboard key OR mouse button pressed is chosen."""
    changed = Signal(str)
    capture_state = Signal(bool)
    _key_event = Signal(str)

    def __init__(self, value="mouse:left"):
        super().__init__()
        self.setObjectName("hotkeyEdit")
        self.value = value
        self.listening = False
        self._hook = None
        self._key_event.connect(self._on_key)
        self._refresh()

    def _refresh(self):
        if self.listening:
            self.setText("請按下要連點的「鍵盤按鍵」或「滑鼠按鍵」…")
            self.setStyleSheet(_listen_style())
        else:
            self.setText(target_display(self.value))
            self.setStyleSheet("")

    def _start(self):
        self.listening = True
        self.capture_state.emit(True)
        self._hook = keyboard.hook(lambda e: self._key_event.emit(e.name or "") if e.event_type == "down" else None)
        self.grabMouse()  # any mouse button pressed inside the EchoDeck window is captured
        self._refresh()

    def _stop(self):
        if self._hook is not None:
            try:
                keyboard.unhook(self._hook)
            except Exception:
                pass
            self._hook = None
        self.releaseMouse()
        self.listening = False

    def _finish(self, value):
        self._stop()
        self.value = value
        self._refresh()
        self.changed.emit(value)
        self.capture_state.emit(False)

    def _on_key(self, name):
        if self.listening and name:
            self._finish("key:" + name)

    def mousePressEvent(self, event):
        if not self.listening:
            self._start()
        else:
            for button, key, _label in MOUSE_BUTTONS:
                if event.button() == button:
                    self._finish("mouse:" + key)
                    break
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def keyPressEvent(self, event):
        event.accept() if self.listening else super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        event.accept() if self.listening else super().keyReleaseEvent(event)

    def focusNextPrevChild(self, nxt):
        return False if self.listening else super().focusNextPrevChild(nxt)

    def focusOutEvent(self, event):
        if self.listening:
            self._stop(); self._refresh(); self.capture_state.emit(False)
        super().focusOutEvent(event)


class MouseHotkeyEdit(ClickTargetEdit):
    """Click once to arm, then press a mouse MIDDLE or SIDE button inside the
    EchoDeck window. Left/right are refused: they would fire on every click."""
    ALLOWED = ("middle", "x1", "x2")

    def _refresh(self):
        if self.listening:
            self.setText("請在此視窗內按下滑鼠「中鍵」或「側鍵」…")
            self.setStyleSheet(_listen_style())
        else:
            self.setText(target_display(self.value) if self.value else "未設定（點一下設定）")
            self.setStyleSheet("")

    def _start(self):
        self.listening = True  # mouse only: no keyboard hook
        self.capture_state.emit(True)
        self.grabMouse()
        self._refresh()

    def _on_key(self, name):
        pass

    def clear_value(self):
        self._stop()
        self.value = ""
        self._refresh()
        self.changed.emit("")
        self.capture_state.emit(False)

    def mousePressEvent(self, event):
        if not self.listening:
            self._start()
        else:
            for button, key, _label in MOUSE_BUTTONS:
                if event.button() == button:
                    if key in self.ALLOWED:
                        self._finish("mouse:" + key)
                    else:
                        self.setText("左鍵／右鍵不能當快捷鍵，請改按中鍵或側鍵…")
                    break
        event.accept()


class AutoClicker:
    MIN_INTERVAL = 0.02  # seconds (max ~50 clicks/sec)

    def __init__(self):
        self.target = "mouse:left"
        self.interval = 0.1
        self._stop_event = None
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def start(self):
        if self.running:
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()

    def _loop(self, stop_event):
        try:  # 1 ms timer resolution; default Windows timer is ~15.6 ms
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        try:
            next_time = time.perf_counter()
            while not stop_event.is_set():
                interval = max(self.MIN_INTERVAL, float(self.interval))
                self._click_once(min(0.03, interval * 0.4))
                next_time += interval
                delay = next_time - time.perf_counter()
                if delay < 0:  # fell behind: do not burst-click to catch up
                    next_time, delay = time.perf_counter(), 0
                if stop_event.wait(delay):
                    break
        finally:
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass

    def _click_once(self, hold=0.03):
        kind, _, name = self.target.partition(":")
        try:
            if kind == "mouse":
                send_mouse_click(name, hold)
            elif kind == "key" and name:
                send_key_tap(name, hold)
        except Exception:
            pass


class HoldAction:
    """Holds a keyboard key or mouse button down (not repeated clicking) until
    stopped, optionally for a fixed duration. Same target format as AutoClicker."""

    def __init__(self):
        self.target = "key:v"
        self.active = False
        self._scan_code_down = False

    @property
    def running(self):
        return self.active

    def start(self):
        if self.active:
            return
        kind, name = target_kind_name(self.target)
        try:
            if kind == "mouse":
                send_mouse_button(name, True)
            elif name:
                self._scan_code_down = send_key_state(name, True)
        except Exception:
            pass
        self.active = True

    def stop(self):
        if not self.active:
            return
        kind, name = target_kind_name(self.target)
        try:
            if kind == "mouse":
                send_mouse_button(name, False)
            elif name:
                send_key_state(name, False)
        except Exception:
            pass
        self.active = False


HOTKEY_ROWS = [
    ("play_hotkey", "播放選取歌曲", "播放目前在音樂庫選好的歌曲"),
    ("random_hotkey", "隨機播放", "從資料夾隨機挑一首並開始區域發話"),
    ("next_hotkey", "下一首", "依資料夾順序播放下一首"),
    ("stop_hotkey", "停止全部音效", "立刻停止全部音效並放開遊戲 PTT"),
    ("pause_hotkey", "暫停／繼續", "暫停目前音效（放開 PTT）；再按一次從原位置繼續"),
    ("loop_hotkey", "單曲循環開／關", "開啟後，目前歌曲播完會自動從頭重播"),
    ("volume_up_hotkey", "音量加大", "每按一次 +5%（上限 200%），按住會慢速連續調整"),
    ("volume_down_hotkey", "音量減小", "每按一次 −5%，按住會慢速連續調整"),
    ("mic_hotkey", "麥克風開／關", "開啟／關閉麥克風混音"),
]


@dataclass
class Settings:
    music_folder: str = ""
    output_device: str = ""
    ptt_key: str = "v"
    auto_ptt: bool = True
    ptt_delay_ms: int = 180
    release_delay_ms: int = 100
    shuffle: bool = True
    continue_playback: bool = False
    volume: int = 85
    monitor_enabled: bool = True
    monitor_device: str = ""
    microphone_device: str = ""
    microphone_enabled: bool = False
    mute_mic_while_sound: bool = True
    music_volume: int = 85
    microphone_volume: int = 100
    mic_hotkey: str = "ctrl+alt+m"
    stop_hotkey: str = "ctrl+alt+backspace"
    play_hotkey: str = "ctrl+alt+space"
    random_hotkey: str = "ctrl+alt+r"
    next_hotkey: str = "ctrl+alt+n"
    loop_hotkey: str = "ctrl+alt+l"
    normalize_audio: bool = True
    theme: str = "tokyo"  # see THEMES
    autoplay_mode: str = "off"  # off, random, sequential, loop
    pause_hotkey: str = "ctrl+alt+p"
    volume_up_hotkey: str = "ctrl+alt+up"
    volume_down_hotkey: str = "ctrl+alt+down"
    clicker_target: str = "mouse:left"
    clicker_interval: float = 0.1
    clicker_duration: int = 60  # auto-stop after N seconds; 0 = no limit
    clicker_hotkey: str = "ctrl+alt+c"  # keyboard hotkey: press once to start, again to stop
    clicker_mouse_hotkey: str = ""  # optional mouse hotkey: "mouse:middle" / "mouse:x1" / "mouse:x2"
    clicker_delay: float = 3.0  # seconds to wait before clicking starts (0.5 - 3.0)
    clicker_presets: list = field(default_factory=list)  # [{name, target, interval, duration, delay}]
    hold_target: str = "key:v"
    hold_duration: int = 5  # auto-release after N seconds; 0 = until stopped manually
    hold_delay: float = 3.0  # seconds to wait before the hold starts (0.5 - 3.0)
    hold_hotkey: str = "ctrl+alt+h"  # keyboard hotkey: press once to start, again to stop
    hold_mouse_hotkey: str = ""  # optional mouse hotkey: "mouse:middle" / "mouse:x1" / "mouse:x2"
    # Full file paths are stored directly, so these lists never depend on a folder.
    music_folders: list = field(default_factory=list)  # every folder merged into one library
    favorites: list = field(default_factory=list)      # full paths of favourite songs
    last_view_mode: str = "all"   # remembers whether the 全部/最愛 tab was active on exit
    last_track: str = ""          # full path of the song that was selected/playing on exit (全部 tab)
    last_fav_track: str = ""      # same, but remembered separately for the 最愛 tab
    # ---- online sources ----
    radio_favorites: list = field(default_factory=list)  # [{name, url, urls, cc, tags, custom}]
    radio_country: str = ""       # "" = 全球, otherwise an ISO country code such as "TW"
    radio_genre: str = "ambient"  # Radio Browser tag; "" = 不限
    radio_view: str = "all"       # "all" or "fav"
    yt_favorites: list = field(default_factory=list)     # [{id, title, url, channel, duration, file}]
    yt_after: str = "next"        # what to do when a YouTube song ends: stop / next / loop
    yt_cookies_browser: str = ""  # "", "chrome", "edge", "firefox" (only needed when YouTube asks for a login)
    tools_dir: str = ""           # where yt-dlp.exe / deno.exe live; "" = default (%APPDATA%\\EchoDeck\\tools)
    viz_style: str = "bars"       # playing visualiser: off / bars / mirror / wave
    playlists: list = field(default_factory=list)       # [{id, name, tracks: [full paths]}]
    last_pl_tracks: dict = field(default_factory=dict)  # playlist id -> last selected song
    yt_fav_dir: str = ""          # where favourite YouTube songs are saved; "" = default (%APPDATA%\\EchoDeck\\YouTube最愛)


PEAK_CACHE_PATH = CONFIG_PATH.parent / "peaks.json"
_peak_cache = None
_peak_lock = threading.Lock()


def peak_cache_key(path):
    if "://" in str(path):      # network sources have no stable file identity
        return None
    try:
        st = Path(path).stat()
        return f"{path}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return None


def peak_cache_get(key):
    """Peak level found by an earlier full playback of the same file (0 = unknown)."""
    global _peak_cache
    if not key:
        return 0.0
    with _peak_lock:
        if _peak_cache is None:
            try:
                _peak_cache = json.loads(PEAK_CACHE_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _peak_cache = {}
        return float(_peak_cache.get(key, 0.0))


def peak_cache_put(key, peak):
    if not key:
        return
    with _peak_lock:
        if _peak_cache is None:
            return
        _peak_cache[key] = round(float(peak), 5)
        for old in list(_peak_cache)[:max(0, len(_peak_cache) - 5000)]:
            del _peak_cache[old]
        try:
            PEAK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            PEAK_CACHE_PATH.write_text(json.dumps(_peak_cache), encoding="utf-8")
        except OSError:
            pass


# ---- online sources: web radio + YouTube --------------------------------------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EchoDeck/2.4"
APP_DIR = CONFIG_PATH.parent
TOOLS_DIR_DEFAULT = APP_DIR / "tools"
_TOOLS_CUSTOM = [""]                                     # folder chosen in 設定 ("" = use the default)


def tools_dir():
    return Path(_TOOLS_CUSTOM[0]) if _TOOLS_CUSTOM[0] else TOOLS_DIR_DEFAULT


def ytdlp_exe():
    return tools_dir() / "yt-dlp.exe"                    # downloaded on first use (see yt_tools_dialog)


def deno_exe():
    return tools_dir() / "deno.exe"
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

RADIO_COUNTRIES = [("全球", ""), ("台灣", "TW"), ("日本", "JP"), ("韓國", "KR"), ("美國", "US"),
                   ("英國", "GB"), ("德國", "DE"), ("法國", "FR"), ("加拿大", "CA"), ("澳洲", "AU")]
RADIO_GENRES = [("純音樂", "instrumental"), ("氛圍", "ambient"), ("Lofi", "lofi"), ("古典", "classical"),
                ("爵士", "jazz"), ("鋼琴", "piano"), ("放鬆", "chillout"), ("冥想", "meditation"),
                ("電子", "electronic"), ("不限", "")]
COUNTRY_NAMES = {code: name for name, code in RADIO_COUNTRIES if code}
COUNTRY_NAMES.update({"HK": "香港", "CN": "中國", "SG": "新加坡", "NL": "荷蘭", "IT": "義大利", "ES": "西班牙",
                      "CH": "瑞士", "AT": "奧地利", "RU": "俄羅斯", "BR": "巴西", "SE": "瑞典", "BE": "比利時",
                      "NZ": "紐西蘭", "TH": "泰國", "PL": "波蘭", "CZ": "捷克"})
GENRE_NAMES = {value: name for name, value in RADIO_GENRES if value}
GENRE_NAMES.update({"downtempo": "Downtempo", "lounge": "Lounge", "soundtrack": "影視原聲", "mellow": "Mellow"})
# Stations tagged like this are almost always talking, so they are hidden when browsing by genre.
TALK_TAGS = {"talk", "talk radio", "news", "news talk", "sports", "sport", "podcast", "politics", "comedy",
             "religion", "christian", "gospel", "preaching", "call-in", "public radio", "interview"}


def _soma(slug, name, tags):
    return {"name": f"SomaFM {name}", "cc": "US", "tags": tags, "bitrate": 128, "curated": True,
            "urls": [f"https://ice.somafm.com/{slug}", f"https://somafm.com/{slug}.pls"]}


# A short hand-picked list that needs no network lookup. Every entry is a station that is
# (nearly) free of presenters. Taiwan / Japan / Korea stations come from the live search.
CURATED_STATIONS = [
    _soma("groovesalad", "Groove Salad", ["ambient", "downtempo", "chillout"]),
    _soma("dronezone", "Drone Zone", ["ambient", "meditation"]),
    _soma("deepspaceone", "Deep Space One", ["ambient", "meditation"]),
    _soma("spacestation", "Space Station", ["electronic", "ambient"]),
    _soma("lush", "Lush", ["chillout", "downtempo"]),
    _soma("fluid", "Fluid", ["instrumental", "chillout"]),
    _soma("sonicuniverse", "Sonic Universe", ["jazz", "instrumental"]),
    _soma("secretagent", "Secret Agent", ["lounge", "soundtrack", "chillout"]),
    _soma("illstreet", "Illinois Street Lounge", ["jazz", "lounge"]),
    _soma("n5md", "n5MD", ["ambient", "electronic"]),
    _soma("synphaera", "Synphaera", ["ambient", "electronic"]),
    {"name": "Radio Swiss Classic", "cc": "CH", "tags": ["classical", "instrumental"], "bitrate": 128,
     "curated": True, "urls": ["https://stream.srg-ssr.ch/m/rsc_de/mp3_128"]},
    {"name": "Radio Swiss Jazz", "cc": "CH", "tags": ["jazz", "instrumental"], "bitrate": 128,
     "curated": True, "urls": ["https://stream.srg-ssr.ch/m/rsj/mp3_128"]},
    {"name": "FIP Jazz", "cc": "FR", "tags": ["jazz"], "bitrate": 128, "curated": True,
     "urls": ["https://icecast.radiofrance.fr/fipjazz-midfi.mp3"]},
    {"name": "FIP Groove", "cc": "FR", "tags": ["chillout", "electronic"], "bitrate": 128, "curated": True,
     "urls": ["https://icecast.radiofrance.fr/fipgroove-midfi.mp3"]},
    {"name": "Radio Paradise Mellow Mix", "cc": "US", "tags": ["chillout", "mellow"], "bitrate": 128,
     "curated": True, "urls": ["https://stream.radioparadise.com/mellow-128"]},
]

YT_SUGGESTIONS = [
    ("Lofi 純音樂", "lofi hip hop instrumental no lyrics"), ("鋼琴放鬆", "relaxing piano music no talking"),
    ("爵士器樂", "jazz instrumental music"), ("氛圍 Ambient", "ambient music long mix"),
    ("讀書專注", "study focus music no lyrics"), ("古典音樂", "classical music playlist"),
    ("24/7 直播", "lofi 24/7 live radio"),
]


def fmt_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _http_get(url, timeout=10, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(limit) if limit else response.read()


def resolve_playlist_url(url):
    """.pls / .m3u / .asx are tiny text files that point at the real stream; FFmpeg cannot open them."""
    low = url.lower().split("?")[0]
    if not low.endswith((".pls", ".m3u", ".asx")):
        return [url]
    try:
        text = _http_get(url, 8, 65536).decode("utf-8", "replace")
        found = re.findall(r"https?://[^\s\"'<>]+", text)
        return found[:3] or [url]
    except Exception:
        return [url]


# -- Radio Browser (community station directory) --
_RB_FALLBACK = ["https://de1.api.radio-browser.info", "https://de2.api.radio-browser.info",
                "https://fi1.api.radio-browser.info", "https://nl1.api.radio-browser.info",
                "https://at1.api.radio-browser.info"]
_rb_servers = None


def radio_servers():
    global _rb_servers
    if _rb_servers is None:
        names = []
        try:
            data = json.loads(_http_get("https://all.api.radio-browser.info/json/servers", 6).decode("utf-8"))
            names = [f"https://{d['name']}" for d in data if isinstance(d, dict) and d.get("name")]
        except Exception:
            pass
        random.shuffle(names)
        _rb_servers = names + [s for s in _RB_FALLBACK if s not in names]
    return _rb_servers


def radio_search(params, limit=150):
    query = urllib.parse.urlencode({"hidebroken": "true", "order": "votes", "reverse": "true",
                                    "limit": str(limit), **params})
    last = None
    for base in radio_servers()[:4]:
        try:
            data = json.loads(_http_get(f"{base}/json/stations/search?{query}", 10).decode("utf-8"))
            return [s for s in data if isinstance(s, dict)]
        except Exception as exc:
            last = exc
    raise RuntimeError(f"連不上電台資料庫（{last}）")


def station_from_rb(s):
    url = (s.get("url_resolved") or s.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    tags = [t.strip().lower() for t in (s.get("tags") or "").split(",") if t.strip()]
    try:
        bitrate = int(s.get("bitrate") or 0)
    except (TypeError, ValueError):
        bitrate = 0
    return {"name": (s.get("name") or "").strip() or "（未命名電台）", "urls": [url],
            "cc": (s.get("countrycode") or "").upper(), "tags": tags[:6], "bitrate": bitrate,
            "codec": (s.get("codec") or "").upper()}


def radio_query(text, cc, tag):
    params = {}
    if text:
        params["name"] = text
    elif tag:
        params["tag"] = tag
    if cc:
        params["countrycode"] = cc
    if not params:              # 全球 + 不限：給一個合理的預設
        params["tag"] = "ambient"
    seen, out = set(), []
    for raw in radio_search(params):
        st = station_from_rb(raw)
        if st is None:
            continue
        if not text and set(st["tags"]) & TALK_TAGS:
            continue
        key = re.sub(r"\W+", "", st["name"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(st)
        if len(out) >= 60:
            break
    return out


def curated_matches(text, cc, tag):
    text = text.strip().lower()
    out = []
    for s in CURATED_STATIONS:
        if text:
            ok = text in s["name"].lower() and (not cc or s["cc"] == cc)
        else:
            ok = (not cc or s["cc"] == cc) and (not tag or tag in s["tags"])
        if ok:
            out.append(s)
    return out


# -- yt-dlp (YouTube). The official yt-dlp.exe is downloaded once and run as a separate program:
#    it brings its own Python + every dependency, so nothing has to be bundled into EchoDeck.exe
#    and updating it is just downloading a new file. --
_ytdlp_ver = [None]


def ytdlp_path():
    if ytdlp_exe().is_file():
        return str(ytdlp_exe())
    return shutil.which("yt-dlp") or shutil.which("yt-dlp.exe") or ""


def ytdlp_available():
    return bool(ytdlp_path())


def _run_hidden(args, timeout):
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW: no console flash
    proc = subprocess.run(args, timeout=timeout, **kwargs)
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"))


def ytdlp_version(refresh=False):
    """Version string (needs a process start, so it is cached; call from a worker thread)."""
    if _ytdlp_ver[0] is None or refresh:
        exe = ytdlp_path()
        ver = ""
        if exe:
            try:
                code, out, _err = _run_hidden([exe, "--version"], 30)
                ver = out.strip() if code == 0 else ""
            except Exception:
                ver = ""
        _ytdlp_ver[0] = ver
    return _ytdlp_ver[0]


def find_js_runtime():
    """YouTube needs a JavaScript runtime (Deno preferred) for yt-dlp to read the video page."""
    if deno_exe().is_file():
        return "deno", str(deno_exe())
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path:
            return name, path
    return None


def _ytdlp_args(browser=""):
    args = ["--ignore-config", "--no-warnings", "--no-colors", "--socket-timeout", "15", "--retries", "2"]
    runtime = find_js_runtime()
    if runtime:
        args += ["--js-runtimes", f"{runtime[0]}:{runtime[1]}"]
    if browser:
        args += ["--cookies-from-browser", browser]
    return args


def _ytdlp_json(args, browser="", timeout=90):
    exe = ytdlp_path()
    if not exe:
        raise RuntimeError("尚未安裝 yt-dlp。請按「🔧 YouTube 元件」→「下載」。")
    try:
        code, out, err = _run_hidden([exe] + _ytdlp_args(browser) + args, timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("YouTube 回應太慢（逾時），請稍後再試。")
    except OSError as exc:
        raise RuntimeError(f"無法執行 yt-dlp：{exc}")
    if code != 0 or not out.strip():
        raise RuntimeError(_yt_friendly_error(err))
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError("yt-dlp 回傳了無法解讀的內容，請試著更新 yt-dlp。")


def _yt_friendly_error(message):
    whole = re.sub(r"\x1b\[[0-9;]*m", "", str(message or "")).strip()
    lines = [ln.strip() for ln in whole.splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("ERROR")]
    text = (errors or lines or [""])[-1].replace("ERROR:", "").strip()
    low = whole.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube 要求驗證。請到「🔧 YouTube 元件」→「Cookies 來源」選你平常登入 YouTube 的瀏覽器。"
    if "js runtime" in low or "javascript" in low or "challenge" in low:
        return "缺少 JavaScript 執行環境。請到「🔧 YouTube 元件」安裝 Deno，必要時再更新 yt-dlp。"
    if any(k in low for k in ("private video", "unavailable", "removed", "not available", "blocked")):
        return "這部影片無法播放（私人、已移除或地區限制）。"
    if any(k in low for k in ("urlopen error", "timed out", "getaddrinfo", "unable to download", "unable to connect",
                              "proxy", "connection", "network")):
        return "連不上 YouTube，請檢查網路。"
    return (text or "未知錯誤")[:160]


def yt_resolve_audio(page_url, browser=""):
    """Video page URL -> direct audio stream URL (+ headers) that FFmpeg can open."""
    info = _ytdlp_json(["-J", "--no-playlist", "-f", "bestaudio/best", page_url], browser)
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), None)
    if not info:
        raise RuntimeError("找不到這個影片。")
    url = info.get("url")
    if not url:
        for fmt in reversed(info.get("formats") or []):
            if fmt.get("url") and fmt.get("acodec") not in (None, "none"):
                url = fmt["url"]
                info["http_headers"] = fmt.get("http_headers") or info.get("http_headers")
                break
    if not url:
        raise RuntimeError("解析不到音訊串流（可能需要更新 yt-dlp 或安裝 Deno）。")
    return {"url": url, "headers": dict(info.get("http_headers") or {}), "live": bool(info.get("is_live")),
            "title": info.get("title") or "", "channel": info.get("channel") or info.get("uploader") or "",
            "duration": info.get("duration")}


_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})")


def yt_list(text, browser=""):
    """Keyword search, playlist, or single link -> list of entries (cheap 'flat' extraction)."""
    text = text.strip()
    is_link = bool(re.match(r"^(https?://|www\.|youtu\.be/|(music\.)?youtube\.com/)", text, re.I))
    if is_link:
        if not re.match(r"^https?://", text, re.I):
            text = "https://" + text
        is_playlist = bool(re.search(r"[?&]list=|/playlist", text)) and not re.search(r"[?&]v=|youtu\.be/", text)
        if not is_playlist:             # one video: no need to fetch anything before playing
            match = _YT_ID.search(text)
            vid = match.group(1) if match else ""
            return {"autoplay": True, "entries": [{
                "id": vid, "title": text, "url": f"https://www.youtube.com/watch?v={vid}" if vid else text,
                "channel": "", "duration": None, "live": False}]}
        target = text
    else:
        target = f"ytsearch20:{text}"
    args = ["-J", "--flat-playlist", "--playlist-end", "30", target]
    info = _ytdlp_json(args, browser)
    raw = [e for e in ((info or {}).get("entries") or []) if e]
    entries = []
    for e in raw:
        vid = e.get("id") or ""
        url = e.get("url") or e.get("webpage_url") or ""
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid or url}"
        entries.append({"id": vid, "title": e.get("title") or "（未命名）", "url": url,
                        "channel": e.get("channel") or e.get("uploader") or "",
                        "duration": e.get("duration"), "live": e.get("live_status") == "is_live"})
    if not entries:
        raise RuntimeError("沒有找到結果。")
    return {"autoplay": is_link, "entries": entries}


YT_FAV_DIR_DEFAULT = APP_DIR / "YouTube最愛"   # default place where favourite songs are saved as real files
_YT_FAV_CUSTOM = [""]                           # folder chosen in 設定 ("" = use the default)


def app_home_dir():
    """Folder that holds EchoDeck.exe (or this script when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def yt_fav_dir():
    custom = _YT_FAV_CUSTOM[0]
    return Path(custom) if custom else YT_FAV_DIR_DEFAULT


def yt_fav_dirs():
    """Every folder EchoDeck may have saved favourites in (current + default)."""
    return [yt_fav_dir(), YT_FAV_DIR_DEFAULT]
YT_TMP = APP_DIR / "yt-tmp"               # scratch space while a favourite downloads


def yt_safe_filename(title, limit=80):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(title or "")).strip(" .")
    name = re.sub(r"\s+", " ", name)[:limit].strip(" .")
    return name or "YouTube 音樂"


def yt_fav_file(entry):
    """Path of the downloaded file of a favourite, or "" when there is none (any more)."""
    path = (entry or {}).get("file") or ""
    return path if path and os.path.isfile(path) else ""


def finalize_audio(src, dest_base):
    """Downloaded audio -> final file. WebM/Opus is re-wrapped (no re-encoding) into a real .opus file;
    anything else keeps its own container."""
    src = Path(src)
    if src.suffix.lower() in (".webm", ".opus", ".ogg"):
        dest = dest_base.with_suffix(".opus")
        try:
            with av.open(str(src)) as inp:
                audio = inp.streams.audio[0]
                if audio.codec_context.name != "opus":
                    raise ValueError("not opus")
                with av.open(str(dest), "w", format="ogg") as out:
                    template = out.add_stream_from_template(audio)
                    for packet in inp.demux(audio):
                        if packet.dts is None:
                            continue
                        packet.stream = template
                        out.mux(packet)
            return dest
        except Exception:
            dest.unlink(missing_ok=True)
    dest = dest_base.with_suffix(src.suffix.lower() if src.suffix.lower() in FORMATS else ".webm")
    shutil.move(str(src), str(dest))
    return dest


class YtFavJob:
    """Downloads one favourite with yt-dlp into YouTube最愛 on a background thread."""

    def __init__(self, entry, browser, on_done):
        self.entry, self.key, self.browser, self.on_done = entry, entry.get("id") or entry.get("url"), browser, on_done
        self.cancelled = False
        self.proc = None
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass

    def _run(self):
        err = ""
        tmp = YT_TMP / hashlib.md5(str(self.key).encode("utf-8")).hexdigest()[:12]
        try:
            exe = ytdlp_path()
            if not exe:
                raise RuntimeError("尚未安裝 yt-dlp，請先到「🔧 YouTube 元件」下載。")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            log = tempfile.TemporaryFile()
            args = [exe] + _ytdlp_args(self.browser) + ["--no-playlist", "--quiet", "--no-progress", "--fixup", "never",
                    "-f", "bestaudio[ext=webm]/bestaudio/best", "-o", str(tmp / "audio.%(ext)s"),
                    self.entry["url"]]
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": log}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000
            self.proc = subprocess.Popen(args, **kwargs)
            code = self.proc.wait()
            if self.cancelled:
                return
            if code != 0:
                log.seek(0)
                raise RuntimeError(_yt_friendly_error(log.read().decode("utf-8", "replace")))
            files = [f for f in tmp.glob("audio.*") if f.suffix not in (".part", ".ytdl")]
            if not files:
                raise RuntimeError("沒有下載到檔案")
            fav_dir = yt_fav_dir()
            fav_dir.mkdir(parents=True, exist_ok=True)
            base = fav_dir / yt_safe_filename(self.entry.get("title"))
            if any(base.with_suffix(ext).exists() for ext in (".opus", ".webm", ".m4a")):
                base = fav_dir / f"{base.name} ({str(self.key)[-6:]})"
            final = finalize_audio(files[0], base)
            if self.cancelled:
                final.unlink(missing_ok=True)
                return
            self.entry["file"] = str(final)
        except Exception as exc:
            err = str(exc) or exc.__class__.__name__
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not self.cancelled:
            self.on_done(self.key, err)


class YtPipe:
    """File-like object over `yt-dlp -o -`. YouTube often refuses (HTTP 403) when FFmpeg opens a direct
    googlevideo link, but yt-dlp's own downloader gets through; so yt-dlp downloads and FFmpeg reads
    the bytes from its output. Not seekable (like a radio), but the song's length is known."""

    def __init__(self, page_url, browser=""):
        exe = ytdlp_path()
        if not exe:
            raise RuntimeError("尚未安裝 yt-dlp。")
        self.err = tempfile.TemporaryFile()
        args = [exe] + _ytdlp_args(browser) + ["--no-playlist", "--quiet", "--no-part", "--no-progress",
                                                "-f", "bestaudio[ext=webm]/bestaudio/best", "-o", "-", page_url]
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": self.err}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000
        self.proc = subprocess.Popen(args, **kwargs)

    def read(self, size=-1):
        return self.proc.stdout.read1(size if size and size > 0 else 65536)

    def error_text(self):
        try:
            self.err.seek(0)
            return self.err.read().decode("utf-8", "replace")
        except Exception:
            return ""

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.stdout.close()
        except Exception:
            pass
        try:
            self.err.close()
        except Exception:
            pass


def _download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as response, open(part, "wb") as out:
        shutil.copyfileobj(response, out, 1 << 16)
    return part


def update_ytdlp():
    part = _download(YTDLP_URL, ytdlp_exe())
    try:
        with open(part, "rb") as f:
            if f.read(2) != b"MZ":                      # a Windows program starts with "MZ"
                raise RuntimeError("下載到的檔案不是有效的 yt-dlp.exe")
        os.replace(part, ytdlp_exe())
    finally:
        if part.exists():
            part.unlink()
    ytdlp_version(refresh=True)


def install_deno():
    part = _download(DENO_URL, tools_dir() / "deno.zip")
    try:
        with zipfile.ZipFile(part) as z:
            name = next((n for n in z.namelist() if n.lower().endswith("deno.exe")), None)
            if name is None:
                raise RuntimeError("下載到的檔案裡沒有 deno.exe")
            with z.open(name) as src, open(deno_exe(), "wb") as out:
                shutil.copyfileobj(src, out)
    finally:
        if part.exists():
            part.unlink()


class TrackStreamer:
    """Decodes one file on a background thread, a few seconds ahead of playback.

    Memory stays at a few MB whatever the file length (a 1 h song used to be
    decoded whole: ~1.4 GB plus temporary copies). Samples are 48 kHz stereo
    float32. FFmpeg (PyAV) does the decoding AND the high-quality resampling /
    channel mixing, so mp3/m4a/aac/wma/flac/ogg/wav/opus all work.

    Blocks are stored with an absolute frame index so the virtual-cable output
    and the local monitor can read the same song at slightly different
    positions, exactly like before."""
    RATE = 48000
    MAX_AHEAD = RATE * 3        # never decode more than 3 s ahead of the output
    KEEP_BEHIND = RATE // 2     # keep 0.5 s so the monitor stream may lag a little
    PREBUFFER = RATE // 4       # play_file() returns once this much is ready
    MIN_BLOCK = 4096            # coalesce tiny decoder frames into bigger blocks
    NEVER = 1 << 60
    FADE_IN = RATE // 4         # soften the hard onset of a new song

    def __init__(self, path, normalize=True, live=False, headers=None, duration=0, opener=None):
        self.path = str(path)
        self.is_url = "://" in self.path
        self.opener = opener                      # () -> file-like source (YtPipe); such a source cannot seek
        self._source = None
        self.nosee = opener is not None
        self.live = bool(live)                    # radio / live stream: endless, no seek, auto-reconnect
        self.headers = headers or {}
        self.normalize = normalize
        self.status = ""                          # shown in the UI, e.g. while reconnecting
        self.meta_title = ""                      # ICY "now playing" text of a radio stream (best effort)
        self.max_ahead = self.RATE * (8 if self.live else 3)
        self.prebuffer = self.RATE * 3 // 2 if self.live else self.PREBUFFER
        self.container = None
        self._open()                              # raises on unreadable files / unreachable URLs
        self.cond = threading.Condition()
        self.blocks = deque()                     # (start_frame, float32[n, 2])
        self.pos = 0                              # next frame the output will play
        self.mon_pos = 0                          # next frame the monitor will play
        self.end = 0                              # frame index after the last decoded block
        self.eof = False
        self.finished = False
        self.closing = False
        self.error = ""
        self.gen = 0
        self.ready = threading.Event()
        self._seek = None
        self._moved = False
        self._pending, self._pending_frames = [], 0
        self._dpos = 0                            # decoder-thread only: timeline index of next sample
        self._target = 0
        self._sync = False
        self.cache_key = peak_cache_key(self.path)
        self.peak = peak_cache_get(self.cache_key)
        self._gain = self._gain_for(self.peak)
        self._played = False
        self.total_est = self._estimate_total()
        if not self.total_est and duration and not self.live:
            self.total_est = int(float(duration) * self.RATE)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    # ------------------------------------------------------------- public API
    def total(self):
        with self.cond:
            if self.live:
                return 0
            return self.end if self.eof else max(self.total_est, self.end)

    def read(self, frames):
        """Output stream: take up to `frames` samples (zero padded) and advance."""
        with self.cond:
            self.pos = max(self.pos, self._first_start())
            out, got = self._gather(self.pos, frames)
            self._fade_in(out, self.pos, got)
            self.pos += got
            if self.eof and self.pos >= self.end:
                self.finished = True
            self._trim()
            self.cond.notify_all()
            return out, got

    def read_monitor(self, frames):
        with self.cond:
            if self.mon_pos < self._first_start():
                self.mon_pos = self._first_start()
            out, got = self._gather(self.mon_pos, frames)
            self._fade_in(out, self.mon_pos, got)
            self.mon_pos += got
            return out, got

    def seek(self, frame):
        if self.live or self.nosee:
            return
        with self.cond:
            if self.eof or self.total_est:
                frame = min(frame, self.total() - 1)
            frame = max(0, frame)
            self.gen += 1
            self.blocks.clear()
            self.pos = self.mon_pos = self.end = frame
            self.eof = self.finished = False
            self._seek = frame
            self.cond.notify_all()

    def close(self):
        with self.cond:
            self.closing = True
            self.cond.notify_all()

    # --------------------------------------------------------------- internals
    def _first_start(self):
        return self.blocks[0][0] if self.blocks else self.end

    def _trim(self):
        limit = self.pos - self.KEEP_BEHIND
        while self.blocks and self.blocks[0][0] + len(self.blocks[0][1]) <= limit:
            self.blocks.popleft()

    def _fade_in(self, out, index, got):
        if got and index < self.FADE_IN:
            n = min(got, self.FADE_IN - index)
            out[:n] *= (np.arange(index, index + n, dtype=np.float32) / self.FADE_IN)[:, None]

    def _gather(self, index, frames):
        out = np.zeros((frames, 2), dtype=np.float32)
        got = 0
        for start, arr in self.blocks:
            cur = index + got
            if start + len(arr) <= cur:
                continue
            if start > cur:
                break
            offset = cur - start
            n = min(len(arr) - offset, frames - got)
            out[got:got + n] = arr[offset:offset + n]
            got += n
            if got >= frames:
                break
        return out, got

    def _open(self):
        if self.opener is not None:
            self._source = self.opener()
            self.container = av.open(self._source, timeout=(12.0, 12.0))
        elif self.is_url:
            options = {"reconnect": "1", "reconnect_streamed": "1", "reconnect_delay_max": "5",
                       "user_agent": self.headers.get("User-Agent") or UA}
            extra = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items()
                            if k.lower() not in ("user-agent", "host", "accept-encoding"))
            if extra:
                options["headers"] = extra
            self.container = av.open(self.path, options=options, timeout=(12.0, 12.0))
        else:
            self.container = av.open(self.path)
        try:
            self.stream = self.container.streams.audio[0]
        except IndexError:
            self.container.close()
            raise ValueError("這個檔案裡沒有音訊")

    def _reconnect(self):
        """Live streams drop now and then: reopen with a growing pause. False = gave up."""
        delay = 0.5
        for attempt in range(1, 11):
            if self.closing:
                return False
            self.status = f"連線中斷，重新連線中…（{attempt}）"
            try:
                self.container.close()
            except Exception:
                pass
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if self.closing:
                    return False
                time.sleep(0.1)
            try:
                self._open()
                self.status = ""
                return True
            except Exception:
                delay = min(delay * 2, 8.0)
        self.status = ""
        return False

    def _poll_meta(self):
        try:
            meta = self.container.metadata or {}
            title = (meta.get("StreamTitle") or meta.get("icy-title") or "").strip()
            if title:
                self.meta_title = title
        except Exception:
            pass

    def _estimate_total(self):
        if self.live:
            return 0
        seconds = None
        try:
            if self.stream.duration is not None and self.stream.time_base:
                seconds = float(self.stream.duration * self.stream.time_base)
            elif self.container.duration:
                seconds = self.container.duration / av.time_base
            elif self.stream.bit_rate:
                seconds = Path(self.path).stat().st_size * 8 / self.stream.bit_rate
        except Exception:
            seconds = None
        return int((seconds or 0) * self.RATE)

    def _gain_for(self, peak):
        return min(1.0, 0.80 / peak) if (self.normalize and peak > 0) else 1.0

    def _apply_gain(self, arr):
        """Peak normalisation without scanning the whole file first: the gain
        follows the loudest peak decoded so far (it can only go down, smoothly),
        and a finished play is remembered so the next play is exact."""
        if not len(arr):
            return arr
        peak = float(np.max(np.abs(arr)))
        if peak > self.peak:
            self.peak = peak
        target = self._gain_for(self.peak)
        if target != self._gain:
            gain = np.full((len(arr), 1), target, dtype=np.float32)
            if self._played:
                # short ramp (<= 5 ms) so a gain change cannot click; the very
                # first block has nothing before it, so it gets the gain at once
                n = min(len(arr), 240)
                gain[:n, 0] = np.linspace(self._gain, target, n, dtype=np.float32)
            self._gain = target
            self._played = True
            return arr * gain
        self._played = True
        return arr * np.float32(target) if target != 1.0 else arr

    def _flush_pending(self, gen, force=False):
        if not self._pending or (self._pending_frames < self.MIN_BLOCK and not force):
            return True
        arr = self._pending[0] if len(self._pending) == 1 else np.concatenate(self._pending)
        self._pending, self._pending_frames = [], 0
        # drop everything before the requested seek position
        if self._dpos + len(arr) <= self._target:
            self._dpos += len(arr)
            return True
        if self._dpos < self._target:
            arr = arr[self._target - self._dpos:]
            self._dpos = self._target
        arr = self._apply_gain(arr)
        with self.cond:
            if gen != self.gen or self.closing:
                return False
            self.blocks.append((self._dpos, arr))
            self.end = self._dpos + len(arr)
            if self.end - self.pos >= self.prebuffer:
                self.ready.set()
            self.cond.notify_all()
        self._dpos += len(arr)
        return True

    def _convert(self, resampler, frame):
        """AudioFrame -> list of float32[n, 2] blocks at 48 kHz stereo."""
        out = resampler.resample(frame)
        if out is None:
            return []
        if not isinstance(out, (list, tuple)):
            out = [out]
        return [f.to_ndarray().reshape(-1, 2) for f in out]

    def _new_resampler(self):
        return av.AudioResampler(format="flt", layout="stereo", rate=self.RATE)

    def _run(self):
        resampler, packets, bad = None, None, 0
        try:
            while True:
                with self.cond:
                    while True:
                        if self.closing:
                            return
                        if self._seek is not None:
                            break
                        if self.eof:
                            self.cond.wait(0.1)
                        elif self.end - self.pos < self.max_ahead:
                            break
                        else:
                            self.cond.wait(0.05)
                    seek, self._seek = self._seek, None
                    gen = self.gen
                if seek is not None or packets is None:
                    self._pending, self._pending_frames = [], 0
                    if self.live:                  # (re)connect: the timeline just keeps counting
                        self._target = self._dpos = max(self.end, self._dpos)
                    else:
                        self._target = seek or 0
                        self._dpos = self._target
                    if seek is not None:
                        self._moved = True
                        self.container.seek(int((seek / self.RATE) / self.stream.time_base),
                                            stream=self.stream, backward=True, any_frame=False)
                        self._sync = True
                    packets = self.container.demux(self.stream)
                    resampler = self._new_resampler()
                try:
                    try:
                        packet = next(packets)
                    except StopIteration:
                        raise
                    except Exception:
                        if not self.live:
                            raise
                        raise StopIteration          # network error on a live stream = dropped connection
                except StopIteration:
                    if self.live:                  # a radio never ends: the connection dropped
                        if self._reconnect():
                            packets = None
                            continue
                        raise RuntimeError("電台連線中斷，無法重新連線")
                    for block in self._convert(resampler, None):
                        self._pending.append(block)
                    self._flush_pending(gen, force=True)
                    with self.cond:
                        if gen == self.gen:
                            self.eof = True
                            self.ready.set()
                            self.cond.notify_all()
                    if not self._moved:
                        peak_cache_put(self.cache_key, self.peak)
                    continue
                try:
                    decoded = packet.decode()
                    bad = 0
                except Exception as exc:       # skip a damaged packet, keep playing
                    decoded = []
                    bad += 1
                    if bad > 200:
                        if self.live and self._reconnect():
                            packets, bad = None, 0
                            continue
                        raise RuntimeError(f"解碼持續失敗：{exc}")
                if self.live:
                    self._poll_meta()
                if self.end - self.pos > self.RATE // 2:
                    time.sleep(0.003)       # already 0.5 s ahead: yield the GIL to the audio callback
                for frame in decoded:
                    if self._sync:
                        t = frame.time
                        self._dpos = int(round(t * self.RATE)) if t is not None else self._target
                        self._sync = False
                    for block in self._convert(resampler, frame):
                        self._pending.append(block)
                        self._pending_frames += len(block)
                    if not self._flush_pending(gen):
                        break
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            with self.cond:
                self.eof = True
                self.ready.set()
                self.cond.notify_all()
        finally:
            try:
                self.container.close()
            except Exception:
                pass
            if self._source is not None:
                self._source.close()


class AudioMixer:
    """One virtual-cable output stream, with bounded microphone buffering.

    The input callback only appends small PCM blocks. The output callback is
    the sole writer to the virtual output, combines effect + mic, and clips
    the result. Dropping oldest blocks keeps latency bounded under load.
    """
    RATE = 48000
    CHANNELS = 2
    MAX_MIC_FRAMES = int(48000 * 0.12)  # keep mic latency under ~120 ms
    GATE_RAMP = 480  # 10 ms fade so muting/unmuting does not click
    STOP_FADE = 480  # 10 ms fade so a stop is instant but click-free

    def __init__(self, on_error):
        self.on_error = on_error
        self.output_stream = None
        self.input_stream = None
        self.monitor_stream = None
        self.mic_chunks = deque()
        self.mic_frames = 0
        self.mic_lock = threading.Lock()
        self.mic_hold = False  # app forces mic mute (PTT lead-in / release tail)
        self.mic_gate = 0.0
        self.stopping = False
        self.mic_in_peak = 0.0
        self.mic_out_peak = 0.0
        self.underflows = 0
        self._viz_tail = np.zeros(2048, dtype=np.float32)   # last ~43 ms of the song, mono (for the visualiser)
        self.lock = threading.RLock()
        self.streamer = None
        self.manual_stop = False
        self.mic_enabled = False
        self.mute_mic_while_sound = True
        self.music_gain = .85
        self.mic_gain = 1.0
        self.paused = False

    @staticmethod
    def devices(kind):
        try:
            return [d['name'] for d in sd.query_devices()
                    if d['max_' + kind + '_channels'] > 0]
        except Exception:
            return []

    @staticmethod
    def _device_id(name, kind):
        for index, device in enumerate(sd.query_devices()):
            if device['name'] == name and device['max_' + kind + '_channels'] > 0:
                return index
        raise ValueError(f"找不到音訊裝置：{name}")

    def configure_output(self, output_name, monitor_name, monitor_enabled):
        self._close_stream('output_stream')
        self._close_stream('monitor_stream')
        try:
            output_id = self._device_id(output_name, 'output')
            self.output_stream = sd.OutputStream(device=output_id, samplerate=self.RATE,
                channels=self.CHANNELS, dtype='float32', latency=0.08, callback=self._output_callback)
            self.output_stream.start()
            if monitor_enabled and monitor_name and monitor_name != output_name:
                monitor_id = self._device_id(monitor_name, 'output')
                self.monitor_stream = sd.OutputStream(device=monitor_id, samplerate=self.RATE,
                    channels=self.CHANNELS, dtype='float32', latency=0.08, callback=self._monitor_callback)
                self.monitor_stream.start()
        except Exception as exc:
            self._close_stream('output_stream')
            self._close_stream('monitor_stream')
            self.on_error(f"無法開啟音樂／虛擬聲卡輸出：{exc}")

    def configure_microphone(self, input_name):
        self._close_stream('input_stream')
        self._clear_mic()
        if not input_name:
            return
        try:
            input_id = self._device_id(input_name, 'input')
            # Keeping capture at 48k makes input/output PCM compatible. If a
            # device does not support it, PortAudio fails cleanly and the UI
            # reports the error instead of crashing.
            self.input_stream = sd.InputStream(device=input_id, samplerate=self.RATE,
                channels=1, dtype='float32', callback=self._input_callback)
            self.input_stream.start()
        except Exception as exc:
            self._close_stream('input_stream')
            self.on_error(f"無法開啟麥克風：{exc}")

    def _clear_mic(self):
        with self.mic_lock:
            self.mic_chunks.clear()
            self.mic_frames = 0

    def _input_callback(self, indata, _frames, _time, _status):
        block = np.repeat(indata, self.CHANNELS, axis=1)
        self.mic_in_peak = max(self.mic_in_peak, float(np.max(np.abs(indata))))
        with self.mic_lock:
            self.mic_chunks.append(block)
            self.mic_frames += len(block)
            # Bound latency: drop the oldest audio, never let it pile up.
            while self.mic_frames > self.MAX_MIC_FRAMES and len(self.mic_chunks) > 1:
                self.mic_frames -= len(self.mic_chunks.popleft())

    def _take_song(self, frames):
        with self.lock:
            st = self.streamer
            if st is None:
                return np.zeros((frames, self.CHANNELS), dtype=np.float32), False
            if self.paused and not self.stopping:
                return np.zeros((frames, self.CHANNELS), dtype=np.float32), not st.finished
            piece, got = st.read(frames)
            if self.stopping:
                if got:
                    self._fade_out(piece[:got])
                st.finished = True
                st.close()
            # "playing" stays true during a brief decoder underrun (silence), so
            # the mic does not un-mute and PTT is not released mid-song.
            return piece, (not st.finished) or got > 0

    def _fade_out(self, piece):
        n = min(len(piece), self.STOP_FADE)
        if n:
            piece[:n] *= np.linspace(1.0, 0.0, n, dtype=np.float32)[:, None]
        piece[n:] = 0
        return piece

    def _take_mic(self, frames):
        result = np.zeros((frames, self.CHANNELS), dtype=np.float32)
        written = 0
        with self.mic_lock:
            while written < frames and self.mic_chunks:
                chunk = self.mic_chunks[0]
                count = min(frames - written, len(chunk))
                result[written:written + count] = chunk[:count]
                written += count
                self.mic_frames -= count
                if count == len(chunk):
                    self.mic_chunks.popleft()
                else:
                    self.mic_chunks[0] = chunk[count:]
        return result

    def _tap(self, block):
        """Keep the newest samples for the visualiser (cheap; must never disturb the audio)."""
        try:
            n = len(block)
            if n == 0:
                return
            mono = (block[:, 0] + block[:, 1]) * 0.5 if block.shape[1] > 1 else block[:, 0]
            buf = self._viz_tail
            if n >= len(buf):
                buf[:] = mono[-len(buf):]
            else:
                buf[:-n] = buf[n:]
                buf[-n:] = mono
        except Exception:
            pass

    def viz_samples(self):
        return self._viz_tail.copy()

    def _output_callback(self, outdata, frames, _time, status):
        if status.output_underflow:
            self.underflows += 1
        effect, effect_playing = self._take_song(frames)
        self._tap(effect)
        mix = effect * self.music_gain
        muted = (not self.mic_enabled) or (
            self.mute_mic_while_sound and (effect_playing or self.mic_hold))
        # Always drain the mic buffer, even when muted.  Otherwise old voice
        # piles up and is replayed the moment the effect ends.
        mic = self._take_mic(frames)
        target = 0.0 if muted else 1.0
        if self.mic_gate != 0.0 or target != 0.0:
            gate = np.full(frames, target, dtype=np.float32)
            if self.mic_gate != target:
                n = min(frames, self.GATE_RAMP)
                gate[:n] = np.linspace(self.mic_gate, target, n, dtype=np.float32)
            self.mic_gate = target
            sent = mic * (gate * self.mic_gain)[:, None]
            self.mic_out_peak = max(self.mic_out_peak, float(np.max(np.abs(sent))))
            mix += sent
        if muted:
            self._clear_mic()
        # Explicit clipping prevents wraparound/distortion if users set 200%.
        np.clip(mix, -1.0, 1.0, out=outdata)

    def _monitor_callback(self, outdata, frames, _time, status):
        if status.output_underflow:
            self.underflows += 1
        # Local monitor intentionally excludes microphone to avoid echo.
        effect, _playing = self._take_monitor(frames)
        if self.output_stream is None:          # no virtual cable open: feed the visualiser from the monitor
            self._tap(effect)
        np.clip(effect * self.music_gain, -1.0, 1.0, out=outdata)

    def _take_monitor(self, frames):
        # The monitor reads the same song at its own position, so it never
        # consumes the virtual-cable stream.
        with self.lock:
            st = self.streamer
            if st is None or (self.paused and not self.stopping):
                return np.zeros((frames, self.CHANNELS), dtype=np.float32), st is not None
            piece, got = st.read_monitor(frames)
            if self.stopping:
                if got:
                    self._fade_out(piece[:got])
                st.mon_pos = st.NEVER
            return piece, got > 0

    def release_song_if_finished(self):
        """Drop the finished song's decoder once every stream has played it out."""
        with self.lock:
            st = self.streamer
            if st is None or self.paused or not st.finished:
                return
            if self.monitor_stream is not None and st.mon_pos < st.end:
                return
            self.streamer = None
        st.close()

    def open_stream(self, path, normalize=True, live=False, headers=None, duration=0, opener=None):
        """Open + pre-buffer a song or network stream. BLOCKS (a URL can take seconds):
        call it from a worker thread. Returns a started streamer or raises RuntimeError."""
        try:
            st = TrackStreamer(path, normalize, live=live, headers=headers, duration=duration, opener=opener)
        except Exception as exc:
            raise RuntimeError(str(exc) or exc.__class__.__name__)
        st.start()
        # first moments decoded: a local file needs a few ms, a network stream seconds, yt-dlp's pipe longer
        st.ready.wait(45.0 if opener is not None else 15.0 if st.is_url else 3.0)
        if st.error and st.end == 0:
            message = st.error
            st.close()
            raise RuntimeError(message)
        if st.is_url and st.end == 0:
            st.close()
            raise RuntimeError("串流沒有回應（逾時）")
        return st

    def install(self, st):
        """Make an opened streamer the current song (the old one is closed)."""
        with self.lock:
            old, self.streamer = self.streamer, st
            self.manual_stop = self.stopping = self.paused = False
        if old is not None:
            old.close()

    def play_file(self, path, normalize=True):
        try:
            st = self.open_stream(path, normalize)
        except Exception as exc:
            self.on_error(f"無法讀取音效檔：{exc}")
            return False
        self.install(st)
        return True

    def stop_effect(self):
        # Streams finish with a 10 ms fade on their next callback, then go
        # silent.  If a stream is not running, jump straight to the end.
        with self.lock:
            self.manual_stop = True
            self.stopping = True
            self.paused = False
            st = self.streamer
            if st is not None:
                if self.output_stream is None:
                    st.finished = True
                    st.close()
                if self.monitor_stream is None:
                    st.mon_pos = st.NEVER

    def seek(self, percent):
        with self.lock:
            st = self.streamer
            if st is None:
                return
            st.seek(int(st.total() * max(0, min(100, percent)) / 100))
            self.stopping = False

    def progress(self):
        with self.lock:
            st = self.streamer
            total = st.total() if st is not None else 0
            if not total:
                return 0
            return int(min(st.pos, total) * 100 / total)

    def times(self):
        """Elapsed / total playback time in seconds (0, 0) when nothing is loaded."""
        with self.lock:
            st = self.streamer
            total = st.total() if st is not None else 0
            if st is not None and st.live:
                return st.pos / self.RATE, 0.0
            if not total:
                return 0.0, 0.0
            return min(st.pos, total) / self.RATE, total / self.RATE

    def can_seek(self):
        with self.lock:
            st = self.streamer
            return st is not None and not st.live and not st.nosee

    def stream_info(self):
        """(is_live, status text, now-playing text) of the current stream."""
        with self.lock:
            st = self.streamer
            if st is None:
                return False, "", ""
            return st.live, st.status, st.meta_title

    def effect_playing(self):
        with self.lock:
            st = self.streamer
            return st is not None and not st.finished

    def _close_stream(self, attribute):
        stream = getattr(self, attribute)
        if stream:
            try:
                stream.stop(); stream.close()
            except Exception:
                pass
        setattr(self, attribute, None)

    def close(self):
        st, self.streamer = self.streamer, None
        if st is not None:
            st.close()
        for attribute in ('input_stream', 'output_stream', 'monitor_stream'):
            self._close_stream(attribute)


# ---- library list rows -------------------------------------------
_UR = int(Qt.ItemDataRole.UserRole)
ROLE_PATH, ROLE_NAME, ROLE_SEARCH, ROLE_NUM = _UR, _UR + 1, _UR + 2, _UR + 3
ROLE_FAV, ROLE_MISSING, ROLE_PLAYING, ROLE_SUB = _UR + 4, _UR + 5, _UR + 6, _UR + 7
ROLE_DATA = _UR + 8          # the full station / video dict of an online row


def path_key(path) -> str:
    """Case/slash-insensitive identity of a file path (Windows paths are case-insensitive)."""
    return os.path.normcase(os.path.normpath(str(path)))


class TrackDelegate(QStyledItemDelegate):
    """Paints one song row: number, ★, title and the folder tag.

    Missing files are drawn grey with a 「檔案遺失」 tag; the playing row gets an
    accent bar. The ★ geometry is shared by painting and hit-testing.
    """

    @staticmethod
    def star_rect(r):
        return QRect(r.left() + 48, r.top(), 28, r.height())

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 46)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(2, 1, -2, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        playing = bool(index.data(ROLE_PLAYING))
        missing = bool(index.data(ROLE_MISSING))
        fav = bool(index.data(ROLE_FAV))
        bg = T["SEL"] if selected else T["ACC_SOFT"] if playing else T["RAISED"] if hover else None
        if bg:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(bg))
            painter.drawRoundedRect(r, 9, 9)
        if playing:
            painter.setBrush(QColor(T["ACC_BAR"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRect(r.left() + 3, r.top() + r.height() // 2 - 9, 3, 18), 1.5, 1.5)

        align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        small = QFont(option.font)
        small.setPixelSize(12)
        title_font = QFont(option.font)
        title_font.setBold(playing)

        painter.setFont(small)
        painter.setPen(QColor(T["ACC_TEXT"] if playing else T["DISABLED"] if missing else T["FAINT"]))
        painter.drawText(QRect(r.left() + 14, r.top(), 32, r.height()), align,
                         "♪" if playing else (index.data(ROLE_NUM) or ""))

        star_font = QFont(option.font)
        star_font.setPixelSize(16)
        painter.setFont(star_font)
        if fav:
            painter.setPen(QColor(T["STAR_MISSING"] if missing else T["STAR"]))
            painter.drawText(self.star_rect(r), Qt.AlignmentFlag.AlignCenter, "★")
        elif hover or selected:
            painter.setPen(QColor(T["SEL_LINE"]))
            painter.drawText(self.star_rect(r), Qt.AlignmentFlag.AlignCenter, "☆")
        left = r.left() + 80

        right = r.right() - 12
        tag = "檔案遺失" if missing else (index.data(ROLE_SUB) or "")
        if tag:
            painter.setFont(small)
            fm = QFontMetrics(small)
            online = index.data(ROLE_DATA) is not None
            width = min(fm.horizontalAdvance(tag) + 2, max(80, int(r.width() * (0.42 if online else 0.32))))
            painter.setPen(QColor(T["TAG_MISSING"] if missing else T["FAINT"]))
            painter.drawText(QRect(right - width, r.top(), width, r.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             fm.elidedText(tag, Qt.TextElideMode.ElideRight if online else Qt.TextElideMode.ElideLeft, width))
            right -= width + 12

        painter.setFont(title_font)
        painter.setPen(QColor(T["TEXT_MISSING_SEL"] if missing and selected else T["DISABLED"] if missing
                              else T["ACC_TEXT"] if playing else T["STRONG"] if selected else T["TEXT2"]))
        title_rect = QRect(left, r.top(), max(10, right - left), r.height())
        painter.drawText(title_rect, align, QFontMetrics(title_font).elidedText(
            index.data(ROLE_NAME) or "", Qt.TextElideMode.ElideRight, title_rect.width()))
        painter.restore()


class ElideLabel(QLabel):
    """Single-line label that shortens long text with … instead of widening its parent."""

    def __init__(self, text=""):
        super().__init__()
        self._full = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._refit()

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._refit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refit()

    def _refit(self):
        QLabel.setText(self, self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(10, self.width())))


class SeekSlider(QSlider):
    """Slider that jumps to where you click (then keeps dragging if you hold)."""
    seek_requested = Signal(int)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.width() > 0:
            value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(event.position().x()), self.width())
            self.setValue(value)
            self.seek_requested.emit(value)
        super().mousePressEvent(event)


class TrackListWidget(QListWidget):
    """Song list whose ★ zone is clickable (a fast double-click never starts playback)."""
    star_clicked = Signal(object)
    order_changed = Signal()  # emitted after the user drags a row to a new position

    def __init__(self):
        super().__init__()
        self.setItemDelegate(TrackDelegate(self))
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)

    def _star_hit(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return None
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        if item is not None and TrackDelegate.star_rect(self.visualItemRect(item).adjusted(2, 1, -2, -1)).contains(pos):
            return item
        return None

    def mousePressEvent(self, event):
        item = self._star_hit(event)
        if item is not None:
            self.star_clicked.emit(item)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        item = self._star_hit(event)
        if item is not None:
            self.star_clicked.emit(item)
            return
        super().mouseDoubleClickEvent(event)

    def set_reorderable(self, enabled):
        """Turn drag-to-reorder on/off (on only for the unfiltered 最愛 list)."""
        mode = QListWidget.DragDropMode.InternalMove if enabled else QListWidget.DragDropMode.NoDragDrop
        self.setDragDropMode(mode)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragEnabled(enabled)
        self.setAcceptDrops(enabled)
        self.setDropIndicatorShown(enabled)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.order_changed.emit()


class VisualizerWidget(QWidget):
    """Soft spectrum display drawn in the active theme's accent colours (FFT of the playing song)."""
    BARS = 44
    N = 2048
    TILT_DB = 15.0          # high notes are naturally quieter; lift them so the right side moves too

    def __init__(self, source):
        super().__init__()
        self._source = source
        self.style_name = "bars"
        self._vals = np.zeros(self.BARS)
        self._peaks = np.zeros(self.BARS)
        self._win = np.hanning(self.N).astype(np.float32)
        freqs = np.fft.rfftfreq(self.N, 1.0 / 48000)
        edges = np.geomspace(45, 15000, self.BARS + 1)
        self._bands = []
        for i in range(self.BARS):
            a = int(np.searchsorted(freqs, edges[i]))
            b = max(int(np.searchsorted(freqs, edges[i + 1])), a + 1)
            self._bands.append((a, b))
        self._tilt = np.linspace(0, self.TILT_DB, self.BARS)
        self._idle = True
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self.setFixedHeight(92)
        self.setToolTip("播放中視覺化（可在「設定 → 外觀」更換樣式或關閉）")

    def set_style(self, name):
        self.style_name = name if name in ("bars", "mirror", "wave") else "bars"
        self.setVisible(name != "off")
        self.update()

    def showEvent(self, event):
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _tick(self):
        try:
            x = self._source()
        except Exception:
            return
        silent = float(np.max(np.abs(x))) < 1e-4
        if silent and self._idle and not self._vals.any():
            return                                           # nothing playing and nothing left to fade out
        if silent:
            norm = np.zeros(self.BARS)
        else:
            spec = np.abs(np.fft.rfft(x * self._win)) / (self.N / 4)
            level = np.array([spec[a:b].max() for a, b in self._bands])
            db = 20 * np.log10(level + 1e-7) + self._tilt
            norm = np.clip((db + 72.0) / 62.0, 0.0, 1.0)
            norm = np.convolve(np.pad(norm, 1, mode="edge"), [.2, .6, .2], mode="valid")
        v = self._vals
        self._vals = np.where(norm > v, v + (norm - v) * .6, v + (norm - v) * .14)
        self._vals[self._vals < .004] = 0.0
        self._peaks = np.maximum(self._peaks - .014, self._vals)
        self._idle = silent
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        base = QColor(T["BASE"])
        base.setAlpha(110)
        p.setPen(QPen(QColor(T["LINE"]), 1))
        p.setBrush(base)
        p.drawRoundedRect(QRectF(.5, .5, w - 1, h - 1), 14, 14)
        pad = 14
        area = QRectF(pad, 10, w - pad * 2, h - 20)
        n = self.BARS
        top_col, bot_col = QColor(T["ACC_BAR"]), QColor(T["ACC_LO"])
        cap_col = QColor(T["FOCUS_HI"])
        vals = self._vals
        if self.style_name == "wave":
            pts = [QPointF(area.left() + area.width() * i / (n - 1), area.bottom() - max(.03, vals[i]) * area.height())
                   for i in range(n)]
            path = QPainterPath(QPointF(area.left(), area.bottom()))
            path.lineTo(pts[0])
            for a, b in zip(pts, pts[1:]):
                mid = QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
                path.quadTo(a, mid)
            path.lineTo(pts[-1])
            path.lineTo(QPointF(area.right(), area.bottom()))
            path.closeSubpath()
            g = QLinearGradient(0, area.top(), 0, area.bottom())
            c1, c2 = QColor(top_col), QColor(bot_col)
            c1.setAlpha(190); c2.setAlpha(25)
            g.setColorAt(0, c1); g.setColorAt(1, c2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawPath(path)
            line = QPainterPath(pts[0])
            for a, b in zip(pts, pts[1:]):
                line.quadTo(a, QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(cap_col, 1.8))
            p.drawPath(line)
            return
        gap = 3.0
        bw = max(2.0, (area.width() - gap * (n - 1)) / n)
        mirror = self.style_name == "mirror"
        half = area.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(n):
            x = area.left() + i * (bw + gap)
            v = vals[i]
            if mirror:
                bh = max(2.0, v * half)
                rect = QRectF(x, area.center().y() - bh, bw, bh * 2)
                g = QLinearGradient(0, rect.top(), 0, rect.bottom())
                c_out, c_mid = QColor(top_col), QColor(bot_col)
                c_out.setAlpha(230 if v > 0 else 70); c_mid.setAlpha(120 if v > 0 else 40)
                g.setColorAt(0, c_out); g.setColorAt(.5, c_mid); g.setColorAt(1, c_out)
                p.setBrush(QBrush(g))
                p.drawRoundedRect(rect, bw / 2, bw / 2)
            else:
                bh = max(3.0, v * area.height())
                rect = QRectF(x, area.bottom() - bh, bw, bh)
                g = QLinearGradient(0, rect.top(), 0, area.bottom())
                c_top, c_bot = QColor(top_col), QColor(bot_col)
                if v <= 0:
                    c_top.setAlpha(80); c_bot.setAlpha(80)
                g.setColorAt(0, c_top); g.setColorAt(1, c_bot)
                p.setBrush(QBrush(g))
                p.drawRoundedRect(rect, min(bw / 2, 3), min(bw / 2, 3))
                pk = self._peaks[i]
                if pk > .06:
                    cap = QColor(cap_col)
                    cap.setAlpha(170)
                    p.setBrush(cap)
                    p.drawRoundedRect(QRectF(x, area.bottom() - pk * area.height() - 4, bw, 2.2), 1, 1)


class Signals(QObject):
    play_requested = Signal()
    stop_requested = Signal()
    random_requested = Signal()
    audio_error = Signal(str)
    mic_requested = Signal()
    next_requested = Signal()
    pause_requested = Signal()
    loop_requested = Signal()
    hold_requested = Signal()
    clicker_requested = Signal()
    volume_up_requested = Signal()
    volume_down_requested = Signal()
    scan_done = Signal(int, object, object, int)
    stream_ready = Signal(int, object, str)    # token, opened streamer (None on failure), error text
    radio_results = Signal(int, object, str)   # token, stations, error text
    yt_results = Signal(int, object, str)      # token, {autoplay, entries}, error text
    tool_done = Signal(str, str)               # which component, error text ("" = ok)


class EchoDeck(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = self.load_settings()
        self.tracks: list[Path] = []
        self.history: list[Path] = []
        self._shuffle_played: set = set()  # path keys already played in the current shuffle round
        self.view_mode = self.settings.last_view_mode if self.settings.last_view_mode in ("all", "fav") else "all"
        if self.settings.last_view_mode.startswith("pl:") and self._pl_of_view(self.settings.last_view_mode) is not None:
            self.view_mode = self.settings.last_view_mode
        self._restored_last_track = False  # guard so restore-on-launch only runs once
        self._track_info: dict = {}
        self._playing_key = None
        self._pause_ui_state = None
        self.current_source = None          # the radio station / YouTube video that is playing
        self._pending_src = None
        self._panels = []                   # now-playing panels of the online pages
        self._volume_mirrors = []
        self._radio_token = 0
        self._radio_loaded = False
        self._yt_token = 0
        self._yt_queue = []
        self._yt_tool_busy = False
        self._yt_dialog = None
        self._yt_jobs = {}                  # favourites that are downloading right now
        self._yt_pipe_only = False        # set once YouTube refuses direct links on this PC
        self._scan_token = 0
        self._scanning = False
        self.current_track: Path | None = None
        self.current_track_index = -1
        self.ptt_down = False
        self.ptt_uses_scan_code = False
        self.hotkeys = []
        self.signals = Signals()
        self.signals.play_requested.connect(self.play_selected)
        self.signals.stop_requested.connect(self.stop)
        self.signals.random_requested.connect(self.play_random)
        self.signals.audio_error.connect(self.show_audio_error)
        self.signals.scan_done.connect(self._on_scan_done)
        self.signals.stream_ready.connect(self._on_stream_ready)
        self.signals.radio_results.connect(self._on_radio_results)
        self.signals.yt_results.connect(self._on_yt_results)
        self.signals.tool_done.connect(self._on_tool_done)
        self.signals.mic_requested.connect(self.toggle_microphone)
        self.signals.next_requested.connect(self.play_next_in_order)
        self.signals.pause_requested.connect(self.toggle_pause)
        self.signals.loop_requested.connect(self.toggle_loop)
        self.signals.hold_requested.connect(self.toggle_hold)
        self.signals.clicker_requested.connect(self.toggle_clicker)
        self.signals.volume_up_requested.connect(lambda: self.adjust_volume(5))
        self.signals.volume_down_requested.connect(lambda: self.adjust_volume(-5))
        self.hotkey_edits = {}
        self.clicker = AutoClicker()
        self._clicker_pending = False
        self._clicker_token = 0
        self._clicker_deadline = QTimer(self)
        self._clicker_deadline.setSingleShot(True)
        self._clicker_deadline.timeout.connect(self.stop_clicker)
        self.clicker.target = self.settings.clicker_target
        self.clicker.interval = max(AutoClicker.MIN_INTERVAL, self.settings.clicker_interval)
        self.hold_action = HoldAction()
        self.hold_action.target = self.settings.hold_target
        self._hold_pending = False
        self._hold_token = 0
        self._capturing = False
        self._mouse_hotkey_down = {}
        self._mouse_hotkey_timer = QTimer(self)
        self._mouse_hotkey_timer.setInterval(15)
        self._mouse_hotkey_timer.timeout.connect(self._poll_mouse_hotkey)
        self._hold_deadline = QTimer(self)
        self._hold_deadline.setSingleShot(True)
        self._hold_deadline.timeout.connect(self.stop_hold)
        self._extra_viz = []                 # visualisers of the radio / YouTube pages
        self.mixer = AudioMixer(lambda message: self.signals.audio_error.emit(message))
        self.effect_was_playing = False
        self._starting = False
        self._play_token = 0
        self.effect_timer = QTimer(self)
        self.effect_timer.timeout.connect(self.poll_effect_state)
        self.effect_timer.start(150)

        self.setWindowTitle(f"{APP_NAME} — Game Voice Music  [{BUILD_TAG}]")
        self.setMinimumSize(1120, 720)
        self.setAcceptDrops(True)  # drop a folder onto the window to add it to the library
        self.resize(1280, 800)
        self.build_ui()
        shutil.rmtree(YT_TMP, ignore_errors=True)       # leftovers of an interrupted favourite download
        self.refresh_devices()
        self.apply_microphone()
        self.refresh_folder_list()
        self.scan_folders()
        self.install_hotkeys()

    # ---- layout helpers -------------------------------------------------
    def page_header(self, title, subtitle):
        col = QVBoxLayout()
        col.setSpacing(4)
        col.addWidget(QLabel(title, objectName="title"))
        sub = QLabel(subtitle)
        sub.setObjectName("subtitle")
        sub.setWordWrap(True)
        col.addWidget(sub)
        return col

    def page_shell(self, title, subtitle):
        """A scrollable page with the shared header. Returns (page, box)."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        area.setWidget(content)
        outer.addWidget(area)
        box = QVBoxLayout(content)
        box.setContentsMargins(10, 4, 14, 14)
        box.setSpacing(16)
        box.addLayout(self.page_header(title, subtitle))
        box.addSpacing(4)
        return page, box

    def section_card(self, title, desc=""):
        """A titled card with a grid inside. Returns (card, grid)."""
        card = QFrame()
        card.setObjectName("settingsCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(26, 22, 26, 24)
        v.setSpacing(6)
        v.addWidget(QLabel(title, objectName="sectionTitle"))
        if desc:
            note = QLabel(desc)
            note.setObjectName("hint")
            note.setWordWrap(True)
            v.addWidget(note)
        v.addSpacing(8)
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(14)
        grid.setColumnMinimumWidth(0, 150)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        return card, grid

    def slider_with_value(self, slider, suffix="%"):
        """Slider + live value badge in one widget; returns (widget, badge)."""
        wrap = QWidget()
        wrap.setObjectName("row")
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        badge = QLabel(f"{slider.value()}{suffix}")
        badge.setObjectName("valueBadge")
        badge.setMinimumWidth(58)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        slider.valueChanged.connect(lambda v, b=badge, s=suffix: b.setText(f"{v}{s}"))
        row.addWidget(slider, 1)
        row.addWidget(badge)
        return wrap, badge

    def _on_page_changed(self, index):
        if index == 5 and not self._radio_loaded:
            self._radio_loaded = True
            self.radio_reload()           # first visit: fetch stations (no network at startup)

    def _sync_nav(self, index):
        button = self.nav_buttons.get(index)
        if button is not None:
            button.setChecked(True)

    def build_ui(self):
        root = Backdrop()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(18)

        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(236)
        n = QVBoxLayout(nav)
        n.setContentsMargins(16, 20, 16, 16)
        n.setSpacing(4)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(6, 0, 0, 0)
        brand_row.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(app_icon_pixmap(44))
        logo.setFixedSize(44, 44)
        brand_col = QVBoxLayout()
        brand_col.setSpacing(0)
        brand_col.addWidget(QLabel("EchoDeck", objectName="brand"))
        brand_col.addWidget(QLabel("Game Voice Music", objectName="brandSub"))
        brand_row.addWidget(logo)
        brand_row.addLayout(brand_col, 1)
        n.addLayout(brand_row)
        n.addSpacing(24)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.nav_buttons = {}
        self.nav_kinds = {}
        for label, kind, page in [("音樂庫", "music", 0), ("網路電台", "radio", 5), ("YouTube", "play", 6),
                                  ("快捷音效板", "keys", 1), ("連點器", "mouse", 4), ("診斷", "pulse", 2),
                                  ("設定", "gear", 3)]:
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setIcon(nav_icon(kind))
            button.setIconSize(QSize(22, 22))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            group.addButton(button)
            button.clicked.connect(lambda _, p=page: self.pages.setCurrentIndex(p))
            self.nav_buttons[page] = button
            self.nav_kinds[page] = kind
            n.addWidget(button)
        n.addStretch()
        note = QLabel("音樂與 Mic 共用輸出\nMic 預設完全關閉")
        note.setObjectName("privacyNote")
        n.addWidget(note)
        layout.addWidget(nav)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.library_page())
        self.pages.addWidget(self.quick_page())
        self.pages.addWidget(self.diagnostics_page())
        self.pages.addWidget(self.settings_page())
        self.pages.addWidget(self.clicker_page())
        self.pages.addWidget(self.radio_page())       # index 5
        self.pages.addWidget(self.youtube_page())     # index 6
        self.pages.currentChanged.connect(self._sync_nav)
        self.pages.currentChanged.connect(self._on_page_changed)
        self._diag_timer = QTimer(self)
        self._diag_timer.setInterval(500)
        self._diag_timer.timeout.connect(self.refresh_diagnostics)
        self.pages.currentChanged.connect(lambda i: self._diag_timer.start() if i == 2 else self._diag_timer.stop())
        self.nav_buttons[0].setChecked(True)
        layout.addWidget(self.pages, 1)

    def library_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 4, 10, 4)
        outer.setSpacing(12)
        header = QHBoxLayout()
        title_wrap = QVBoxLayout()
        title_wrap.setSpacing(4)
        title_wrap.addWidget(QLabel("你的音樂庫", objectName="title"))
        self.subtitle = QLabel("選一首歌開始；EchoDeck 會自動替你開啟遊戲區域發話。")
        self.subtitle.setObjectName("subtitle")
        title_wrap.addWidget(self.subtitle)
        header.addLayout(title_wrap)
        header.addStretch()
        self.folder_button = QPushButton("📁  加入音樂資料夾")
        self.folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_button.clicked.connect(self.add_folder)
        header.addWidget(self.folder_button, 0, Qt.AlignmentFlag.AlignTop)
        self.folder_manage_button = QPushButton("🗂️  管理資料夾")
        self.folder_manage_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_manage_button.setToolTip("查看目前已加入的資料夾，可直接在這裡移除")
        self.folder_manage_button.clicked.connect(self.show_folder_manage_menu)
        header.addWidget(self.folder_manage_button, 0, Qt.AlignmentFlag.AlignTop)
        self.status = QLabel("● 待命中")
        self.status.setObjectName("statusIdle")
        header.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)
        self.hotkey_note = QLabel()
        self.hotkey_note.setObjectName("tipBar")
        self.hotkey_note.setWordWrap(True)
        outer.addWidget(self.hotkey_note)
        self.refresh_hotkey_hint()
        self.meter = QLabel("")
        self.meter.setObjectName("hint")
        outer.addWidget(self.meter)
        tools = QHBoxLayout()
        tools.setSpacing(12)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.seg_all = QPushButton("全部")
        self.seg_all.setObjectName("segL")
        self.seg_fav = QPushButton("★  最愛")
        self.seg_fav.setObjectName("segM")
        self.seg_list = QPushButton("📋  我的清單  ▾")
        self.seg_list.setObjectName("segR")
        self.seg_list.setToolTip("自訂清單：自己命名、自己排順序（點開可新增、切換、改名、刪除）")
        seg_group = QButtonGroup(self)
        seg_group.setExclusive(True)
        for button, mode in ((self.seg_all, "all"), (self.seg_fav, "fav"), (self.seg_list, "list")):
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            seg_group.addButton(button)
            if mode == "list":
                button.clicked.connect(self.show_playlist_menu)
            else:
                button.clicked.connect(lambda _=False, m=mode: self.set_view_mode(m))
            seg.addWidget(button)
        if self.view_mode == "fav":
            self.seg_fav.setChecked(True)
        elif self._pl_of_view() is not None:
            self.seg_list.setChecked(True)
        else:
            self.seg_all.setChecked(True)
        tools.addLayout(seg)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("🔍  搜尋歌曲或資料夾…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.filter_tracks)
        tools.addWidget(self.search_box, 1)
        self.autoplay_combo = QComboBox()
        self.autoplay_combo.addItem("播完後：停止", "off")
        self.autoplay_combo.addItem("播完後：隨機播放", "random")
        self.autoplay_combo.addItem("播完後：依順序播放", "sequential")
        self.autoplay_combo.addItem("單曲循環播放", "loop")
        saved_mode = self.autoplay_combo.findData(self.settings.autoplay_mode)
        self.autoplay_combo.setCurrentIndex(max(0, saved_mode))
        self.autoplay_combo.currentIndexChanged.connect(self.autoplay_changed)
        self.autoplay_combo.setMinimumWidth(190)
        tools.addWidget(self.autoplay_combo)
        self.loop_button = QPushButton("🔁  單曲循環")
        self.loop_button.setCheckable(True)
        self.loop_button.setChecked(self.settings.autoplay_mode == "loop")
        self.loop_button.setToolTip("開啟後，目前歌曲播完會自動從頭重播（快捷鍵也可切換）")
        self.loop_button.toggled.connect(self.loop_button_toggled)
        self._set_loop_button_style()
        tools.addWidget(self.loop_button)
        outer.addLayout(tools)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.track_list = TrackListWidget()
        self.track_list.setObjectName("trackList")
        self.track_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.track_list.customContextMenuRequested.connect(self.library_menu)
        self.track_list.itemDoubleClicked.connect(lambda _: self.play_selected())
        self.track_list.currentItemChanged.connect(self.update_selection_details)
        self.track_list.star_clicked.connect(lambda item: self.toggle_favorite(Path(item.data(ROLE_PATH))))
        self.track_list.order_changed.connect(self.fav_order_changed)
        self.list_empty = QLabel()
        self.list_empty.setObjectName("emptyState")
        self.list_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_empty.setWordWrap(True)
        self.list_stack = QStackedWidget()
        self.list_stack.setObjectName("row")
        self.list_stack.addWidget(self.track_list)
        self.list_stack.addWidget(self.list_empty)
        self.list_stack.setMinimumWidth(320)
        body.addWidget(self.list_stack, 1)

        panel = QFrame()
        panel.setObjectName("nowPanel")
        panel.setMinimumWidth(380)
        p = QVBoxLayout(panel)
        p.setContentsMargins(18, 16, 18, 16)
        p.setSpacing(8)

        # ---- pinned now-playing card: title, seekable progress, volume ----
        card = QFrame()
        card.setObjectName("nowCard")
        k = QVBoxLayout(card)
        k.setContentsMargins(20, 12, 20, 14)
        k.setSpacing(4)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        top = QHBoxLayout()
        top.addWidget(QLabel("♪  正在播放", objectName="eyebrowAccent"))
        top.addStretch()
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("hint")
        top.addWidget(self.time_label)
        k.addLayout(top)
        self.card_title = ElideLabel("尚未播放")
        self.card_title.setObjectName("cardTitle")
        k.addWidget(self.card_title)
        self.card_sub = ElideLabel("")
        self.card_sub.setObjectName("muted")
        k.addWidget(self.card_sub)
        k.addSpacing(2)
        self.progress = SeekSlider(Qt.Orientation.Horizontal)
        self.progress.setObjectName("cardSeek")
        self.progress.setRange(0, 1000)
        self.progress.setFixedHeight(20)
        self.progress.setCursor(Qt.CursorShape.PointingHandCursor)
        self.progress.setToolTip("點一下或拖曳可跳到指定位置")
        self.progress.seek_requested.connect(lambda v: self.mixer.seek(v / 10))
        self.progress.sliderReleased.connect(lambda: self.mixer.seek(self.progress.value() / 10))
        k.addWidget(self.progress)
        p.addWidget(card)
        self.viz = VisualizerWidget(lambda: self.mixer.viz_samples())
        self.viz.set_style(self.settings.viz_style)
        p.addWidget(self.viz)

        # ---- volume (below the card) ----
        vol_row = QHBoxLayout()
        vol_row.setSpacing(10)
        vol_row.addWidget(QLabel("🔊"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 200)
        self.volume.setValue(self.settings.music_volume)
        self.volume.setToolTip("音效音量（0–200%）")
        self.volume.valueChanged.connect(self.change_volume)
        vol_widget, self.volume_badge = self.slider_with_value(self.volume)
        vol_row.addWidget(vol_widget, 1)
        vol_row.setContentsMargins(4, 0, 4, 0)
        p.addLayout(vol_row)

        # ---- selected song ----
        sel_box = QVBoxLayout()
        sel_box.setSpacing(2)
        sel_box.addWidget(QLabel("目前選取", objectName="eyebrow"))
        self.now_title = ElideLabel("尚未選擇音樂")
        self.now_title.setObjectName("nowTitle")
        sel_box.addWidget(self.now_title)
        self.now_path = ElideLabel("請先在設定選取音樂資料夾")
        self.now_path.setObjectName("nowPath")
        sel_box.addWidget(self.now_path)
        p.addLayout(sel_box)
        p.addStretch(1)

        # ---- transport ----
        self.play_button = QPushButton("▶  預覽播放（桌面）")
        self.play_button.setObjectName("primary")
        self.play_button.setFixedHeight(44)
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.clicked.connect(self.play_selected)
        p.addWidget(self.play_button)
        pair = QHBoxLayout()
        pair.setSpacing(12)
        self.random_button = QPushButton("⤨  隨機一首")
        self.random_button.setFixedHeight(38)
        self.random_button.setToolTip("從目前清單（全部／最愛）隨機挑一首播放")
        self.random_button.clicked.connect(self.play_random)
        pair.addWidget(self.random_button)
        self.next_button = QPushButton("⏭  下一首")
        self.next_button.setFixedHeight(38)
        self.next_button.setToolTip("依目前清單（全部／最愛）順序播放下一首")
        self.next_button.clicked.connect(self.play_next_in_order)
        pair.addWidget(self.next_button)
        p.addLayout(pair)
        stop_row = QHBoxLayout()
        stop_row.setSpacing(12)
        self.pause_button = QPushButton("⏸  暫停")
        self.pause_button.setObjectName("pauseBtn")
        self.pause_button.setFixedHeight(40)
        self.pause_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_button.setToolTip("暫停／繼續目前播放（快捷鍵也可使用）")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        stop_row.addWidget(self.pause_button, 1)
        self.stop_button = QPushButton("■  停止並關閉發話")
        self.stop_button.setObjectName("danger")
        self.stop_button.setFixedHeight(40)
        self.stop_button.setToolTip("停止播放並放開遊戲發話鍵")
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.clicked.connect(lambda: self.stop())
        stop_row.addWidget(self.stop_button, 1)
        p.addLayout(stop_row)
        body.addWidget(panel, 1)
        outer.addLayout(body, 1)
        return page

    # ---- track list helpers ----
    def _apply_playing_mark(self):
        for row in range(self.track_list.count()):
            item = self.track_list.item(row)
            item.setData(ROLE_PLAYING, bool(self._playing_key) and path_key(item.data(ROLE_PATH)) == self._playing_key)
        self.track_list.viewport().update()

    def mark_playing(self, track):
        """Show a ♪ marker on the track that is currently playing."""
        self._playing_key = path_key(track) if track is not None else None
        if not hasattr(self, "track_list"):
            return
        self._apply_playing_mark()
        if track is not None:
            self.set_now(Path(track).stem, Path(track).parent.name or str(Path(track).parent))
        else:
            self.set_now("尚未播放", "")
        if track is not None:
            for row in range(self.track_list.count()):
                item = self.track_list.item(row)
                if item.data(ROLE_PLAYING):
                    self.track_list.scrollToItem(item)
                    break

    def _select_in_list(self, track):
        key = path_key(track)
        for row in range(self.track_list.count()):
            if path_key(self.track_list.item(row).data(ROLE_PATH)) == key:
                self.track_list.setCurrentRow(row)
                return

    def _flag_missing(self, track):
        key = path_key(track)
        for row in range(self.track_list.count()):
            item = self.track_list.item(row)
            if path_key(item.data(ROLE_PATH)) == key:
                item.setData(ROLE_MISSING, True)
        self.track_list.viewport().update()
        self.subtitle.setText(f"找不到檔案：{Path(track).name}（已跳過）")

    # ---- library: scan / view / favourites ----
    def scan_folders(self):
        """Scan all folders on a worker thread so the window never freezes."""
        self._scan_token += 1
        token = self._scan_token
        folders = list(self.settings.music_folders)
        self._scanning = True
        if folders:
            self.subtitle.setText("正在掃描音樂資料夾…（可以先使用其他功能）")
        if self.track_list.count() == 0:
            self.rebuild_track_list()
        threading.Thread(target=self._scan_worker, args=(token, folders), daemon=True).start()

    def _scan_worker(self, token, folders):
        found, info, live = {}, {}, 0
        try:
            for base in (Path(f) for f in folders):
                if not base.is_dir():
                    continue
                live += 1
                try:
                    for p in base.rglob("*"):
                        if not (p.is_file() and p.suffix.lower() in FORMATS):
                            continue
                        key = path_key(p)
                        if key in found:
                            continue
                        found[key] = p
                        rel = p.relative_to(base)
                        root_name = base.name or str(base)
                        sub = str(rel.parent) if rel.parent != Path(".") else root_name
                        info[key] = (sub, f"{rel} {root_name}".lower())
                except OSError:
                    continue
        finally:
            tracks = sorted(found.values(), key=lambda p: p.name.lower())
            self.signals.scan_done.emit(token, tracks, info, live)

    def _on_scan_done(self, token, tracks, info, live):
        if token != self._scan_token:
            return  # a newer scan was started; ignore this stale result
        self._scanning = False
        self.tracks = tracks
        self._track_info = info
        self.rebuild_track_list()
        if not self._restored_last_track:
            self._restored_last_track = True
            self._restore_last_selection(self._last_track_for_view())
        if self.settings.music_folders:
            self.subtitle.setText(
                f"已找到 {len(tracks)} 首音樂（{live} 個資料夾）。雙擊播放；右鍵可加入最愛。")

    # ---- custom playlists (自訂清單) ----
    def _pl_of_view(self, mode=None):
        mode = mode or self.view_mode
        if isinstance(mode, str) and mode.startswith("pl:"):
            return next((pl for pl in self.settings.playlists if pl["id"] == mode[3:]), None)
        return None

    def _playlist_by_id(self, pid):
        return next((pl for pl in self.settings.playlists if pl["id"] == pid), None)

    def _next_playlist_name(self):
        used = {pl["name"] for pl in self.settings.playlists}
        n = 1
        while f"清單{n}" in used:
            n += 1
        return f"清單{n}"

    def _ask_playlist_name(self, title, default):
        """Ask for a playlist name; returns a clean unique name or '' when cancelled/invalid."""
        while True:
            text, ok = QInputDialog.getText(self, title, "清單名稱：", QLineEdit.EchoMode.Normal, default)
            name = text.strip()[:40]
            if not ok:
                return ""
            if not name:
                QMessageBox.information(self, APP_NAME, "名稱不能是空的。")
                continue
            if any(pl["name"] == name and pl["name"] != default for pl in self.settings.playlists):
                QMessageBox.information(self, APP_NAME, f"已經有一份叫「{name}」的清單了，請換個名字。")
                default = name
                continue
            return name

    def create_playlist(self, name=None, tracks=None):
        name = name or self._ask_playlist_name("新增清單", self._next_playlist_name())
        if not name:
            return None
        pl = {"id": uuid.uuid4().hex[:12], "name": name, "tracks": [str(t) for t in (tracks or [])]}
        self.settings.playlists.append(pl)
        self.save_settings()
        return pl

    def show_playlist_menu(self):
        menu = QMenu(self)
        current = self._pl_of_view()
        for pl in self.settings.playlists:
            act = menu.addAction(f"{'●' if current is pl else '○'}  {pl['name']}　（{len(pl['tracks'])} 首）")
            act.setData(("open", pl["id"]))
        if self.settings.playlists:
            menu.addSeparator()
        a_new = menu.addAction("＋  新增清單…")
        a_new.setData(("new", ""))
        if current is not None:
            a_rename = menu.addAction(f"✎  重新命名「{current['name']}」…")
            a_rename.setData(("rename", current["id"]))
            a_delete = menu.addAction(f"🗑  刪除「{current['name']}」")
            a_delete.setData(("delete", current["id"]))
        chosen = menu.exec(self.seg_list.mapToGlobal(QPoint(0, self.seg_list.height() + 4)))
        data = chosen.data() if chosen is not None else None
        if data:
            kind, pid = data
            if kind == "open":
                self.set_view_mode(f"pl:{pid}")
            elif kind == "new":
                pl = self.create_playlist()
                if pl:
                    self.set_view_mode(f"pl:{pl['id']}")
                    self.subtitle.setText(f"已建立「{pl['name']}」。在歌曲上按右鍵 →「加入清單」就能放進來。")
            elif kind == "rename":
                pl = self._playlist_by_id(pid)
                name = self._ask_playlist_name("重新命名清單", pl["name"]) if pl else ""
                if name:
                    pl["name"] = name
                    self.save_settings()
        if data and data[0] == "delete":
            pl = self._playlist_by_id(data[1])
            if pl and QMessageBox.question(
                    self, APP_NAME, f"要刪除清單「{pl['name']}」嗎？\n（只會刪除清單，歌曲檔案不會動）",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                self.settings.playlists = [p for p in self.settings.playlists if p["id"] != pl["id"]]
                self.settings.last_pl_tracks.pop(pl["id"], None)
                self.save_settings()
                if self.view_mode == f"pl:{pl['id']}":
                    self.set_view_mode("all")
        self.rebuild_track_list()          # refreshes counts, names and the highlighted tab

    def playlist_add(self, pid, track):
        pl = self._playlist_by_id(pid)
        if pl is None:
            return
        key = path_key(track)
        if any(path_key(t) == key for t in pl["tracks"]):
            pl["tracks"] = [t for t in pl["tracks"] if path_key(t) != key]
            self.subtitle.setText(f"已從「{pl['name']}」移除：{Path(track).stem}")
        else:
            pl["tracks"].append(str(track))
            self.subtitle.setText(f"已加入「{pl['name']}」：{Path(track).stem}")
        self.save_settings()
        if self.view_mode == f"pl:{pid}":
            self.rebuild_track_list()
        else:
            self._sync_seg()

    def _sync_seg(self):
        pl = self._pl_of_view()
        self.seg_list.setText(f"📋  {pl['name']}  ▾" if pl else "📋  我的清單  ▾")
        target = self.seg_fav if self.view_mode == "fav" else self.seg_list if pl else self.seg_all
        target.setChecked(True)

    def _last_track_for_view(self):
        """Remembered song of the tab that is currently open (全部 / 最愛 / 某份清單)."""
        pl = self._pl_of_view()
        if pl is not None:
            return self.settings.last_pl_tracks.get(pl["id"], "")
        return self.settings.last_fav_track if self.view_mode == "fav" else self.settings.last_track

    def _remember_last_track(self, path):
        """Store `path` as the last song of the tab that is currently open."""
        pl = self._pl_of_view()
        if pl is not None:
            self.settings.last_pl_tracks[pl["id"]] = str(path)
        elif self.view_mode == "fav":
            self.settings.last_fav_track = str(path)
        else:
            self.settings.last_track = str(path)

    def _restore_last_selection(self, last):
        """Select `last` in the visible list if it is there (no auto-play)."""
        if not last:
            return
        key = path_key(last)
        for row in range(self.track_list.count()):
            if path_key(self.track_list.item(row).data(ROLE_PATH)) == key:
                self.track_list.setCurrentRow(row)
                return

    def set_view_mode(self, mode):
        # Read this tab's remembered song BEFORE rebuilding: the rebuild can
        # fire a selection change that would overwrite it.
        self.view_mode = mode
        remembered = self._last_track_for_view()
        self.settings.last_view_mode = mode
        self.save_settings()
        self.rebuild_track_list()
        self._restore_last_selection(remembered)
        if (mode == "fav" or self._pl_of_view() is not None) and self.track_list.count() > 1:
            self.subtitle.setText("拖曳歌曲可調整順序，「下一首」會照這個順序播放。")

    def rebuild_track_list(self):
        lst = self.track_list
        scroll = lst.verticalScrollBar().value()
        current = lst.currentItem()
        current_key = path_key(current.data(ROLE_PATH)) if current is not None else None
        favs = [Path(p) for p in self.settings.favorites]
        fav_keys = {path_key(p) for p in favs}
        if self.view_mode.startswith("pl:") and self._pl_of_view() is None:
            self.view_mode = "all"                  # that playlist no longer exists
        pl = self._pl_of_view()
        fav_view = self.view_mode == "fav"
        saved_view = fav_view or pl is not None     # lists made of remembered paths (最愛 / 清單)
        entries = favs if fav_view else [Path(p) for p in pl["tracks"]] if pl is not None else self.tracks
        lst.blockSignals(True)
        lst.clear()
        width = max(2, len(str(len(entries))))
        restore = None
        for index, track in enumerate(entries, 1):
            key = path_key(track)
            sub, hay = self._track_info.get(
                key, (track.parent.name or str(track.parent), f"{track.parent.name} {track.name}".lower()))
            missing = saved_view and not track.is_file()
            item = QListWidgetItem()
            item.setData(ROLE_PATH, str(track))
            item.setData(ROLE_NAME, track.stem)
            item.setData(ROLE_SEARCH, hay)
            item.setData(ROLE_NUM, f"{index:0{width}d}")
            item.setData(ROLE_FAV, key in fav_keys)
            item.setData(ROLE_MISSING, missing)
            item.setData(ROLE_SUB, sub)
            item.setToolTip(str(track) + ("\n（檔案已被移動、改名或刪除）" if missing else ""))
            lst.addItem(item)
            if key == current_key:
                restore = item
        lst.blockSignals(False)
        if restore is not None:
            lst.setCurrentItem(restore)
        lst.verticalScrollBar().setValue(scroll)
        self._apply_playing_mark()
        self.filter_tracks(self.search_box.text())
        self.seg_all.setText(f"全部  {len(self.tracks)}")
        self.seg_fav.setText(f"★  最愛  {len(favs)}")
        self._sync_seg()
        if lst.count() == 0:
            if pl is not None:
                self.list_empty.setText(f"「{pl['name']}」還是空的\n\n在歌曲上按右鍵 →「加入清單」就能放進來")
            elif self._scanning and not fav_view:
                self.list_empty.setText("正在掃描音樂資料夾…\n\n歌曲很多時會花一點時間，期間其他功能都能正常使用")
            elif fav_view:
                self.list_empty.setText("還沒有最愛歌曲\n\n在「全部」清單的歌曲上按右鍵 →「加入最愛」\n或直接點歌名前面的 ☆")
            elif not self.settings.music_folders:
                self.list_empty.setText("尚未加入音樂資料夾\n\n點右上角「加入音樂資料夾」開始\n可以加入多個資料夾，會合併成同一個曲庫")
            else:
                self.list_empty.setText("資料夾裡沒有找到支援的音樂檔\n\n支援 mp3、wav、flac、ogg、m4a、aac、wma、opus")
        self.list_stack.setCurrentIndex(1 if lst.count() == 0 else 0)

    def is_favorite(self, path):
        key = path_key(path)
        return any(path_key(p) == key for p in self.settings.favorites)

    def toggle_favorite(self, track):
        track = Path(track)
        key = path_key(track)
        favs = self.settings.favorites
        index = next((i for i, p in enumerate(favs) if path_key(p) == key), None)
        if index is None:
            favs.append(str(track))
            self.subtitle.setText(f"★ 已加入最愛：{track.stem}")
        else:
            favs.pop(index)
            self.subtitle.setText(f"已從最愛移除：{track.stem}")
        self.save_settings()
        fav_keys = {path_key(p) for p in favs}
        for row in range(self.track_list.count()):
            item = self.track_list.item(row)
            item.setData(ROLE_FAV, path_key(item.data(ROLE_PATH)) in fav_keys)
        self.track_list.viewport().update()
        if self.view_mode == "fav":
            self.rebuild_track_list()
        else:
            self.seg_fav.setText(f"★  最愛  {len(favs)}")

    def library_menu(self, pos):
        item = self.track_list.itemAt(pos)
        if item is None:
            return
        self.track_list.setCurrentItem(item)
        track = Path(item.data(ROLE_PATH))
        menu = QMenu(self)
        a_fav = menu.addAction("☆  從最愛移除" if self.is_favorite(track) else "★  加入最愛")
        sub = menu.addMenu("📋  加入清單")
        key = path_key(track)
        for pl in self.settings.playlists:
            has = any(path_key(t) == key for t in pl["tracks"])
            act = sub.addAction(f"{'✓  ' if has else '      '}{pl['name']}")
            act.setData(pl["id"])
        if self.settings.playlists:
            sub.addSeparator()
        a_new_pl = sub.addAction("＋  新增清單並加入…")
        a_remove_here = None
        current_pl = self._pl_of_view()
        if current_pl is not None:
            a_remove_here = menu.addAction(f"✖  從「{current_pl['name']}」移除")
        menu.addSeparator()
        a_play = menu.addAction("▶  立即播放")
        a_play.setEnabled(not item.data(ROLE_MISSING))
        chosen = menu.exec(self.track_list.viewport().mapToGlobal(pos))
        if chosen is a_fav:
            self.toggle_favorite(track)
        elif chosen is a_new_pl:
            pl = self.create_playlist(tracks=[str(track)])
            if pl:
                self.subtitle.setText(f"已建立「{pl['name']}」並加入：{track.stem}")
                self._sync_seg()
        elif a_remove_here is not None and chosen is a_remove_here:
            self.playlist_add(current_pl["id"], track)
        elif chosen is not None and chosen.data() and chosen.parent() is sub:
            self.playlist_add(chosen.data(), track)
        elif chosen is a_play:
            self.play_track(track)

    def _sync_pause_ui(self, playing):
        paused = bool(playing and self.mixer.paused)
        state = (bool(playing), paused)
        if state == self._pause_ui_state:
            return
        self._pause_ui_state = state
        self.progress.setProperty("paused", paused)
        restyle(self.progress)
        self.pause_button.setEnabled(bool(playing))
        self.pause_button.setText("▶  繼續" if paused else "⏸  暫停")
        self.pause_button.setProperty("resume", paused)
        restyle(self.pause_button)

    def quick_page(self):
        page, box = self.page_shell(
            "快捷音效板",
            "點一下按鈕 → 顯示「請按下按鍵…」→ 直接按下想用的鍵或組合鍵（會即時顯示）→ 放開後自動記錄。")
        rows = {attr: (title, hint) for attr, title, hint in HOTKEY_ROWS}
        groups = [
            ("播放控制", ["play_hotkey", "random_hotkey", "next_hotkey", "stop_hotkey", "pause_hotkey"]),
            ("音量與麥克風", ["volume_up_hotkey", "volume_down_hotkey", "mic_hotkey"]),
        ]
        used = {a for _, attrs in groups for a in attrs}
        groups[-1][1].extend(a for a in rows if a not in used)  # never drop a row
        for group_title, attrs in groups:
            card, grid = self.section_card(group_title)
            for r, attr in enumerate(attrs):
                title, hint = rows[attr]
                grid.addWidget(QLabel(title, objectName="settingLabel"), r, 0)
                grid.addWidget(self.make_hotkey_row(attr), r, 1)
                grid.addWidget(QLabel(hint, objectName="hint"), r, 2)
            box.addWidget(card)
        box.addStretch()
        return page

    def make_hotkey_row(self, attr):
        edit = HotkeyEdit(getattr(self.settings, attr))
        self.hotkey_edits[attr] = edit
        edit.changed.connect(lambda value, a=attr: self.hotkey_edit_changed(a, value))
        edit.capture_state.connect(self.on_capture_state)
        clear = QPushButton("清除")
        clear.setFixedWidth(70)
        clear.clicked.connect(edit.clear_value)
        wrap = QWidget()
        wrap.setObjectName("row")
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(edit, 1)
        row.addWidget(clear)
        return wrap

    def _field_row(self, grid, row, label, widget, hint=""):
        """Label + widget on one line, with an optional one-line hint right under the widget."""
        # Key-capture buttons carry long prompts ("請按下…"); let them shrink instead
        # of forcing the two side-by-side cards wider than the window.
        for button in ([widget] if isinstance(widget, QPushButton) else []) + widget.findChildren(QPushButton):
            button.setSizePolicy(QSizePolicy.Policy.Ignored, button.sizePolicy().verticalPolicy())
        grid.addWidget(QLabel(label, objectName="settingLabel"), row * 2, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(widget, row * 2, 1)
        if hint:
            note = QLabel(hint)
            note.setObjectName("hint")
            note.setWordWrap(True)
            note.setContentsMargins(0, 0, 0, 6)
            grid.addWidget(note, row * 2 + 1, 1)

    def _mouse_hotkey_row(self, edit):
        clear = QPushButton("清除")
        clear.setFixedWidth(70)
        clear.clicked.connect(edit.clear_value)
        wrap = QWidget()
        wrap.setObjectName("row")
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(edit, 1)
        row.addWidget(clear)
        return wrap

    def _compact_card(self, title):
        card, grid = self.section_card(title)
        grid.setColumnMinimumWidth(0, 104)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)
        return card, grid

    def clicker_page(self):
        page, box = self.page_shell(
            "連點器",
            "連點與壓住並排設定；上方的預設組合可一次切換兩邊的設定。")

        # ---- presets (one preset = click settings + hold settings) ----
        presets_card, pgrid = self.section_card(
            "預設組合", "一組預設同時記住「連點」和「壓住」的按鍵、時間與開始前等待；快捷鍵不包含在內。")
        preset_row = QHBoxLayout()
        preset_row.setSpacing(10)
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(320)
        self.preset_combo.activated.connect(self.apply_clicker_preset)  # only user picks, not code changes
        save_preset = QPushButton("＋  存成預設")
        save_preset.setToolTip("把目前的連點與壓住設定存成一組預設（名稱相同會覆蓋舊的）")
        save_preset.clicked.connect(self.save_clicker_preset)
        self.delete_preset_button = QPushButton("－  刪除所選")
        self.delete_preset_button.clicked.connect(self.delete_clicker_preset)
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(save_preset)
        preset_row.addWidget(self.delete_preset_button)
        pgrid.addWidget(QLabel("套用預設", objectName="settingLabel"), 0, 0)
        pgrid.addLayout(preset_row, 0, 1)
        self.refresh_clicker_presets()
        box.addWidget(presets_card)

        columns = QHBoxLayout()
        columns.setSpacing(16)

        # ---- left: auto click ----
        card, grid = self._compact_card("連點")
        self.click_target = ClickTargetEdit(self.settings.clicker_target)
        self.click_target.changed.connect(self.clicker_target_changed)
        self.click_target.capture_state.connect(self.on_capture_state)
        self._field_row(grid, 0, "連點按鍵", self.click_target, "點一下再按想連點的鍵；滑鼠鍵需在本視窗內點選")
        self.click_interval = QDoubleSpinBox()
        self.click_interval.setRange(AutoClicker.MIN_INTERVAL, 3600.0)
        self.click_interval.setDecimals(2)
        self.click_interval.setSingleStep(0.1)
        self.click_interval.setSuffix(" 秒")
        self.click_interval.setValue(max(AutoClicker.MIN_INTERVAL, self.settings.clicker_interval))
        self.click_interval.valueChanged.connect(self.clicker_interval_changed)
        self._field_row(grid, 1, "連點間隔", self.click_interval, "最低 0.02 秒（每秒 50 次）")
        self.click_duration = QSpinBox()
        self.click_duration.setRange(0, 86400)
        self.click_duration.setSpecialValueText("不限時")
        self.click_duration.setSuffix(" 秒")
        self.click_duration.setValue(max(0, int(self.settings.clicker_duration)))
        self.click_duration.valueChanged.connect(self.clicker_duration_changed)
        self._field_row(grid, 2, "時間上限", self.click_duration, "滿這段時間自動停止；0 為不限時")
        self.click_delay = QDoubleSpinBox()
        self.click_delay.setRange(0.5, 3.0)
        self.click_delay.setDecimals(1)
        self.click_delay.setSingleStep(0.5)
        self.click_delay.setSuffix(" 秒")
        self.click_delay.setValue(min(3.0, max(0.5, float(self.settings.clicker_delay))))
        self.click_delay.valueChanged.connect(self.clicker_delay_changed)
        self._field_row(grid, 3, "開始前等待", self.click_delay, "按下開始後先等一下（0.5～3 秒），好移到目標位置")
        self._field_row(grid, 4, "快捷鍵（鍵盤）", self.make_hotkey_row("clicker_hotkey"),
                        "按一次開始、再按一次結束；可含組合鍵")
        self.click_mouse_edit = MouseHotkeyEdit(self.settings.clicker_mouse_hotkey)
        self.click_mouse_edit.changed.connect(self.clicker_mouse_hotkey_changed)
        self.click_mouse_edit.capture_state.connect(self.on_capture_state)
        self._field_row(grid, 5, "快捷鍵（滑鼠）", self._mouse_hotkey_row(self.click_mouse_edit),
                        "可用滑鼠中鍵或側鍵（遊戲內也有效）")
        self.click_button = QPushButton()
        self.click_button.setObjectName("primary")
        self.click_button.setFixedHeight(42)
        self.click_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.click_button.clicked.connect(self.clicker_button_clicked)
        grid.addWidget(self.click_button, 12, 0, 1, 2)
        self.click_status = QLabel()
        self.click_status.setObjectName("hint")
        self.click_status.setWordWrap(True)
        grid.addWidget(self.click_status, 13, 0, 1, 2)
        columns.addWidget(card, 1, Qt.AlignmentFlag.AlignTop)

        # ---- right: hold key ----
        card, grid = self._compact_card("壓住按鍵（長按）")
        self.hold_target_edit = ClickTargetEdit(self.settings.hold_target)
        self.hold_target_edit.changed.connect(self.hold_target_changed)
        self.hold_target_edit.capture_state.connect(self.on_capture_state)
        self._field_row(grid, 0, "壓住按鍵", self.hold_target_edit, "壓住不放（不連點）；滑鼠鍵需在本視窗內點選")
        self.hold_duration = QSpinBox()
        self.hold_duration.setRange(0, 86400)
        self.hold_duration.setSpecialValueText("不限時（手動停止）")
        self.hold_duration.setSuffix(" 秒")
        self.hold_duration.setValue(max(0, int(self.settings.hold_duration)))
        self.hold_duration.valueChanged.connect(self.hold_duration_changed)
        self._field_row(grid, 1, "壓住時間", self.hold_duration, "壓滿這段時間自動放開；0 要自己按停止")
        self.hold_delay = QDoubleSpinBox()
        self.hold_delay.setRange(0.5, 3.0)
        self.hold_delay.setDecimals(1)
        self.hold_delay.setSingleStep(0.5)
        self.hold_delay.setSuffix(" 秒")
        self.hold_delay.setValue(min(3.0, max(0.5, float(self.settings.hold_delay))))
        self.hold_delay.valueChanged.connect(self.hold_delay_changed)
        self._field_row(grid, 2, "開始前等待", self.hold_delay, "按下開始後先等一下（0.5～3 秒），好移到目標位置")
        self._field_row(grid, 3, "快捷鍵（鍵盤）", self.make_hotkey_row("hold_hotkey"),
                        "按一次開始、再按一次結束；可含組合鍵")
        self.hold_mouse_edit = MouseHotkeyEdit(self.settings.hold_mouse_hotkey)
        self.hold_mouse_edit.changed.connect(self.hold_mouse_hotkey_changed)
        self.hold_mouse_edit.capture_state.connect(self.on_capture_state)
        self._field_row(grid, 4, "快捷鍵（滑鼠）", self._mouse_hotkey_row(self.hold_mouse_edit),
                        "可用滑鼠中鍵或側鍵（遊戲內也有效）")
        self.hold_button = QPushButton()
        self.hold_button.setObjectName("primary")
        self.hold_button.setFixedHeight(42)
        self.hold_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hold_button.clicked.connect(self.hold_button_clicked)
        grid.addWidget(self.hold_button, 12, 0, 1, 2)
        self.hold_status = QLabel()
        self.hold_status.setObjectName("hint")
        self.hold_status.setWordWrap(True)
        grid.addWidget(self.hold_status, 13, 0, 1, 2)
        columns.addWidget(card, 1, Qt.AlignmentFlag.AlignTop)

        box.addLayout(columns)
        box.addStretch()
        self.update_clicker_ui()
        self.update_hold_ui()
        self._update_mouse_hotkey_timer()
        return page

    def diagnostics_page(self):
        page, box = self.page_shell(
            "音訊診斷",
            "這裡確認 EchoDeck 本機端是否準備完成；WARDOGS 是否真的送達附近玩家仍需在真實伺服器測試。")
        card, grid = self.section_card("目前狀態")
        self.diagnostic_labels = {}
        for r, (key, label) in enumerate([
            ("output", "虛擬聲卡輸出"), ("input", "真實麥克風輸入"),
            ("monitor", "本機音效監聽"), ("mic", "麥克風混入"),
            ("effect", "音效播放狀態"), ("ptt", "遊戲 PTT 狀態"),
        ]):
            grid.addWidget(QLabel(label, objectName="settingLabel"), r, 0)
            value = QLabel("檢查中…")
            value.setObjectName("diagOff")
            grid.addWidget(value, r, 1, Qt.AlignmentFlag.AlignLeft)
            self.diagnostic_labels[key] = value
        self.diagnostic_refresh = QPushButton("重新檢查")
        self.diagnostic_refresh.clicked.connect(self.refresh_diagnostics)
        grid.addWidget(self.diagnostic_refresh, 6, 0, 1, 2)
        box.addWidget(card)
        box.addStretch()
        QTimer.singleShot(0, self.refresh_diagnostics)
        return page

    def _ptt_diagnostic(self):
        if not self.ptt_down:
            return False, "未按住"
        kind, name = target_kind_name(self.settings.ptt_key)
        if kind == "mouse":
            return True, "已按住（滑鼠鍵）"
        state = key_is_down(name)
        if state is True:
            return True, f"已按住「{name}」，Windows 已確認"
        if state is False:
            return False, f"程式已送出「{name}」，但 Windows 沒偵測到按下"
        return True, "已按住"

    def refresh_diagnostics(self):
        values = {
            "output": (self.mixer.output_stream is not None, self.settings.output_device or "尚未選擇"),
            "input": (self.mixer.input_stream is not None, self.settings.microphone_device or "尚未選擇"),
            "monitor": ((not self.settings.monitor_enabled) or self.mixer.monitor_stream is not None,
                        "關閉" if not self.settings.monitor_enabled else self.settings.monitor_device or "尚未選擇"),
            "mic": (self.settings.microphone_enabled, "開啟" if self.settings.microphone_enabled else "關閉"),
            "effect": (self.mixer.effect_playing(), "播放中" if self.mixer.effect_playing() else "待命"),
            "ptt": self._ptt_diagnostic(),
        }
        for key, (ok, text) in values.items():
            widget = self.diagnostic_labels.get(key)
            if widget:
                widget.setText(("✓ " if ok else "○ ") + text)
                widget.setObjectName("diagOk" if ok else "diagOff")
                restyle(widget)

    def settings_page(self):
        page, box = self.page_shell("設定精靈", "只要設定一次。每個欄位旁都有用途說明，不用懂音訊技術。")

        # -- music folders --
        card, grid = self.section_card("音樂資料夾", "全部資料夾（含子資料夾）會合併成同一個曲庫，不用一個一個切換。")
        grid.addWidget(QLabel("資料夾", objectName="settingLabel"), 0, 0, Qt.AlignmentFlag.AlignTop)
        self.folder_list = QListWidget()
        self.folder_list.setObjectName("folderList")
        self.folder_list.setFixedHeight(150)
        self.folder_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        grid.addWidget(self.folder_list, 0, 1)
        folder_buttons = QHBoxLayout()
        folder_buttons.setSpacing(10)
        add_folder_button = QPushButton("＋  加入資料夾")
        add_folder_button.clicked.connect(self.add_folder)
        self.remove_folder_button = QPushButton("－  移除所選")
        self.remove_folder_button.clicked.connect(self.remove_selected_folder)
        rescan_button = QPushButton("↻  重新掃描")
        rescan_button.clicked.connect(self.scan_folders)
        folder_buttons.addWidget(add_folder_button)
        folder_buttons.addWidget(self.remove_folder_button)
        folder_buttons.addWidget(rescan_button)
        folder_buttons.addStretch()
        grid.addLayout(folder_buttons, 1, 1)
        grid.addWidget(QLabel("移除資料夾只會從曲庫拿掉，不會刪除檔案；最愛直接記錄歌曲完整路徑，不受影響。",
                              objectName="hint"), 2, 1)
        box.addWidget(card)

        # -- YouTube favourites folder --
        card, grid = self.section_card("YouTube 檔案存放位置",
                                       "最愛歌曲與 YouTube 元件（yt-dlp、Deno）都可以各自選資料夾，例如放在 EchoDeck 資料夾裡方便找。")
        grid.addWidget(QLabel("最愛歌曲位置", objectName="settingLabel"), 0, 0)
        self.yt_dir_edit = QLineEdit()
        self.yt_dir_edit.setReadOnly(True)
        grid.addWidget(self.yt_dir_edit, 0, 1)
        yt_dir_buttons = QHBoxLayout()
        yt_dir_buttons.setSpacing(10)
        yt_pick_button = QPushButton("📁  選擇資料夾…")
        yt_pick_button.clicked.connect(self.yt_choose_fav_dir)
        yt_app_button = QPushButton("📦  放到 EchoDeck 資料夾")
        yt_app_button.setToolTip("存到 EchoDeck.exe 所在的資料夾裡的「YouTube最愛」")
        yt_app_button.clicked.connect(self.yt_use_app_fav_dir)
        self.yt_dir_reset_button = QPushButton("↩  恢復預設")
        self.yt_dir_reset_button.setToolTip("%APPDATA%\\EchoDeck\\YouTube最愛")
        self.yt_dir_reset_button.clicked.connect(self.yt_reset_fav_dir)
        for b in (yt_pick_button, yt_app_button, self.yt_dir_reset_button):
            yt_dir_buttons.addWidget(b)
        yt_dir_buttons.addStretch()
        grid.addLayout(yt_dir_buttons, 1, 1)
        grid.addWidget(QLabel("更換位置時，可以選擇要不要把已存好的歌一起搬過去。", objectName="hint"), 2, 1)
        grid.addWidget(QLabel("元件位置（tools）", objectName="settingLabel"), 3, 0)
        self.tools_dir_edit = QLineEdit()
        self.tools_dir_edit.setReadOnly(True)
        grid.addWidget(self.tools_dir_edit, 3, 1)
        tools_buttons = QHBoxLayout()
        tools_buttons.setSpacing(10)
        tools_pick = QPushButton("📁  選擇資料夾…")
        tools_pick.clicked.connect(self.tools_choose_dir)
        tools_app = QPushButton("📦  放到 EchoDeck 資料夾")
        tools_app.setToolTip("存到 EchoDeck.exe 所在的資料夾裡的「tools」")
        tools_app.clicked.connect(self.tools_use_app_dir)
        self.tools_dir_reset_button = QPushButton("↩  恢復預設")
        self.tools_dir_reset_button.setToolTip("%APPDATA%\\EchoDeck\\tools")
        self.tools_dir_reset_button.clicked.connect(self.tools_reset_dir)
        for b in (tools_pick, tools_app, self.tools_dir_reset_button):
            tools_buttons.addWidget(b)
        tools_buttons.addStretch()
        grid.addLayout(tools_buttons, 4, 1)
        grid.addWidget(QLabel("更換位置時會把 yt-dlp.exe 與 deno.exe 複製過去；若正在下載或播放 YouTube 請稍後再換。", objectName="hint"), 5, 1)
        self.refresh_yt_dir_ui()
        box.addWidget(card)

        # -- appearance --
        card, grid = self.section_card("外觀", "整套外觀一起切換：背景圖案、玻璃面板、選取色、文字與強調色。")
        grid.addWidget(QLabel("主題色", objectName="settingLabel"), 0, 0)
        self.theme_combo = QComboBox()
        for key, spec in THEMES.items():
            label, colors = spec["label"], palette_for(key)
            swatch = QPixmap(22, 22)
            swatch.fill(Qt.GlobalColor.transparent)
            sp = QPainter(swatch)
            sp.setRenderHint(QPainter.RenderHint.Antialiasing)
            art = get_art(key).scaled(22, 22, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(1, 1, 20, 20), 6, 6)
            sp.setClipPath(clip)
            sp.drawPixmap(0, 0, art)
            sp.setClipping(False)
            sp.setPen(QPen(QColor(colors["ACC_BAR"]), 1.6))
            sp.setBrush(Qt.BrushStyle.NoBrush)
            sp.drawRoundedRect(QRectF(1, 1, 20, 20), 6, 6)
            sp.end()
            self.theme_combo.addItem(QIcon(swatch), label, key)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(self.settings.theme)))
        self.theme_combo.currentIndexChanged.connect(self.theme_changed)
        grid.addWidget(self.theme_combo, 0, 1)
        grid.addWidget(QLabel("播放中視覺化", objectName="settingLabel"), 1, 0)
        self.viz_combo = QComboBox()
        for label, key in (("柱狀頻譜", "bars"), ("鏡像頻譜", "mirror"), ("柔和曲線", "wave"), ("關閉", "off")):
            self.viz_combo.addItem(label, key)
        self.viz_combo.setCurrentIndex(max(0, self.viz_combo.findData(self.settings.viz_style)))
        self.viz_combo.currentIndexChanged.connect(self.viz_style_changed)
        grid.addWidget(self.viz_combo, 1, 1)
        box.addWidget(card)

        # -- output device --
        card, grid = self.section_card("音樂輸出裝置")
        grid.addWidget(QLabel("輸出裝置", objectName="settingLabel"), 0, 0)
        self.device_combo = QComboBox()
        self.device_combo.currentTextChanged.connect(self.device_changed)
        grid.addWidget(self.device_combo, 0, 1)
        device_note = QLabel("選擇虛擬聲卡的播放端，例如 VB-CABLE 的「CABLE Input」。遊戲內麥克風則選「CABLE Output」。")
        device_note.setObjectName("hint")
        device_note.setWordWrap(True)
        grid.addWidget(device_note, 1, 1)
        box.addWidget(card)

        # -- microphone --
        card, grid = self.section_card("麥克風")
        grid.addWidget(QLabel("輸入裝置", objectName="settingLabel"), 0, 0)
        self.mic_combo = QComboBox()
        self.mic_combo.currentTextChanged.connect(self.microphone_device_changed)
        grid.addWidget(self.mic_combo, 0, 1)
        grid.addWidget(QLabel("混入開關", objectName="settingLabel"), 1, 0)
        self.mic_button = QPushButton()
        self.mic_button.setObjectName("primary")
        self.mic_button.clicked.connect(self.toggle_microphone)
        grid.addWidget(self.mic_button, 1, 1)
        grid.addWidget(QLabel("麥克風音量", objectName="settingLabel"), 2, 0)
        self.mic_volume = QSlider(Qt.Orientation.Horizontal)
        self.mic_volume.setRange(0, 200)
        self.mic_volume.setValue(self.settings.microphone_volume)
        self.mic_volume.valueChanged.connect(self.change_microphone_volume)
        mic_widget, _badge = self.slider_with_value(self.mic_volume)
        grid.addWidget(mic_widget, 2, 1)
        self.mute_mic_when_sound = QCheckBox("播放音效時自動靜音麥克風（預設開啟）")
        self.mute_mic_when_sound.setChecked(self.settings.mute_mic_while_sound)
        self.mute_mic_when_sound.toggled.connect(self.mute_mic_when_sound_changed)
        grid.addWidget(self.mute_mic_when_sound, 3, 1)
        box.addWidget(card)

        # -- local monitor --
        card, grid = self.section_card("本機監聽")
        self.monitor_check = QCheckBox("我也要從耳機／喇叭聽到播放的音樂")
        self.monitor_check.setChecked(self.settings.monitor_enabled)
        self.monitor_check.toggled.connect(self.monitor_changed)
        grid.addWidget(self.monitor_check, 0, 1)
        grid.addWidget(QLabel("監聽裝置", objectName="settingLabel"), 1, 0)
        self.monitor_combo = QComboBox()
        self.monitor_combo.currentTextChanged.connect(self.monitor_device_changed)
        grid.addWidget(self.monitor_combo, 1, 1)
        box.addWidget(card)

        # -- push to talk --
        card, grid = self.section_card("遊戲區域發話鍵（PTT）")
        grid.addWidget(QLabel("發話鍵", objectName="settingLabel"), 0, 0)
        self.ptt_input = QLineEdit(self.settings.ptt_key)
        self.ptt_input.setPlaceholderText("例如：v、caps lock、f8、mouse4")
        self.ptt_input.editingFinished.connect(self.ptt_changed)
        grid.addWidget(self.ptt_input, 0, 1, 1, 2)
        self.auto_ptt_check = QCheckBox("播放音樂時，自動按住並在停止時放開此按鍵")
        self.auto_ptt_check.setChecked(self.settings.auto_ptt)
        self.auto_ptt_check.toggled.connect(self.auto_ptt_changed)
        grid.addWidget(self.auto_ptt_check, 1, 1, 1, 2)
        grid.addWidget(QLabel("開麥等待", objectName="settingLabel"), 2, 0)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 2000)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setValue(self.settings.ptt_delay_ms)
        self.delay_spin.valueChanged.connect(self.delay_changed)
        grid.addWidget(self.delay_spin, 2, 1)
        grid.addWidget(QLabel("先按住 PTT 再播歌，避免歌曲開頭被遊戲切掉。", objectName="hint"), 2, 2)
        self.ptt_test = QPushButton("測試：按住遊戲 PTT 兩秒")
        self.ptt_test.setToolTip("切到遊戲畫面後按這裡；遊戲應顯示區域發話圖示。此測試不播放音樂。")
        self.ptt_test.clicked.connect(self.test_ptt)
        grid.addWidget(self.ptt_test, 3, 1, 1, 2)
        box.addWidget(card)

        # -- playback --
        card, grid = self.section_card("播放")
        self.shuffle_check = QCheckBox("隨機播放：整份清單都播過一輪才會重複")
        self.shuffle_check.setChecked(self.settings.shuffle)
        self.shuffle_check.toggled.connect(self.shuffle_changed)
        grid.addWidget(self.shuffle_check, 0, 1)
        grid.addWidget(QLabel("播完後自動播放下一首的設定已移至「音樂庫」頁上方的下拉選單。", objectName="hint"), 1, 1)
        self.normalize_check = QCheckBox("音量正規化（自動降低過大的歌曲，讓音量較一致）")
        self.normalize_check.setChecked(self.settings.normalize_audio)
        self.normalize_check.toggled.connect(self.normalize_changed)
        grid.addWidget(self.normalize_check, 2, 1)
        self.audio_test_button = QPushButton("執行音訊設定測試")
        self.audio_test_button.clicked.connect(self.run_audio_test)
        grid.addWidget(self.audio_test_button, 3, 1)
        box.addWidget(card)

        warning = QLabel("麥克風混音：Mic 與音效由同一條虛擬聲卡輸出串流送出。耳機監聽只包含音效，不包含麥克風，避免回音。")
        warning.setObjectName("privacyBanner")
        warning.setWordWrap(True)
        box.addWidget(warning)
        box.addStretch()
        return page

    def refresh_devices(self):
        previous = self.settings.output_device
        self.device_combo.blockSignals(True)
        self.monitor_combo.blockSignals(True)
        self.mic_combo.blockSignals(True)
        self.device_combo.clear()
        self.monitor_combo.clear()
        self.mic_combo.clear()
        # The same Windows endpoint can be shown once per host API. Keep one
        # human-readable entry per exact name so menus are not duplicated.
        outputs = list(dict.fromkeys(self.mixer.devices('output')))
        inputs = list(dict.fromkeys(self.mixer.devices('input')))
        for device in outputs:
            self.device_combo.addItem(device)
            self.monitor_combo.addItem(device)
        self.mic_combo.addItem(MIC_OFF_LABEL)
        for device in inputs:
            self.mic_combo.addItem(device)
        index = self.device_combo.findText(previous)
        self.device_combo.setCurrentIndex(index if index >= 0 else 0)
        monitor_index = self.monitor_combo.findText(self.settings.monitor_device)
        if monitor_index < 0:
            # Default output is generally the first Qt device and is a much
            # safer monitor choice than the VB-CABLE endpoint.
            monitor_index = 0
        self.monitor_combo.setCurrentIndex(monitor_index)
        mic_index = self.mic_combo.findText(self.settings.microphone_device)
        # Empty setting (or a missing device) = microphone not used.
        self.mic_combo.setCurrentIndex(mic_index if (mic_index > 0 and self.settings.microphone_device) else 0)
        self.device_combo.blockSignals(False)
        self.monitor_combo.blockSignals(False)
        self.mic_combo.blockSignals(False)
        self.apply_device()
        self.apply_monitor_device()
        self.update_mic_button()

    def apply_device(self):
        self.settings.output_device = self.device_combo.currentText()
        self.save_settings()
        self.apply_mixer_output()

    def apply_monitor_device(self):
        self.settings.monitor_device = self.monitor_combo.currentText()
        self.save_settings()
        self.apply_mixer_output()

    def apply_mixer_output(self):
        if self.settings.output_device:
            self.mixer.configure_output(self.settings.output_device, self.settings.monitor_device,
                                        self.settings.monitor_enabled)

    def apply_microphone(self):
        self.mixer.configure_microphone(self.settings.microphone_device)
        self.mixer.mic_enabled = self.settings.microphone_enabled
        self.mixer.mute_mic_while_sound = self.settings.mute_mic_while_sound
        self.mixer.mic_gain = self.settings.microphone_volume / 100

    def show_audio_error(self, message):
        QMessageBox.warning(self, APP_NAME, message)

    def run_audio_test(self):
        checks = [
            ("虛擬輸出", self.mixer.output_stream is not None),
            ("麥克風輸入", self.mixer.input_stream is not None),
            ("本機監聽", (not self.settings.monitor_enabled) or self.mixer.monitor_stream is not None),
            ("Mic 混入", self.settings.microphone_enabled),
        ]
        report = "\n".join(f"{'✓' if ok else '○'} {name}" for name, ok in checks)
        QMessageBox.information(self, APP_NAME, "本機音訊鏈路測試\n\n" + report +
            "\n\n此測試確認 EchoDeck 的裝置已開啟；最後仍需在 WARDOGS 真實伺服器按 K，確認『當地』沒有紅叉。")

    def refresh_folder_list(self):
        self.folder_list.clear()
        for folder in self.settings.music_folders:
            exists = Path(folder).is_dir()
            item = QListWidgetItem(folder if exists else f"{folder}　（找不到）")
            item.setData(ROLE_PATH, folder)
            item.setToolTip(folder)
            if not exists:
                item.setForeground(QColor(T["NOT_FOUND"]))
            self.folder_list.addItem(item)
        self.remove_folder_button.setEnabled(bool(self.settings.music_folders))
        self.folder_button.setToolTip("\n".join(self.settings.music_folders) or "尚未選擇資料夾")

    def show_folder_manage_menu(self):
        menu = QMenu(self)
        if not self.settings.music_folders:
            empty = menu.addAction("尚未加入任何資料夾")
            empty.setEnabled(False)
        else:
            for folder in list(self.settings.music_folders):
                exists = Path(folder).is_dir()
                label = folder if exists else f"{folder}　（找不到）"
                act = menu.addAction(f"✕  {label}")
                act.setToolTip("點一下從曲庫移除這個資料夾（不會刪除檔案）")
                act.triggered.connect(lambda checked=False, f=folder: self.remove_folder_path(f))
            menu.addSeparator()
        add_act = menu.addAction("＋  加入新資料夾…")
        add_act.triggered.connect(self.add_folder)
        menu.exec(self.folder_manage_button.mapToGlobal(
            self.folder_manage_button.rect().bottomLeft()))

    def remove_folder_path(self, folder):
        self.settings.music_folders = [f for f in self.settings.music_folders if path_key(f) != path_key(folder)]
        self.save_settings()
        self.refresh_folder_list()
        self.scan_folders()
        self.subtitle.setText("已從曲庫移除該資料夾（原始檔案不受影響）。")

    def add_folder(self):
        start = self.settings.music_folders[-1] if self.settings.music_folders else str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "加入音樂資料夾", start)
        if not path:
            return
        if not self._add_folder_path(path):
            self.subtitle.setText("這個資料夾已經在曲庫裡了。")
            return
        self.refresh_folder_list()
        self.scan_folders()

    def _add_folder_path(self, path):
        """Add one folder to the library settings; False if it was already there."""
        path = str(Path(path))
        if any(path_key(f) == path_key(path) for f in self.settings.music_folders):
            return False
        self.settings.music_folders.append(path)
        self.save_settings()
        return True

    @staticmethod
    def _dropped_folders(event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        return [u.toLocalFile() for u in urls if u.isLocalFile() and Path(u.toLocalFile()).is_dir()]

    def dragEnterEvent(self, event):
        if self._dropped_folders(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._dropped_folders(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        folders = self._dropped_folders(event)
        if not folders:
            return
        event.acceptProposedAction()
        added = [f for f in folders if self._add_folder_path(f)]
        if added:
            self.refresh_folder_list()
            self.scan_folders()
            self.subtitle.setText(f"已加入 {len(added)} 個資料夾，正在掃描…")
        else:
            self.subtitle.setText("拖進來的資料夾都已經在曲庫裡了。")

    def remove_selected_folder(self):
        item = self.folder_list.currentItem()
        if item is None:
            return
        folder = item.data(ROLE_PATH)
        self.settings.music_folders = [f for f in self.settings.music_folders if path_key(f) != path_key(folder)]
        self.save_settings()
        self.refresh_folder_list()
        self.scan_folders()

    def update_selection_details(self, current, _previous):
        if current:
            path = Path(current.data(Qt.ItemDataRole.UserRole))
            self.now_title.setText(path.stem)
            self.now_path.setText(str(path.parent))
            if self._restored_last_track:
                self._remember_last_track(path)
                self.save_settings()

    def play_selected(self):
        item = self.track_list.currentItem()
        if not item and self.tracks:
            self.track_list.setCurrentRow(0)
            item = self.track_list.currentItem()
        if item:
            self.play_track(Path(item.data(Qt.ItemDataRole.UserRole)))
        elif not self.tracks:
            self.pages.setCurrentIndex(3)
            QMessageBox.information(self, APP_NAME, "請先在設定頁選擇含有音樂檔的資料夾。")

    def _pool(self):
        """Songs that 隨機 / 下一首 draw from: the favourites when that view is open, else the library."""
        pl = self._pl_of_view()
        if pl is not None:
            pool = [Path(p) for p in pl["tracks"] if Path(p).is_file()]
            if pool:
                return pool
        if self.view_mode == "fav":
            pool = [Path(p) for p in self.settings.favorites if Path(p).is_file()]
            if pool:
                return pool
        return list(self.tracks)

    def play_random(self):
        pool = [p for p in self._pool() if p.is_file()]
        if not pool:
            self.play_selected()
            return
        if self.settings.shuffle and len(pool) > 1:
            # "Deal a deck": every song is played once before any repeats. Songs
            # played by hand also count, and a new round never opens with the
            # song that just finished.
            choices = [p for p in pool if path_key(p) not in self._shuffle_played]
            if not choices:
                self._shuffle_played.clear()
                current = path_key(self.current_track) if self.current_track is not None else None
                choices = [p for p in pool if path_key(p) != current] or pool
        else:
            choices = pool
        track = random.choice(choices)
        self._select_in_list(track)
        self.play_track(track)

    def play_track(self, track: Path):
        track = Path(track)
        if not track.is_file():
            # Moved / renamed / deleted: grey it out and carry on instead of raising.
            self._flag_missing(track)
            if not self.mixer.effect_playing() and not self._starting:
                self.release_ptt()
            return
        self.stop(release=False)
        self._play_token += 1
        token = self._play_token
        self._starting = True
        self.effect_was_playing = False
        # Mute the mic BEFORE PTT goes down and keep it muted until PTT is
        # released, so no voice leaks during the lead-in / release tail.
        self.mixer.mic_hold = True
        self.current_track = track
        self.mark_playing(track)
        self.current_track_index = self.tracks.index(track) if track in self.tracks else -1
        self.history.append(track)
        self._shuffle_played.add(path_key(track))
        self._remember_last_track(track)
        self.save_settings()
        self.history = self.history[-20:]
        self.set_sub("正在送往虛擬麥克風…")
        if self.settings.auto_ptt:
            already_down = self.ptt_down
            self.press_ptt()
            # Non-blocking wait so the UI (and poll timer) never re-enter here.
            QTimer.singleShot(0 if already_down else max(0, self.settings.ptt_delay_ms),
                              lambda: self._begin_playback(track, token))
        else:
            self._begin_playback(track, token)

    def _begin_playback(self, track: Path, token: int):
        if token != self._play_token:
            return  # stopped or replaced while waiting for PTT
        self._starting = False
        if not self.mixer.play_file(track, self.settings.normalize_audio):
            self.release_ptt()
            self.mixer.mic_hold = False
        else:
            self.effect_was_playing = True
            self.set_sub(track.parent.name or str(track.parent))

    def stop(self, release=True):
        self._play_token += 1  # cancels any pending delayed start
        self._starting = False
        self.mixer.stop_effect()
        self.current_source = None
        self.mark_playing(None)
        self._refresh_source_marks()
        if release:
            self.release_ptt()
            if not self.ptt_down:
                self.mixer.mic_hold = False

    def play_next_in_order(self):
        pool = [p for p in self._pool() if p.is_file()]
        if not pool:
            self.play_selected()
            return
        keys = [path_key(p) for p in pool]
        current = path_key(self.current_track) if self.current_track is not None else None
        index = (keys.index(current) + 1) % len(pool) if current in keys else 0
        self._select_in_list(pool[index])
        self.play_track(pool[index])

    def poll_effect_state(self):
        playing = self.mixer.effect_playing()
        self._ptt_keepalive(playing)
        if self.effect_was_playing and not playing:
            self.mark_playing(None)
            was_manual_stop = self.mixer.manual_stop
            mode = self.settings.autoplay_mode
            src, self.current_source = self.current_source, None
            self._refresh_source_marks()
            if src is not None:
                if not was_manual_stop and self._source_ended(src):
                    return
            else:
                if not was_manual_stop and mode == "random":
                    self.play_random()
                    return
                if not was_manual_stop and mode == "sequential":
                    self.play_next_in_order()
                    return
                if not was_manual_stop and mode == "loop" and self.current_track is not None:
                    self.play_track(self.current_track)
                    return
            self.release_ptt()
        self.effect_was_playing = playing
        if not playing:
            self.mixer.release_song_if_finished()
        peak_in, self.mixer.mic_in_peak = self.mixer.mic_in_peak, 0.0
        peak_out, self.mixer.mic_out_peak = self.mixer.mic_out_peak, 0.0
        self.meter.setText(
            f"麥克風收音 {min(999, int(peak_in * 100)):>3}%　｜　EchoDeck 送進虛擬聲卡的麥克風 {min(999, int(peak_out * 100)):>3}%"
            "　（放音樂時右邊應為 0%）"
            + (f"　音訊欠載 {self.mixer.underflows}" if self.mixer.underflows else ""))
        if not playing and not self.ptt_down and not self._starting:
            self.mixer.mic_hold = False
        elapsed, total = self.mixer.times()
        self._update_online_panels(playing, elapsed, total)
        if not self.progress.isSliderDown():
            self.progress.setValue(int(min(1.0, elapsed / total) * 1000) if total else 0)
        if playing and self.mixer.paused:
            self.status.setText("● 已暫停")
            self.status.setObjectName("statusPaused")
        elif playing:
            self.status.setText("● 正在區域發話")
            self.status.setObjectName("statusLive")
        else:
            self.status.setText("● 待命中")
            self.status.setObjectName("statusIdle")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self._sync_pause_ui(playing)

    def press_ptt(self):
        kind, name = target_kind_name(self.settings.ptt_key)
        if self.ptt_down and kind != "mouse" and key_is_down(name) is False:
            # The app believed PTT was held, but Windows says the key is up
            # (e.g. it was lost on a focus change). Press it again.
            self.ptt_down = False
        if self.ptt_down:
            return
        try:
            wait_for_modifiers_released()  # avoid sending Alt+K when K was meant
            self.ptt_kind = kind
            if kind == "mouse":
                send_mouse_button(name, True)
                self.ptt_uses_scan_code = False
            else:
                self.ptt_uses_scan_code = send_key_state(name, True)
            self.ptt_down = True
            QTimer.singleShot(250, self._verify_ptt)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"無法按下 PTT 鍵：{exc}\n\n若遊戲以系統管理員啟動，請也以系統管理員啟動 EchoDeck。")

    def _ptt_keepalive(self, playing):
        """If Windows says the PTT key came up while we are still 'talking'
        (focus change, another program, a stray key-up), press it again."""
        if not (self.ptt_down and (playing or self._starting)) or self.mixer.paused:
            return
        kind, name = target_kind_name(self.settings.ptt_key)
        if kind == "mouse" or key_is_down(name) is not False or modifiers_held():
            return
        try:
            self.ptt_uses_scan_code = send_key_state(name, True)
        except Exception:
            pass

    def _verify_ptt(self):
        """Tell the user when Windows did not register the injected PTT key."""
        if not self.ptt_down:
            return
        kind, name = target_kind_name(self.settings.ptt_key)
        if kind != "mouse" and key_is_down(name) is False:
            self.subtitle.setText("⚠ 已送出發話鍵，但 Windows 沒有偵測到按下。請到「診斷」頁確認，或檢查 PTT 鍵設定。")

    def release_ptt(self):
        if not self.ptt_down:
            return
        try:
            time.sleep(self.settings.release_delay_ms / 1000)
            kind, name = target_kind_name(self.settings.ptt_key)
            if kind == "mouse":
                send_mouse_button(name, False)
            elif self.ptt_uses_scan_code:
                send_scan_code(name, True)
            else:
                keyboard.release(name)
        finally:
            self.ptt_down = False
            self.ptt_uses_scan_code = False
            if not self._starting:
                self.mixer.mic_hold = False

    def test_ptt(self):
        if not target_kind_name(self.settings.ptt_key)[1]:
            QMessageBox.warning(self, APP_NAME, "請先設定遊戲區域發話鍵。")
            return
        self.subtitle.setText("PTT 測試將在 3 秒後執行；請立刻切回遊戲畫面。")
        QTimer.singleShot(3000, self._run_ptt_test)

    def _run_ptt_test(self):
        self.press_ptt()
        QTimer.singleShot(2000, self._finish_ptt_test)

    def _finish_ptt_test(self):
        self.release_ptt()
        self.subtitle.setText("PTT 測試已完成。若遊戲沒有圖示，請以系統管理員啟動 EchoDeck 與遊戲，並告訴我遊戲名稱。")

    def install_hotkeys(self):
        self.remove_hotkeys()
        bindings = [
            ("play_hotkey", self.signals.play_requested),
            ("stop_hotkey", self.signals.stop_requested),
            ("random_hotkey", self.signals.random_requested),
            ("next_hotkey", self.signals.next_requested),
            ("mic_hotkey", self.signals.mic_requested),
            ("pause_hotkey", self.signals.pause_requested),
            ("loop_hotkey", self.signals.loop_requested),
            ("hold_hotkey", self.signals.hold_requested),
            ("clicker_hotkey", self.signals.clicker_requested),
            ("volume_up_hotkey", self.signals.volume_up_requested),
            ("volume_down_hotkey", self.signals.volume_down_requested),
        ]
        self._hotkey_states = {}
        self._poll_bindings = []
        self._poll_alive = False
        ptt_kind, ptt_name = target_kind_name(self.settings.ptt_key)
        ptt_name = (ptt_name or "").strip().lower() if ptt_kind != "mouse" else ""
        use_polling = hasattr(ctypes, "windll")
        registered = failed = 0
        for attr, signal in bindings:
            names = hotkey_names(getattr(self.settings, attr))
            if not names:
                continue
            handler = self._make_hotkey_handler(attr, signal, names)
            # Path 1 - keyboard library (the original, known-good mechanism).
            # It only matches when the pressed keys are EXACTLY the combo, and
            # EchoDeck itself holds the PTT key during playback, so also
            # register "combo + PTT key" so the hotkey still matches then.
            combos = [names]
            if ptt_name and ptt_name not in names:
                combos.append(names + [ptt_name])
            for combo in combos:
                try:  # one bad hotkey must not disable the rest
                    self.hotkeys.append(keyboard.add_hotkey(combo if len(combo) > 1 else combo[0], handler))
                    registered += 1
                except Exception as exc:
                    failed += 1
                    hotkey_log(f"register failed {attr} {combo}: {exc}")
            # Path 2 - poll Windows key state; shares the same de-dup state.
            vks = hotkey_vks(names) if use_polling else None
            if vks is not None:
                self._poll_bindings.append({"attr": attr, "vks": vks, "handler": handler, "was": False,
                                            "repeatable": attr in ("volume_up_hotkey", "volume_down_hotkey")})
        try:
            self._release_hook = keyboard.on_release(self._on_any_key_release)
        except Exception as exc:
            self._release_hook = None
            hotkey_log(f"on_release failed: {exc}")
        if self._poll_bindings:
            if getattr(self, "_hk_timer", None) is None:
                self._hk_timer = QTimer(self)
                self._hk_timer.setInterval(15)
                self._hk_timer.timeout.connect(self._poll_hotkeys)
            self._hk_timer.start()
        hotkey_log(f"installed: keyboard-lib registrations={registered} failed={failed} "
                   f"polled={len(self._poll_bindings)} ptt={ptt_name or ptt_kind}")

    def _poll_hotkeys(self):
        """Second, independent detector: reads Windows key state directly, so it
        also works while the PTT key is held. Calls the same handler as the
        keyboard library, whose de-dup state makes a press fire only once."""
        now = time.monotonic()
        for b in self._poll_bindings:
            held = [any(vk_is_down(vk) for vk in alts) for alts in b["vks"]]
            if any(held) and not self._poll_alive:
                self._poll_alive = True   # Windows key-state polling demonstrably works here
                hotkey_log("polling sees key presses")
            state = self._hotkey_states.get(b["attr"], ({}, None))[0]
            if all(held):
                if not b["was"]:
                    b["was"] = True
                    b["handler"]("poll")
                elif b["repeatable"]:
                    b["handler"]("poll")
            else:
                b["was"] = False
                if self._poll_alive and state.get("down"):
                    state["down"] = False  # polling is trusted only after it has seen a key

    def _make_hotkey_handler(self, attr, signal, names):
        """Fire once per physical key press. Holding a key makes Windows send
        ~30 repeat events/sec, which used to fire the action dozens of times
        (volume jumped ~50%). Volume keys may repeat slowly while held."""
        repeatable = attr in ("volume_up_hotkey", "volume_down_hotkey")
        state = {"down": False, "t0": 0.0, "last": 0.0}
        self._hotkey_states[attr] = (state, {n.lower() for n in names})

        def handler(source="lib"):
            now = time.monotonic()
            if state["down"] and now - state["last"] > 3.0 and source != "poll":
                state["down"] = False  # missed a release event; recover
            if not state["down"]:
                state.update(down=True, t0=now, last=now)
                hotkey_log(f"fire {attr} via {source}")
                signal.emit()
            elif repeatable and now - state["t0"] >= 0.45 and now - state["last"] >= 0.2:
                state["last"] = now
                signal.emit()
        return handler

    def _on_any_key_release(self, event):
        name = (event.name or "").lower()
        for state, keys in getattr(self, "_hotkey_states", {}).values():
            if name in keys:
                state["down"] = False

    def remove_hotkeys(self):
        timer = getattr(self, "_hk_timer", None)
        if timer is not None:
            timer.stop()
        self._poll_bindings = []
        hook = getattr(self, "_release_hook", None)
        if hook is not None:
            try:
                keyboard.unhook(hook)
            except Exception:
                pass
            self._release_hook = None
        for key in self.hotkeys:
            try:
                keyboard.remove_hotkey(key)
            except Exception:
                pass
        self.hotkeys = []

    def change_volume(self, value):
        self.mixer.music_gain = value / 100
        self.settings.music_volume = value
        for slider in self._volume_mirrors:
            if slider.value() != value:
                slider.blockSignals(True)
                slider.setValue(value)
                slider.blockSignals(False)
                slider.valueChanged.emit(value)    # refresh its % badge (our own handler ignores the echo)
        self.save_settings()

    def device_changed(self, _value): self.apply_device()
    def monitor_device_changed(self, _value): self.apply_monitor_device()
    def monitor_changed(self, value): self.settings.monitor_enabled = value; self.save_settings()
    def microphone_device_changed(self, _value):
        self.settings.microphone_device = "" if self.mic_combo.currentIndex() <= 0 else self.mic_combo.currentText()
        self.save_settings(); self.apply_microphone(); self.update_mic_button()
    def change_microphone_volume(self, value):
        self.settings.microphone_volume = value; self.mixer.mic_gain = value / 100; self.save_settings()
    def mute_mic_when_sound_changed(self, value):
        self.settings.mute_mic_while_sound = value; self.mixer.mute_mic_while_sound = value; self.save_settings()
    def toggle_microphone(self):
        if not self.settings.microphone_device:
            return  # no microphone selected
        self.settings.microphone_enabled = not self.settings.microphone_enabled
        self.mixer.mic_enabled = self.settings.microphone_enabled
        self.save_settings(); self.update_mic_button()
    def update_mic_button(self):
        if not self.settings.microphone_device:
            self.mic_button.setText("○ 麥克風：未使用（輸入裝置選「不使用麥克風」）")
            self.mic_button.setObjectName("")
            self.mic_button.setEnabled(False)
            restyle(self.mic_button)
            return
        self.mic_button.setEnabled(True)
        if self.settings.microphone_enabled:
            self.mic_button.setText("● 麥克風：開啟（會混入虛擬聲卡）")
            self.mic_button.setObjectName("primary")
        else:
            self.mic_button.setText("○ 麥克風：關閉")
            self.mic_button.setObjectName("")
        restyle(self.mic_button)
    def ptt_changed(self):
        self.settings.ptt_key = self.ptt_input.text().strip().lower(); self.save_settings(); self.install_hotkeys()
    def viz_style_changed(self, _index):
        self.settings.viz_style = self.viz_combo.currentData()
        self.viz.set_style(self.settings.viz_style)
        for extra in self._extra_viz:
            extra.set_style(self.settings.viz_style)
        self.save_settings()

    def theme_changed(self, _index):
        key = set_theme(self.theme_combo.currentData())
        self.settings.theme = key
        QApplication.instance().setStyleSheet(build_stylesheet())
        for page, button in self.nav_buttons.items():      # icon colours depend on light / dark
            button.setIcon(nav_icon(self.nav_kinds[page]))
        self.track_list.viewport().update()
        self.centralWidget().update()
        self.save_settings()

    def auto_ptt_changed(self, value): self.settings.auto_ptt = value; self.save_settings()
    def delay_changed(self, value): self.settings.ptt_delay_ms = value; self.save_settings()
    def shuffle_changed(self, value): self.settings.shuffle = value; self.save_settings()
    def normalize_changed(self, value): self.settings.normalize_audio = value; self.save_settings()
    def autoplay_changed(self, _index):
        self.settings.autoplay_mode = self.autoplay_combo.currentData()
        self.save_settings()
        if hasattr(self, "loop_button"):
            self.loop_button.blockSignals(True)
            self.loop_button.setChecked(self.settings.autoplay_mode == "loop")
            self.loop_button.blockSignals(False)
            self._set_loop_button_style()

    def loop_button_toggled(self, checked):
        self._apply_loop_state(checked)

    def toggle_loop(self):
        self._apply_loop_state(self.settings.autoplay_mode != "loop")

    def _apply_loop_state(self, enabled):
        self.settings.autoplay_mode = "loop" if enabled else "off"
        self.save_settings()
        if hasattr(self, "autoplay_combo"):
            index = self.autoplay_combo.findData(self.settings.autoplay_mode)
            self.autoplay_combo.blockSignals(True)
            self.autoplay_combo.setCurrentIndex(max(0, index))
            self.autoplay_combo.blockSignals(False)
        if hasattr(self, "loop_button"):
            self.loop_button.blockSignals(True)
            self.loop_button.setChecked(enabled)
            self.loop_button.blockSignals(False)
            self._set_loop_button_style()

    def _set_loop_button_style(self):
        self.loop_button.setObjectName("primary" if self.loop_button.isChecked() else "")
        restyle(self.loop_button)
    def filter_tracks(self, text):
        text = text.strip().lower()
        for row in range(self.track_list.count()):
            item = self.track_list.item(row)
            haystack = (item.data(Qt.ItemDataRole.UserRole + 2) or item.text()).lower()
            item.setHidden(bool(text) and text not in haystack)
        # Reordering a filtered list would be confusing, so it is only allowed
        # in the 最愛 tab while the search box is empty.
        self.track_list.set_reorderable((self.view_mode == "fav" or self._pl_of_view() is not None) and not text)

    def fav_order_changed(self):
        """The user dragged a favourite to a new spot: save that order. 下一首 and
        the numbering both follow settings.favorites."""
        pl = self._pl_of_view()
        if self.view_mode != "fav" and pl is None:
            return
        lst = self.track_list
        order = [str(lst.item(row).data(ROLE_PATH)) for row in range(lst.count())]
        if pl is not None:
            pl["tracks"] = order
        else:
            self.settings.favorites = order
        self.save_settings()
        width = max(2, len(str(lst.count())))
        for row in range(lst.count()):
            lst.item(row).setData(ROLE_NUM, f"{row + 1:0{width}d}")
        lst.viewport().update()
    def on_capture_state(self, capturing):
        self._capturing = bool(capturing)
        # Suspend global hotkeys while a key is being recorded so it cannot trigger an action.
        if capturing:
            self.remove_hotkeys()
        else:
            self.install_hotkeys()

    def hotkey_edit_changed(self, attr, value):
        names = hotkey_names(value)
        if names:
            for other, edit in self.hotkey_edits.items():
                if other != attr and hotkey_names(getattr(self.settings, other)) == names:
                    QMessageBox.warning(self, APP_NAME, f"「{hotkey_display(value)}」已被其他功能使用，請換一組按鍵。")
                    self.hotkey_edits[attr].set_value(getattr(self.settings, attr))
                    return
        setattr(self.settings, attr, value)
        self.save_settings()
        self.install_hotkeys()
        self.refresh_hotkey_hint()
        self.update_clicker_ui()

    def refresh_hotkey_hint(self):
        s = self.settings
        def d(value):
            return hotkey_display(value) or "（未設定）"
        self.hotkey_note.setText(
            f"遊戲內使用：{d(s.play_hotkey)} 播放目前選取；{d(s.random_hotkey)} 隨機播放；"
            f"{d(s.stop_hotkey)} 停止；{d(s.pause_hotkey)} 暫停／繼續；"
            f"{d(s.loop_hotkey)} 單曲循環開／關；"
            f"{d(s.volume_up_hotkey)} / {d(s.volume_down_hotkey)} 音量加／減。"
            "請在遊戲畫面內按快捷鍵，PTT 才會送到遊戲。")

    def adjust_volume(self, delta):
        self.volume.setValue(max(0, min(200, self.volume.value() + delta)))

    def toggle_pause(self):
        if not self.mixer.effect_playing():
            return
        if self.mixer.stream_info()[0]:     # live radio cannot be paused: it would resume minutes late
            self.stop()
            return
        if not self.mixer.paused:
            self.mixer.paused = True
            self.release_ptt()          # let go of PTT while silent
            self.mixer.mic_hold = True  # keep mic muted until resumed
        else:
            token = self._play_token
            if self.settings.auto_ptt:
                self.press_ptt()
                QTimer.singleShot(max(0, self.settings.ptt_delay_ms), lambda: self._finish_resume(token))
            else:
                self.mixer.paused = False

    def _finish_resume(self, token):
        if token == self._play_token:
            self.mixer.paused = False

    # ================= online sources: web radio + YouTube =================
    RADIO_NOTE = "已自動排除新聞、談話類電台。電台有沒有人說話由電台自己決定，播到主持人就換下一台。"
    YT_NOTE = "聲音和本機音樂走同一條混音器：自動按發話鍵、播放時關麥克風、本機聆聽都照常運作。"

    def set_title(self, text):
        self.card_title.setText(text)
        for w in self._panels:
            w["title"].setText(text)

    def set_sub(self, text):
        self.card_sub.setText(text)
        for w in self._panels:
            w["sub"].setText(text)

    def set_now(self, title, sub):
        self.set_title(title)
        self.set_sub(sub)

    def _set_note(self, src_or_kind, text):
        kind = src_or_kind["kind"] if isinstance(src_or_kind, dict) else src_or_kind
        if kind == "radio":
            self.radio_note.setText(text or self.RADIO_NOTE)
        else:
            self.yt_note.setText(text or self._yt_default_note())

    def _yt_default_note(self):
        if not ytdlp_available():
            return "尚未安裝 YouTube 元件：請按右上角「🔧  YouTube 元件」下載（只需要做一次）。"
        return self.YT_NOTE

    def _chip(self, text, checkable=True):
        button = QPushButton(text)
        button.setObjectName("chip")
        button.setFixedHeight(32)
        button.setCheckable(checkable)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _segmented(self, left, right, mode, on_pick):
        seg = QHBoxLayout()
        seg.setSpacing(0)
        a, b = QPushButton(left), QPushButton(right)
        a.setObjectName("segL")
        b.setObjectName("segR")
        group = QButtonGroup(self)
        group.setExclusive(True)
        for button, value in ((a, "all"), (b, "fav")):
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            group.addButton(button)
            button.clicked.connect(lambda _=False, m=value: on_pick(m))
            seg.addWidget(button)
        (b if mode == "fav" else a).setChecked(True)
        return seg

    def _now_panel(self, extras):
        """Right-hand 'now playing' panel for the online pages. Same look as the library's.
        extras = [(key, text, tooltip), ...] shown as a row of small buttons."""
        panel = QFrame()
        panel.setObjectName("nowPanel")
        panel.setMinimumWidth(380)
        p = QVBoxLayout(panel)
        p.setContentsMargins(18, 16, 18, 16)
        p.setSpacing(8)
        w = {"frame": panel}
        card = QFrame()
        card.setObjectName("nowCard")
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        k = QVBoxLayout(card)
        k.setContentsMargins(20, 12, 20, 14)
        k.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(QLabel("♪  正在播放", objectName="eyebrowAccent"))
        top.addStretch()
        w["time"] = QLabel("")
        w["time"].setObjectName("hint")
        top.addWidget(w["time"])
        k.addLayout(top)
        w["title"] = ElideLabel(self.card_title._full)
        w["title"].setObjectName("cardTitle")
        k.addWidget(w["title"])
        w["sub"] = ElideLabel(self.card_sub._full)
        w["sub"].setObjectName("muted")
        k.addWidget(w["sub"])
        k.addSpacing(2)
        w["progress"] = SeekSlider(Qt.Orientation.Horizontal)
        w["progress"].setObjectName("cardSeek")
        w["progress"].setRange(0, 1000)
        w["progress"].setFixedHeight(20)
        w["progress"].setCursor(Qt.CursorShape.PointingHandCursor)
        w["progress"].seek_requested.connect(lambda v: self.mixer.seek(v / 10))
        w["progress"].sliderReleased.connect(lambda: self.mixer.seek(w["progress"].value() / 10))
        k.addWidget(w["progress"])
        p.addWidget(card)
        viz = VisualizerWidget(lambda: self.mixer.viz_samples())
        viz.set_style(self.settings.viz_style)
        self._extra_viz.append(viz)
        p.addWidget(viz)

        vol_row = QHBoxLayout()
        vol_row.setSpacing(10)
        vol_row.addWidget(QLabel("🔊"))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 200)
        slider.setValue(self.settings.music_volume)
        slider.setToolTip("音量（0–200%，和音樂庫共用）")
        slider.valueChanged.connect(self.volume.setValue)      # change_volume() mirrors it everywhere
        wrap, _badge = self.slider_with_value(slider)
        vol_row.addWidget(wrap, 1)
        vol_row.setContentsMargins(4, 0, 4, 0)
        p.addLayout(vol_row)
        self._volume_mirrors.append(slider)

        sel = QVBoxLayout()
        sel.setSpacing(2)
        sel.addWidget(QLabel("目前選取", objectName="eyebrow"))
        w["sel_title"] = ElideLabel("尚未選擇")
        w["sel_title"].setObjectName("nowTitle")
        sel.addWidget(w["sel_title"])
        w["sel_sub"] = ElideLabel("")
        w["sel_sub"].setObjectName("nowPath")
        sel.addWidget(w["sel_sub"])
        p.addLayout(sel)
        p.addStretch(1)

        w["play"] = QPushButton("▶  播放")
        w["play"].setObjectName("primary")
        w["play"].setFixedHeight(44)
        w["play"].setCursor(Qt.CursorShape.PointingHandCursor)
        p.addWidget(w["play"])
        if extras:
            row = QHBoxLayout()
            row.setSpacing(12)
            for key, text, tip in extras:
                button = QPushButton(text)
                button.setFixedHeight(38)
                button.setToolTip(tip)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                w[key] = button
                row.addWidget(button)
            p.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(12)
        w["pause"] = QPushButton("⏸  暫停")
        w["pause"].setObjectName("pauseBtn")
        w["pause"].setFixedHeight(40)
        w["pause"].setEnabled(False)
        w["pause"].setCursor(Qt.CursorShape.PointingHandCursor)
        w["pause"].clicked.connect(self.toggle_pause)
        row.addWidget(w["pause"], 1)
        w["stop"] = QPushButton("■  停止並關閉發話")
        w["stop"].setObjectName("danger")
        w["stop"].setFixedHeight(40)
        w["stop"].setCursor(Qt.CursorShape.PointingHandCursor)
        w["stop"].clicked.connect(lambda: self.stop())
        row.addWidget(w["stop"], 1)
        p.addLayout(row)
        self._panels.append(w)
        return w

    def _online_list(self):
        """List + empty/loading message, built like the library list. Returns (stack, list, message label)."""
        lst = TrackListWidget()
        lst.setObjectName("trackList")
        lst.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        empty = QLabel()
        empty.setObjectName("emptyState")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setWordWrap(True)
        stack = QStackedWidget()
        stack.setObjectName("row")
        stack.addWidget(lst)
        stack.addWidget(empty)
        stack.setMinimumWidth(320)
        return stack, lst, empty

    def _fill_online_list(self, stack, lst, empty, rows, message, fav_keys):
        """rows = [(key, data dict, title, sub tag)]. Shows `message` instead when there is nothing."""
        current = lst.currentItem()
        keep = current.data(ROLE_PATH) if current is not None else None
        lst.setUpdatesEnabled(False)
        lst.clear()
        width = max(2, len(str(len(rows))))
        src = self.current_source
        for index, (key, data, title, sub) in enumerate(rows, 1):
            item = QListWidgetItem()
            item.setData(ROLE_PATH, key)
            item.setData(ROLE_NAME, title)
            item.setData(ROLE_SEARCH, f"{title} {sub}".lower())
            item.setData(ROLE_NUM, f"{index:0{width}d}")
            item.setData(ROLE_FAV, key in fav_keys)
            item.setData(ROLE_SUB, sub)
            item.setData(ROLE_DATA, data)
            item.setData(ROLE_PLAYING, bool(src) and src["key"] == key)
            lst.addItem(item)
            if key == keep:
                lst.setCurrentItem(item)
        lst.setUpdatesEnabled(True)
        if rows and lst.currentItem() is None:
            lst.setCurrentRow(0)
        empty.setText(message)
        stack.setCurrentIndex(0 if rows else 1)

    # ---------------------------------------------------------------- 電台 page
    def radio_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 4, 10, 4)
        outer.setSpacing(12)
        header = QHBoxLayout()
        title_wrap = QVBoxLayout()
        title_wrap.setSpacing(4)
        title_wrap.addWidget(QLabel("網路電台", objectName="title"))
        sub = QLabel("國內外的純音樂電台：氛圍、Lofi、古典、爵士……點兩下就會送進遊戲語音。")
        sub.setObjectName("subtitle")
        title_wrap.addWidget(sub)
        header.addLayout(title_wrap)
        header.addStretch()
        self.radio_add_button = QPushButton("➕  新增電台網址")
        self.radio_add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.radio_add_button.setToolTip("加入你自己知道的電台串流網址（mp3 / aac / m3u8 / pls），會存到最愛")
        self.radio_add_button.clicked.connect(self.radio_add_custom)
        header.addWidget(self.radio_add_button, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        self.radio_note = QLabel(self.RADIO_NOTE)
        self.radio_note.setObjectName("tipBar")
        self.radio_note.setWordWrap(True)
        outer.addWidget(self.radio_note)

        tools = QHBoxLayout()
        tools.setSpacing(12)
        tools.addLayout(self._segmented("電台", "★  最愛", self.settings.radio_view, self.radio_set_view))
        self.radio_search = QLineEdit()
        self.radio_search.setPlaceholderText("🔍  輸入電台名稱，按 Enter 搜尋…")
        self.radio_search.setClearButtonEnabled(True)
        self.radio_search.returnPressed.connect(self.radio_reload)
        tools.addWidget(self.radio_search, 1)
        self.radio_country = QComboBox()
        for label, code in RADIO_COUNTRIES:
            self.radio_country.addItem(f"🌐  {label}" if not code else f"📍  {label}", code)
        self.radio_country.setCurrentIndex(max(0, self.radio_country.findData(self.settings.radio_country)))
        self.radio_country.setMinimumWidth(140)
        self.radio_country.currentIndexChanged.connect(self.radio_country_changed)
        tools.addWidget(self.radio_country)
        outer.addLayout(tools)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.radio_chips = {}
        chip_group = QButtonGroup(self)
        chip_group.setExclusive(True)
        for label, value in RADIO_GENRES:
            chip = self._chip(label)
            chip_group.addButton(chip)
            chip.clicked.connect(lambda _=False, v=value: self.radio_genre_clicked(v))
            self.radio_chips[value] = chip
            chips.addWidget(chip)
        chips.addStretch()
        self.radio_chips.get(self.settings.radio_genre, self.radio_chips["ambient"]).setChecked(True)
        outer.addLayout(chips)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.radio_stack, self.radio_rows, self.radio_empty = self._online_list()
        self.radio_rows.itemDoubleClicked.connect(lambda _: self.radio_play())
        self.radio_rows.currentItemChanged.connect(self._radio_selection)
        self.radio_rows.star_clicked.connect(lambda item: self.radio_toggle_fav(item.data(ROLE_DATA)))
        self.radio_rows.customContextMenuRequested.connect(self.radio_menu)
        body.addWidget(self.radio_stack, 1)
        self.radio_panel = self._now_panel([("random", "🎲  隨機一台", "從目前清單隨機挑一台播放"),
                                            ("fav", "★  加入最愛", "加入／移出最愛")])
        self.radio_panel["play"].setText("▶  播放電台")
        self.radio_panel["play"].clicked.connect(self.radio_play)
        self.radio_panel["random"].clicked.connect(self.radio_random)
        self.radio_panel["fav"].clicked.connect(lambda: self.radio_toggle_fav(self._radio_current()))
        body.addWidget(self.radio_panel["frame"], 1)
        outer.addLayout(body, 1)
        self._radio_apply_view_state()
        return page

    def _station_sub(self, st, tags_shown=2):
        parts = []
        cc = st.get("cc") or ""
        if cc:
            parts.append(COUNTRY_NAMES.get(cc, cc))
        tags = [GENRE_NAMES.get(t, t) for t in (st.get("tags") or [])][:tags_shown]
        parts += tags
        if st.get("bitrate"):
            parts.append(f"{st['bitrate']}k")
        return " · ".join(parts)

    def _radio_fav_keys(self):
        return {d["urls"][0] for d in self.settings.radio_favorites if d.get("urls")}

    def _radio_current(self):
        item = self.radio_rows.currentItem()
        return item.data(ROLE_DATA) if item is not None else None

    def _radio_selection(self, item, _prev=None):
        w = self.radio_panel
        st = item.data(ROLE_DATA) if item is not None else None
        w["sel_title"].setText(st["name"] if st else "尚未選擇")
        w["sel_sub"].setText(self._station_sub(st) if st else "")
        faved = bool(st) and st["urls"][0] in self._radio_fav_keys()
        w["fav"].setText("★  移出最愛" if faved else "★  加入最愛")
        w["fav"].setEnabled(bool(st))
        w["play"].setEnabled(bool(st))

    def _radio_apply_view_state(self):
        fav = self.settings.radio_view == "fav"
        for chip in self.radio_chips.values():
            chip.setEnabled(not fav)
        self.radio_country.setEnabled(not fav)
        self.radio_search.setPlaceholderText("🔍  在最愛裡篩選…" if fav else "🔍  輸入電台名稱，按 Enter 搜尋…")

    def radio_set_view(self, mode):
        self.settings.radio_view = mode
        self.save_settings()
        self._radio_apply_view_state()
        self.radio_reload()

    def radio_genre_clicked(self, value):
        self.settings.radio_genre = value
        self.radio_search.clear()
        self.save_settings()
        self.radio_reload()

    def radio_country_changed(self, _index):
        self.settings.radio_country = self.radio_country.currentData() or ""
        self.save_settings()
        self.radio_reload()

    def _radio_rows(self, stations):
        return [(st["urls"][0], st, st["name"], self._station_sub(st, 1)) for st in stations if st.get("urls")]

    def radio_reload(self):
        self._radio_token += 1
        token = self._radio_token
        text = self.radio_search.text().strip()
        if self.settings.radio_view == "fav":
            favs = [d for d in self.settings.radio_favorites if d.get("urls")]
            if text:
                favs = [d for d in favs if text.lower() in d.get("name", "").lower()]
            self._fill_online_list(self.radio_stack, self.radio_rows, self.radio_empty, self._radio_rows(favs),
                                   "還沒有最愛的電台。\n\n在「電台」分頁按一下列表左邊的 ☆，或用右上角「新增電台網址」加入。",
                                   self._radio_fav_keys())
            self._radio_selection(self.radio_rows.currentItem())
            return
        self.radio_empty.setText("搜尋電台中…")
        self.radio_stack.setCurrentIndex(1)
        cc, tag = self.settings.radio_country, ("" if text else self.settings.radio_genre)
        threading.Thread(target=self._radio_worker, args=(token, text, cc, tag), daemon=True).start()

    def _radio_worker(self, token, text, cc, tag):
        try:
            curated = curated_matches(text, cc, tag)
            online, err = [], ""
            try:
                online = radio_query(text, cc, tag)
            except Exception as exc:
                err = str(exc) or exc.__class__.__name__
            seen = {re.sub(r"\W+", "", s["name"].lower()) for s in curated}
            merged = curated + [s for s in online if re.sub(r"\W+", "", s["name"].lower()) not in seen]
            self.signals.radio_results.emit(token, merged, "" if merged else err)
        except Exception as exc:
            self.signals.radio_results.emit(token, [], str(exc) or exc.__class__.__name__)

    def _on_radio_results(self, token, stations, err):
        if token != self._radio_token or self.settings.radio_view == "fav":
            return
        if err:
            message = f"{err}\n\n請檢查網路後，再按一次上方的類型或國家。"
        else:
            message = ("這個國家／類型沒有找到電台。\n\n試試「全球」、換一個類型，或直接輸入電台名稱搜尋。")
        self._fill_online_list(self.radio_stack, self.radio_rows, self.radio_empty, self._radio_rows(stations),
                               message, self._radio_fav_keys())
        self._radio_selection(self.radio_rows.currentItem())

    def _radio_src(self, st):
        return {"kind": "radio", "key": st["urls"][0], "title": st["name"], "sub": self._station_sub(st),
                "urls": list(st["urls"]), "live": True}

    def radio_play(self):
        st = self._radio_current()
        if st:
            self.play_source(self._radio_src(st))

    def radio_random(self):
        count = self.radio_rows.count()
        if not count:
            return
        row = random.randrange(count)
        self.radio_rows.setCurrentRow(row)
        self.radio_play()

    def radio_toggle_fav(self, st):
        if not st or not st.get("urls"):
            return
        key = st["urls"][0]
        favs = self.settings.radio_favorites
        if key in self._radio_fav_keys():
            self.settings.radio_favorites = [d for d in favs if not (d.get("urls") and d["urls"][0] == key)]
        else:
            favs.append({k: st.get(k) for k in ("name", "urls", "cc", "tags", "bitrate", "custom") if k in st})
        self.save_settings()
        if self.settings.radio_view == "fav":
            self.radio_reload()
        else:
            fav_keys = self._radio_fav_keys()
            for row in range(self.radio_rows.count()):
                item = self.radio_rows.item(row)
                item.setData(ROLE_FAV, item.data(ROLE_PATH) in fav_keys)
            self.radio_rows.viewport().update()
            self._radio_selection(self.radio_rows.currentItem())

    def radio_add_custom(self):
        url, ok = QInputDialog.getText(self, "新增電台", "電台串流網址（http / https）：")
        url = url.strip()
        if not ok or not url:
            return
        if not re.match(r"^https?://", url, re.I):
            QMessageBox.warning(self, APP_NAME, "網址要以 http:// 或 https:// 開頭。")
            return
        host = urllib.parse.urlparse(url).netloc
        name, ok = QInputDialog.getText(self, "新增電台", "電台名稱：", text=host)
        if not ok:
            return
        name = name.strip() or host or url
        if url not in self._radio_fav_keys():
            self.settings.radio_favorites.append({"name": name, "urls": [url], "cc": "", "tags": [], "custom": True})
            self.save_settings()
        self.settings.radio_view = "fav"
        self.save_settings()
        self.radio_set_view_buttons()
        self._radio_apply_view_state()
        self.radio_reload()

    def radio_set_view_buttons(self):
        """Re-check the 電台/最愛 segmented buttons after the view was changed from code."""
        for button in self.pages.widget(5).findChildren(QPushButton):
            if button.objectName() == "segL":
                button.setChecked(self.settings.radio_view != "fav")
            elif button.objectName() == "segR":
                button.setChecked(self.settings.radio_view == "fav")

    def radio_menu(self, pos):
        item = self.radio_rows.itemAt(pos)
        if item is None:
            return
        st = item.data(ROLE_DATA)
        menu = QMenu(self)
        menu.addAction("▶  播放", lambda: (self.radio_rows.setCurrentItem(item), self.radio_play()))
        faved = st["urls"][0] in self._radio_fav_keys()
        menu.addAction("移出最愛" if faved else "★  加入最愛", lambda: self.radio_toggle_fav(st))
        menu.addAction("複製串流網址", lambda: QApplication.clipboard().setText(st["urls"][0]))
        menu.exec(self.radio_rows.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ YouTube page
    def youtube_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(10, 4, 10, 4)
        outer.setSpacing(12)
        header = QHBoxLayout()
        title_wrap = QVBoxLayout()
        title_wrap.setSpacing(4)
        title_wrap.addWidget(QLabel("YouTube 音樂", objectName="title"))
        sub = QLabel("貼上影片、播放清單或直播網址，或直接輸入關鍵字搜尋。")
        sub.setObjectName("subtitle")
        title_wrap.addWidget(sub)
        header.addLayout(title_wrap)
        header.addStretch()
        self.yt_tools_button = QPushButton("🔧  YouTube 元件")
        self.yt_tools_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.yt_tools_button.setToolTip("下載／更新 yt-dlp 與 Deno；YouTube 要求驗證時可在這裡選瀏覽器 Cookies")
        self.yt_tools_button.clicked.connect(self.yt_tools_dialog)
        header.addWidget(self.yt_tools_button, 0, Qt.AlignmentFlag.AlignTop)
        folder_button = QPushButton("📂  最愛資料夾")
        folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        folder_button.setToolTip("加入最愛的 YouTube 歌曲會存成檔案放在這裡，也可以把它加進音樂庫")
        folder_button.clicked.connect(self.yt_open_fav_folder)
        header.addWidget(folder_button, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        self.yt_note = QLabel(self._yt_default_note())
        self.yt_note.setObjectName("tipBar")
        self.yt_note.setWordWrap(True)
        outer.addWidget(self.yt_note)

        tools = QHBoxLayout()
        tools.setSpacing(12)
        tools.addLayout(self._segmented("搜尋結果", "★  最愛", "all", self.yt_set_view))
        self.yt_search = QLineEdit()
        self.yt_search.setPlaceholderText("🔍  貼上 YouTube 網址，或輸入關鍵字，按 Enter…")
        self.yt_search.setClearButtonEnabled(True)
        self.yt_search.returnPressed.connect(self.yt_submit)
        tools.addWidget(self.yt_search, 1)
        go = QPushButton("搜尋")
        go.setCursor(Qt.CursorShape.PointingHandCursor)
        go.clicked.connect(self.yt_submit)
        tools.addWidget(go)
        self.yt_after_combo = QComboBox()
        self.yt_after_combo.addItem("播完後：自動下一首", "next")
        self.yt_after_combo.addItem("播完後：單曲循環", "loop")
        self.yt_after_combo.addItem("播完後：停止", "stop")
        self.yt_after_combo.setCurrentIndex(max(0, self.yt_after_combo.findData(self.settings.yt_after)))
        self.yt_after_combo.currentIndexChanged.connect(self.yt_after_changed)
        self.yt_after_combo.setMinimumWidth(170)
        tools.addWidget(self.yt_after_combo)
        outer.addLayout(tools)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        for label, query in YT_SUGGESTIONS:
            chip = self._chip(label, checkable=False)
            chip.clicked.connect(lambda _=False, q=query: (self.yt_search.setText(q), self.yt_submit()))
            chips.addWidget(chip)
        chips.addStretch()
        outer.addLayout(chips)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.yt_stack, self.yt_rows, self.yt_empty = self._online_list()
        self.yt_empty.setText("在上方貼上 YouTube 網址，或輸入關鍵字（例如「lofi 純音樂」）開始。\n\n也可以直接點上面的快速搜尋。")
        self.yt_stack.setCurrentIndex(1)
        self.yt_rows.itemDoubleClicked.connect(lambda _: self.yt_play())
        self.yt_rows.currentItemChanged.connect(self._yt_selection)
        self.yt_rows.star_clicked.connect(lambda item: self.yt_toggle_fav(item.data(ROLE_DATA)))
        self.yt_rows.customContextMenuRequested.connect(self.yt_menu)
        body.addWidget(self.yt_stack, 1)
        self.yt_panel = self._now_panel([("next", "⏭  下一首", "播放清單裡的下一首"),
                                         ("fav", "★  加入最愛", "加入／移出最愛")])
        self.yt_panel["play"].setText("▶  播放")
        self.yt_panel["play"].clicked.connect(self.yt_play)
        self.yt_panel["next"].clicked.connect(self.yt_next)
        self.yt_panel["fav"].clicked.connect(lambda: self.yt_toggle_fav(self._yt_current()))
        body.addWidget(self.yt_panel["frame"], 1)
        outer.addLayout(body, 1)
        self._yt_results_cache = []
        self._yt_view = "all"
        return page

    def _yt_current(self):
        item = self.yt_rows.currentItem()
        return item.data(ROLE_DATA) if item is not None else None

    def _yt_fav_keys(self):
        return {d.get("id") or d.get("url") for d in self.settings.yt_favorites}

    @staticmethod
    def _yt_key(entry):
        return entry.get("id") or entry.get("url")

    def _yt_fav_entry(self, key):
        for d in self.settings.yt_favorites:
            if self._yt_key(d) == key:
                return d
        return None

    def _yt_sub(self, e):
        parts = [e.get("channel") or ""]
        parts.append("🔴 直播" if e.get("live") else fmt_duration(e.get("duration")))
        key = self._yt_key(e)
        if key in self._yt_jobs:
            parts.append("⬇ 下載中")
        elif yt_fav_file(self._yt_fav_entry(key)):
            parts.append("✓ 已存檔")
        return " · ".join(x for x in parts if x)

    def _yt_refresh_rows(self):
        for row in range(self.yt_rows.count()):
            item = self.yt_rows.item(row)
            item.setData(ROLE_SUB, self._yt_sub(item.data(ROLE_DATA)))
            item.setData(ROLE_FAV, item.data(ROLE_PATH) in self._yt_fav_keys())
        self.yt_rows.viewport().update()
        self._yt_selection(self.yt_rows.currentItem())

    def _yt_rows(self, entries):
        return [(self._yt_key(e), e, e.get("title") or "（未命名）", self._yt_sub(e)) for e in entries]

    def _yt_selection(self, item, _prev=None):
        w = self.yt_panel
        e = item.data(ROLE_DATA) if item is not None else None
        w["sel_title"].setText(e.get("title", "") if e else "尚未選擇")
        w["sel_sub"].setText(self._yt_sub(e) if e else "")
        faved = bool(e) and self._yt_key(e) in self._yt_fav_keys()
        w["fav"].setText("★  移出最愛" if faved else "★  加入最愛")
        w["fav"].setEnabled(bool(e))
        w["play"].setEnabled(bool(e))

    def yt_set_view(self, mode):
        self._yt_view = mode
        if mode == "fav":
            favs = list(self.settings.yt_favorites)
            self._fill_online_list(self.yt_stack, self.yt_rows, self.yt_empty, self._yt_rows(favs),
                                   "還沒有最愛的影片。\n\n搜尋後按一下列表左邊的 ☆ 就能收藏。", self._yt_fav_keys())
        else:
            self._fill_online_list(self.yt_stack, self.yt_rows, self.yt_empty, self._yt_rows(self._yt_results_cache),
                                   "在上方貼上 YouTube 網址，或輸入關鍵字開始。", self._yt_fav_keys())
        self._yt_selection(self.yt_rows.currentItem())

    def yt_after_changed(self, _index):
        self.settings.yt_after = self.yt_after_combo.currentData() or "next"
        self.save_settings()

    def yt_submit(self):
        text = self.yt_search.text().strip()
        if not text:
            return
        if not ytdlp_available():
            self._set_note("yt", "⚠  尚未安裝 YouTube 元件，請按右上角「🔧  YouTube 元件」下載。")
            self.yt_tools_dialog()
            return
        self._yt_token += 1
        token = self._yt_token
        self._yt_view = "all"
        for button in self.pages.widget(6).findChildren(QPushButton):
            if button.objectName() == "segL":
                button.setChecked(True)
        self.yt_empty.setText("搜尋中…（第一次可能需要幾秒）")
        self.yt_stack.setCurrentIndex(1)
        self._set_note("yt", "")
        threading.Thread(target=self._yt_worker, args=(token, text), daemon=True).start()

    def _yt_worker(self, token, text):
        try:
            result = yt_list(text, self.settings.yt_cookies_browser)
            self.signals.yt_results.emit(token, result, "")
        except Exception as exc:
            self.signals.yt_results.emit(token, None, str(exc) or exc.__class__.__name__)

    def _on_yt_results(self, token, result, err):
        if token != self._yt_token:
            return
        if err or not result:
            self._yt_results_cache = []
            self.yt_set_view("all")
            self.yt_empty.setText(f"⚠  {err or '沒有結果'}")
            self.yt_stack.setCurrentIndex(1)
            return
        self._yt_results_cache = result["entries"]
        self.yt_set_view("all")
        if result["autoplay"] and result["entries"]:
            self.yt_rows.setCurrentRow(0)
            self.yt_play()

    def _yt_src(self, e):
        return {"kind": "yt", "key": self._yt_key(e), "title": e.get("title") or "（未命名）",
                "sub": e.get("channel") or "", "page_url": e["url"], "live": bool(e.get("live")),
                "duration": e.get("duration") or 0}

    def yt_play(self):
        e = self._yt_current()
        if not e:
            return
        if not ytdlp_available():
            self.yt_tools_dialog()
            return
        # The list on screen at this moment becomes the queue that 下一首 / 自動下一首 walk through.
        self._yt_queue = [self.yt_rows.item(r).data(ROLE_DATA) for r in range(self.yt_rows.count())]
        self.play_source(self._yt_src(e))

    def _yt_next_src(self, src):
        queue = self._yt_queue
        if len(queue) < 2:
            return None
        keys = [self._yt_key(e) for e in queue]
        index = (keys.index(src["key"]) + 1) % len(queue) if src["key"] in keys else 0
        return self._yt_src(queue[index])

    def yt_next(self):
        src = self.current_source if self.current_source and self.current_source["kind"] == "yt" else None
        if src is None:
            self.yt_play()
            return
        nxt = self._yt_next_src(src)
        if nxt:
            self.yt_select_key(nxt["key"])
            self.play_source(nxt)

    def _yt_update_row(self, src):
        """A pasted link only has its URL as title; show the real title / channel once it is known."""
        for entry in self._yt_results_cache:
            if self._yt_key(entry) == src["key"]:
                entry["title"], entry["channel"] = src["title"], src.get("sub", "")
                entry["live"] = bool(src.get("live"))
        for row in range(self.yt_rows.count()):
            item = self.yt_rows.item(row)
            if item.data(ROLE_PATH) == src["key"]:
                data = dict(item.data(ROLE_DATA))
                data["title"], data["channel"], data["live"] = src["title"], src.get("sub", ""), bool(src.get("live"))
                item.setData(ROLE_DATA, data)
                item.setData(ROLE_NAME, data["title"])
                item.setData(ROLE_SUB, self._yt_sub(data))
        self.yt_rows.viewport().update()
        self._yt_selection(self.yt_rows.currentItem())

    def yt_select_key(self, key):
        for row in range(self.yt_rows.count()):
            if self.yt_rows.item(row).data(ROLE_PATH) == key:
                self.yt_rows.setCurrentRow(row)
                return

    def yt_toggle_fav(self, e):
        if not e:
            return
        key = self._yt_key(e)
        entry = self._yt_fav_entry(key)
        if entry is not None:                    # remove: stop its download and delete the saved file
            job = self._yt_jobs.pop(key, None)
            if job is not None:
                job.cancel()
            path = yt_fav_file(entry)
            if path and Path(path).parent.resolve() in [d.resolve() for d in yt_fav_dirs()]:
                try:
                    Path(path).unlink()
                except OSError:
                    pass
            self.settings.yt_favorites = [d for d in self.settings.yt_favorites if self._yt_key(d) != key]
            self.save_settings()
        else:
            entry = {k: e.get(k) for k in ("id", "title", "url", "channel", "duration", "live")}
            entry["file"] = ""
            self.settings.yt_favorites.append(entry)
            self.save_settings()
            self.yt_download_fav(entry)
        if self._yt_view == "fav":
            self.yt_set_view("fav")
        else:
            self._yt_refresh_rows()

    def yt_download_fav(self, entry):
        """Save a favourite as a real file (YouTube最愛 folder). Live streams cannot be saved."""
        key = self._yt_key(entry)
        if entry.get("live"):
            self._set_note("yt", "直播無法存成檔案，只加入最愛清單。")
            return
        if key in self._yt_jobs or yt_fav_file(entry):
            return
        if not ytdlp_available():
            self._set_note("yt", "已加入最愛，但尚未安裝 YouTube 元件，所以還沒存檔。")
            return
        self._yt_jobs[key] = YtFavJob(entry, self.settings.yt_cookies_browser,
                                      lambda k, err: self.signals.tool_done.emit(f"fav:{k}", err))
        self._set_note("yt", f"⬇  正在把「{entry.get('title', '')}」存到最愛資料夾…")

    def yt_open_fav_folder(self):
        folder = yt_fav_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---- 設定：YouTube 最愛存放位置 ----
    def refresh_yt_dir_ui(self):
        folder = yt_fav_dir()
        self.yt_dir_edit.setText(str(folder))
        self.yt_dir_edit.setToolTip(str(folder))
        self.yt_dir_edit.setCursorPosition(0)
        self.yt_dir_reset_button.setEnabled(bool(self.settings.yt_fav_dir))
        self.tools_dir_edit.setText(str(tools_dir()))
        self.tools_dir_edit.setToolTip(str(tools_dir()))
        self.tools_dir_edit.setCursorPosition(0)
        self.tools_dir_reset_button.setEnabled(bool(self.settings.tools_dir))

    # ---- 設定：YouTube 元件（tools）位置 ----
    def tools_choose_dir(self):
        current = tools_dir()
        start = str(current if current.is_dir() else current.parent if current.parent.is_dir() else Path.home())
        path = QFileDialog.getExistingDirectory(self, "選擇 YouTube 元件要存放的資料夾", start)
        if path:
            self.tools_set_dir(path)

    def tools_use_app_dir(self):
        self.tools_set_dir(str(app_home_dir() / "tools"))

    def tools_reset_dir(self):
        self.tools_set_dir("")

    def tools_set_dir(self, new):
        old = tools_dir()
        target = Path(new) if new else TOOLS_DIR_DEFAULT
        if path_key(target) == path_key(old):
            self.refresh_yt_dir_ui()
            return
        if getattr(self, "_yt_tool_busy", False) or self._yt_jobs:
            QMessageBox.information(self, APP_NAME, "YouTube 元件或最愛歌曲正在下載中，請等完成後再更換位置。")
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".echodeck_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"這個資料夾無法寫入，請換一個位置。\n\n{target}\n{exc}")
            return
        copied = []
        try:                                         # copy first; only switch when every file made it
            for name in ("yt-dlp.exe", "deno.exe"):
                src_file, dest = old / name, target / name
                if src_file.is_file() and not dest.is_file():
                    shutil.copy2(str(src_file), str(dest))
                    copied.append(dest)
        except OSError as exc:
            for dest in copied:
                try:
                    dest.unlink()
                except OSError:
                    pass
            QMessageBox.warning(self, APP_NAME, f"複製元件失敗，位置沒有更換。\n\n{exc}")
            return
        self.settings.tools_dir = "" if path_key(target) == path_key(TOOLS_DIR_DEFAULT) else str(target)
        _TOOLS_CUSTOM[0] = self.settings.tools_dir
        _ytdlp_ver[0] = None
        self.save_settings()
        leftovers = 0
        for name in ("yt-dlp.exe", "deno.exe"):      # tidy the old folder; a locked file just stays
            try:
                (old / name).unlink(missing_ok=True)
            except OSError:
                leftovers += 1
        self.refresh_yt_dir_ui()
        text = f"✓  YouTube 元件之後會放在：{target}"
        if leftovers:
            text += "（舊位置有檔案被佔用沒刪掉，可以之後手動刪除）"
        self.subtitle.setText(text)

    def yt_choose_fav_dir(self):
        current = yt_fav_dir()
        start = str(current if current.is_dir() else current.parent if current.parent.is_dir() else Path.home())
        path = QFileDialog.getExistingDirectory(self, "選擇 YouTube 最愛要存放的資料夾", start)
        if path:
            self.yt_set_fav_dir(path)

    def yt_use_app_fav_dir(self):
        self.yt_set_fav_dir(str(app_home_dir() / "YouTube最愛"))

    def yt_reset_fav_dir(self):
        self.yt_set_fav_dir("")

    def yt_set_fav_dir(self, new):
        old = yt_fav_dir()
        target = Path(new) if new else YT_FAV_DIR_DEFAULT
        if path_key(target) == path_key(old):
            self.refresh_yt_dir_ui()
            return
        if self._yt_jobs:
            QMessageBox.information(self, APP_NAME, "還有最愛歌曲正在下載中，請等下載完成後再更換存放位置。")
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".echodeck_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"這個資料夾無法寫入，請換一個位置。\n\n{target}\n{exc}")
            return
        movable = [e for e in self.settings.yt_favorites
                   if yt_fav_file(e) and path_key(Path(e["file"]).parent) == path_key(old)]
        move = False
        if movable:
            answer = QMessageBox.question(
                self, APP_NAME,
                f"要把已經存好的 {len(movable)} 首歌，從\n{old}\n搬到新位置嗎？\n\n{target}\n\n"
                "「是」＝一起搬過去（最愛清單不會掉）\n「否」＝舊檔留在原處，之後新加的最愛才存到新位置",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes)
            if answer == QMessageBox.StandardButton.Cancel:
                return
            move = answer == QMessageBox.StandardButton.Yes
        moved = failed = 0
        if move:
            for entry in movable:
                src_file = Path(entry["file"])
                dest = target / src_file.name
                n = 2
                while dest.exists():
                    dest = target / f"{src_file.stem} ({n}){src_file.suffix}"
                    n += 1
                try:
                    shutil.move(str(src_file), str(dest))
                    entry["file"] = str(dest)
                    moved += 1
                except OSError:
                    failed += 1                     # e.g. the song is playing right now
        self.settings.yt_fav_dir = "" if path_key(target) == path_key(YT_FAV_DIR_DEFAULT) else str(target)
        _YT_FAV_CUSTOM[0] = self.settings.yt_fav_dir
        self.save_settings()
        self.refresh_yt_dir_ui()
        if getattr(self, "_yt_view", "") == "fav":
            self.yt_set_view("fav")
        elif hasattr(self, "_yt_refresh_rows"):
            self._yt_refresh_rows()
        text = f"✓  YouTube 最愛之後會存到：{target}"
        if move:
            text += f"（已搬移 {moved} 首" + (f"，{failed} 首搬不動，請先停止播放再試" if failed else "") + "）"
        self.subtitle.setText(text)

    def yt_menu(self, pos):
        item = self.yt_rows.itemAt(pos)
        if item is None:
            return
        e = item.data(ROLE_DATA)
        menu = QMenu(self)
        menu.addAction("▶  播放", lambda: (self.yt_rows.setCurrentItem(item), self.yt_play()))
        faved = self._yt_key(e) in self._yt_fav_keys()
        menu.addAction("移出最愛（並刪除檔案）" if faved else "★  加入最愛（並存檔）", lambda: self.yt_toggle_fav(e))
        fav_entry = self._yt_fav_entry(self._yt_key(e))
        if fav_entry is not None and not yt_fav_file(fav_entry) and self._yt_key(e) not in self._yt_jobs:
            menu.addAction("⬇  存成檔案", lambda: (self.yt_download_fav(fav_entry), self._yt_refresh_rows()))
        menu.addAction("在瀏覽器開啟", lambda: QDesktopServices.openUrl(QUrl(e["url"])))
        menu.addAction("複製連結", lambda: QApplication.clipboard().setText(e["url"]))
        menu.exec(self.yt_rows.viewport().mapToGlobal(pos))

    # --------------------------------------------------- YouTube components dialog
    def yt_tools_dialog(self):
        if getattr(self, "_yt_dialog", None) is not None:
            self._yt_dialog.raise_()
            self._yt_dialog.activateWindow()
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("YouTube 元件")
        dlg.setMinimumWidth(520)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(26, 22, 26, 22)
        v.setSpacing(12)
        v.addWidget(QLabel("YouTube 元件", objectName="sectionTitle"))
        intro = QLabel("YouTube 需要兩個小工具才能取得音訊（官方 yt-dlp 與 Deno）。都只需下載一次，之後 YouTube 改版造成搜尋或播放失敗時，再按「更新」即可。")
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        v.addWidget(intro)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(1, 1)
        names = QLabel("yt-dlp（解析 YouTube）")
        self._yt_ver_label = QLabel()
        self._yt_ver_label.setObjectName("muted")
        self._yt_ytdlp_button = QPushButton()
        self._yt_ytdlp_button.clicked.connect(lambda: self._yt_run_tool("ytdlp"))
        grid.addWidget(names, 0, 0)
        grid.addWidget(self._yt_ver_label, 0, 1)
        grid.addWidget(self._yt_ytdlp_button, 0, 2)
        names2 = QLabel("Deno（JavaScript 環境）")
        self._yt_deno_label = QLabel()
        self._yt_deno_label.setObjectName("muted")
        self._yt_deno_button = QPushButton()
        self._yt_deno_button.clicked.connect(lambda: self._yt_run_tool("deno"))
        grid.addWidget(names2, 1, 0)
        grid.addWidget(self._yt_deno_label, 1, 1)
        grid.addWidget(self._yt_deno_button, 1, 2)
        v.addLayout(grid)
        cookie_row = QHBoxLayout()
        cookie_row.addWidget(QLabel("Cookies 來源"))
        self._yt_cookie_combo = QComboBox()
        self._yt_cookie_combo.addItem("不使用（預設）", "")
        for label, value in (("Chrome", "chrome"), ("Edge", "edge"), ("Firefox", "firefox"), ("Brave", "brave")):
            self._yt_cookie_combo.addItem(label, value)
        self._yt_cookie_combo.setCurrentIndex(max(0, self._yt_cookie_combo.findData(self.settings.yt_cookies_browser)))
        self._yt_cookie_combo.currentIndexChanged.connect(self._yt_cookie_changed)
        cookie_row.addWidget(self._yt_cookie_combo, 1)
        v.addLayout(cookie_row)
        cookie_note = QLabel("只有出現「請證明你不是機器人」之類的驗證訊息時才需要。選擇你平常登入 YouTube 的瀏覽器，EchoDeck 只會用它讀取登入狀態。")
        cookie_note.setObjectName("hint")
        cookie_note.setWordWrap(True)
        v.addWidget(cookie_note)
        self._yt_tool_status = QLabel("")
        self._yt_tool_status.setObjectName("muted")
        self._yt_tool_status.setWordWrap(True)
        v.addWidget(self._yt_tool_status)
        close = QPushButton("關閉")
        close.clicked.connect(dlg.accept)
        v.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dlg.finished.connect(lambda _=0: setattr(self, "_yt_dialog", None))
        self._yt_dialog = dlg
        self._yt_tools_refresh()
        dlg.open()

    def _yt_tools_refresh(self):
        if getattr(self, "_yt_dialog", None) is None:
            return
        have = ytdlp_available()
        ver = _ytdlp_ver[0]
        if have and ver is None:
            self._yt_ver_label.setText("已安裝　（讀取版本中…）")
            threading.Thread(target=lambda: (ytdlp_version(), self.signals.tool_done.emit("version", "")),
                             daemon=True).start()
        elif have:
            self._yt_ver_label.setText(f"已安裝　{ver}" if ver else "已安裝")
        else:
            self._yt_ver_label.setText("尚未安裝")
        self._yt_ytdlp_button.setText("更新" if have else "下載")
        runtime = find_js_runtime()
        self._yt_deno_label.setText(f"已安裝　{runtime[0]}" if runtime else "尚未安裝")
        self._yt_deno_button.setText("更新" if deno_exe().is_file() else "下載")

    def _yt_cookie_changed(self, _index):
        self.settings.yt_cookies_browser = self._yt_cookie_combo.currentData() or ""
        self.save_settings()

    def _yt_run_tool(self, which):
        if self._yt_tool_busy:
            return
        self._yt_tool_busy = True
        for button in (self._yt_ytdlp_button, self._yt_deno_button):
            button.setEnabled(False)
        self._yt_tool_status.setText("下載中…請稍候" + ("（約 40 MB）" if which == "deno" else "（約 18 MB）"))
        threading.Thread(target=self._yt_tool_worker, args=(which,), daemon=True).start()

    def _yt_tool_worker(self, which):
        try:
            if which == "ytdlp":
                update_ytdlp()
            else:
                install_deno()
            self.signals.tool_done.emit(which, "")
        except Exception as exc:
            self.signals.tool_done.emit(which, str(exc) or exc.__class__.__name__)

    def _on_tool_done(self, which, err):
        if which.startswith("fav:"):             # a favourite finished downloading (or failed)
            key = which[4:]
            self._yt_jobs.pop(key, None)
            entry = self._yt_fav_entry(key)
            if entry is not None:
                self.save_settings()
                self._set_note("yt", f"⚠  存檔失敗：{err}" if err else f"✓  已存到「{Path(entry['file']).parent.name}」資料夾：{Path(entry['file']).name}")
            if self._yt_view == "fav":
                self.yt_set_view("fav")
            else:
                self._yt_refresh_rows()
            return
        if which == "version":              # only the version label needs a refresh
            self._yt_tools_refresh()
            return
        self._yt_tool_busy = False
        if getattr(self, "_yt_dialog", None) is not None:
            self._yt_tool_status.setText(f"⚠  下載失敗：{err}" if err else "✓  完成")
            for button in (self._yt_ytdlp_button, self._yt_deno_button):
                button.setEnabled(True)
            self._yt_tools_refresh()
        self._set_note("yt", "")

    # ------------------------------------------------------- shared playback path
    def play_source(self, src):
        """Play a radio station / YouTube video. Connecting happens on a worker thread (it can take
        seconds); the current sound keeps playing until the new one is ready, then PTT is handled
        exactly like a local song."""
        self._play_token += 1
        token = self._play_token
        self._starting = True
        self._pending_src = src
        self._set_note(src, "連線中…（網路串流第一次可能需要幾秒）")
        threading.Thread(target=self._stream_worker, args=(token, src), daemon=True).start()

    def _stream_worker(self, token, src):
        st, err = None, ""
        try:
            headers, urls = {}, []
            live, duration = bool(src.get("live")), src.get("duration") or 0
            saved = yt_fav_file(self._yt_fav_entry(src["key"])) if src["kind"] == "yt" else ""
            if saved:                          # favourite already saved: instant start and seekable
                st = self.mixer.open_stream(saved, self.settings.normalize_audio, live=False)
            elif src["kind"] == "yt":
                if live or not duration:        # unknown length / maybe live: ask yt-dlp first (a few seconds)
                    info = yt_resolve_audio(src["page_url"], self.settings.yt_cookies_browser)
                    if token != self._play_token:
                        return
                    urls, headers = [info["url"]], info["headers"]
                    live, duration = info["live"], info["duration"] or duration
                    if info["title"]:
                        src["title"] = info["title"]
                    if info["channel"]:
                        src["sub"] = info["channel"]
                    src["live"] = live
            else:
                for url in src["urls"]:
                    urls += resolve_playlist_url(url)
            last = None
            if st is not None:
                pass                           # opened from the saved favourite above
            elif src["kind"] == "yt" and not live:
                # A direct googlevideo link is refused (HTTP 403) when FFmpeg opens it: the link is tied to
                # the IP / client that yt-dlp used. So yt-dlp itself downloads the song and FFmpeg reads
                # the bytes from its output. Nothing to resolve first when the length is already known.
                if token != self._play_token:
                    return
                pipe = YtPipe(src["page_url"], self.settings.yt_cookies_browser)
                try:
                    st = self.mixer.open_stream(src["page_url"], self.settings.normalize_audio, live=False,
                                                duration=duration, opener=lambda: pipe)
                except Exception as exc:
                    detail = pipe.error_text()
                    pipe.close()
                    raise RuntimeError(_yt_friendly_error(detail) if detail.strip() else str(exc))
            else:
                for url in urls:
                    if token != self._play_token:
                        return
                    try:
                        st = self.mixer.open_stream(url, self.settings.normalize_audio, live=live,
                                                    headers=headers, duration=duration)
                        break
                    except Exception as exc:
                        last = exc
            if st is None:
                raise RuntimeError(f"無法連線（{last}）" if last else "沒有可用的串流網址")
        except Exception as exc:
            err = re.sub(r"https?://\S+", "…", str(exc) or exc.__class__.__name__)[:160]
        self.signals.stream_ready.emit(token, st, err)

    def _on_stream_ready(self, token, st, err):
        src = self._pending_src
        if token != self._play_token:           # stopped / replaced while connecting
            if st is not None:
                st.close()
            return
        if st is None:
            self._starting = False
            self._set_note(src, "⚠  " + (err or "無法播放"))
            return
        self.mixer.mic_hold = True              # mute the mic before PTT goes down, like local songs

        def go():
            if token != self._play_token:
                st.close()
                return
            self._starting = False
            self.mixer.install(st)
            self.current_track = None
            self.mark_playing(None)
            self.current_source = src
            self.effect_was_playing = True
            self.set_now(src["title"], src.get("sub", ""))
            self._set_note(src, "")
            self._refresh_source_marks()
            if src["kind"] == "yt":
                self._yt_update_row(src)
        if self.settings.auto_ptt:
            already_down = self.ptt_down
            self.press_ptt()
            QTimer.singleShot(0 if already_down else max(0, self.settings.ptt_delay_ms), go)
        else:
            go()

    def _source_ended(self, src):
        """A stream stopped by itself. True when something new was started."""
        if src["kind"] == "radio":
            self.set_now("電台連線中斷", "已多次嘗試重新連線仍失敗，請再按一次播放或換一台")
            return False
        mode = self.settings.yt_after
        if mode == "loop":
            self.play_source(src)
            return True
        if mode == "next":
            nxt = self._yt_next_src(src)
            if nxt:
                self.yt_select_key(nxt["key"])
                self.play_source(nxt)
                return True
        return False

    def _refresh_source_marks(self):
        src = self.current_source
        for kind, lst in (("radio", getattr(self, "radio_rows", None)), ("yt", getattr(self, "yt_rows", None))):
            if lst is None:
                continue
            key = src["key"] if src and src["kind"] == kind else None
            for row in range(lst.count()):
                item = lst.item(row)
                item.setData(ROLE_PLAYING, key is not None and item.data(ROLE_PATH) == key)
            lst.viewport().update()

    def _update_online_panels(self, playing, elapsed, total):
        live, status, meta = self.mixer.stream_info()

        def fmt(seconds):
            seconds = int(seconds)
            h, rest = divmod(seconds, 3600)
            return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"
        if live and playing:
            text = f"● LIVE  {fmt(elapsed)}"
        else:
            text = f"{fmt(elapsed)} / {fmt(total)}"
        self.time_label.setText(text)
        src = self.current_source
        if src is not None and playing:
            sub = status or (meta if live and meta else src.get("sub", ""))
            if sub != self.card_sub._full:
                self.set_sub(sub)
        paused = bool(playing and self.mixer.paused)
        pause_text = "▶  繼續" if paused else "⏸  暫停"
        for w in self._panels:
            w["time"].setText(text)
            if not w["progress"].isSliderDown():
                w["progress"].setValue(int(min(1.0, elapsed / total) * 1000) if total and not live else 0)
            w["progress"].setVisible(not live or not playing)
            w["progress"].setEnabled(bool(total) and not live and self.mixer.can_seek())
            w["pause"].setEnabled(bool(playing) and not live)
            if w["pause"].text() != pause_text:
                w["pause"].setText(pause_text)
                w["pause"].setProperty("resume", paused)
                restyle(w["pause"])

    # ---- auto clicker ----
    def clicker_target_changed(self, value):
        kind, _, name = value.partition(":")
        if kind == "mouse" and name and name == (self.settings.clicker_mouse_hotkey or "").partition(":")[2]:
            QMessageBox.warning(self, APP_NAME, "要連點的滑鼠鍵不能和「快捷鍵（滑鼠）」相同，請換一個。")
            self.click_target.value = self.settings.clicker_target
            self.click_target._refresh()
            return
        self.settings.clicker_target = value
        self.clicker.target = value
        self.save_settings()

    def clicker_interval_changed(self, value):
        value = max(AutoClicker.MIN_INTERVAL, value)
        self.settings.clicker_interval = value
        self.clicker.interval = value
        self.save_settings()

    def clicker_duration_changed(self, value):
        self.settings.clicker_duration = int(value)
        self.save_settings()

    def _begin_clicker(self):
        self.clicker.start()
        seconds = int(self.settings.clicker_duration)
        if seconds > 0:
            self._clicker_deadline.start(seconds * 1000)

    def stop_clicker(self):
        self._clicker_pending = False
        self._clicker_token += 1
        self._clicker_deadline.stop()
        self.clicker.stop()
        self.update_clicker_ui()

    def clicker_delay_changed(self, value):
        self.settings.clicker_delay = round(float(value), 1)
        self.save_settings()
        self.update_clicker_ui()

    # ---- presets: one preset = the click settings AND the hold settings ----
    @staticmethod
    def _preset_part(preset, part):
        """The "click" or "hold" half of a preset (None if it has none). Presets
        saved by the earlier clicker-only version are flat and count as "click"."""
        sub = preset.get(part)
        if isinstance(sub, dict):
            return sub
        if part == "click" and "target" in preset:
            return preset
        return None

    def refresh_clicker_presets(self, select=None):
        combo = self.preset_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("（選擇預設組合…）")
        for p in self.settings.clicker_presets:
            bits = []
            click, hold = self._preset_part(p, "click"), self._preset_part(p, "hold")
            if click:
                bits.append(f"連點 {target_display(click.get('target'))} 每 {float(click.get('interval', 0.1)):g} 秒")
            if hold:
                bits.append(f"壓住 {target_display(hold.get('target'))}")
            combo.addItem(p["name"] + ("　" + "　｜　".join(bits) if bits else ""))
        index = 0
        if select is not None:
            index = next((i + 1 for i, p in enumerate(self.settings.clicker_presets) if p["name"] == select), 0)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self.delete_preset_button.setEnabled(bool(self.settings.clicker_presets))

    def save_clicker_preset(self):
        name, ok = QInputDialog.getText(self, APP_NAME, "預設組合名稱：")
        name = name.strip()
        if not ok or not name:
            return
        preset = {
            "name": name,
            "click": {
                "target": self.settings.clicker_target,
                "interval": max(AutoClicker.MIN_INTERVAL, float(self.settings.clicker_interval)),
                "duration": int(self.settings.clicker_duration),
                "delay": round(float(self.settings.clicker_delay), 1),
            },
            "hold": {
                "target": self.settings.hold_target,
                "duration": int(self.settings.hold_duration),
                "delay": round(float(self.settings.hold_delay), 1),
            },
        }
        presets = self.settings.clicker_presets
        for i, old in enumerate(presets):
            if old["name"] == name:
                presets[i] = preset
                break
        else:
            presets.append(preset)
        self.save_settings()
        self.refresh_clicker_presets(select=name)
        self.click_status.setText(f"○ 已儲存預設：{name}")
        self.hold_status.setText(f"○ 已儲存預設：{name}")

    def apply_clicker_preset(self, index):
        presets = self.settings.clicker_presets
        if not (1 <= index <= len(presets)):
            return
        preset = presets[index - 1]
        click, hold = self._preset_part(preset, "click"), self._preset_part(preset, "hold")

        def mouse_name(target):
            kind, _, button = str(target or "").partition(":")
            return button if kind == "mouse" else ""
        # Same conflict rules as picking the keys by hand: a target may not be
        # the mouse button that is also used as that feature's hotkey.
        if click and mouse_name(click.get("target")) and \
                mouse_name(click.get("target")) == (self.settings.clicker_mouse_hotkey or "").partition(":")[2]:
            QMessageBox.warning(self, APP_NAME, "這組預設要連點的滑鼠鍵，和目前「連點快捷鍵（滑鼠）」相同，請先改掉其中一個。")
            self.refresh_clicker_presets()
            return
        if hold and mouse_name(hold.get("target")) and \
                mouse_name(hold.get("target")) == (self.settings.hold_mouse_hotkey or "").partition(":")[2]:
            QMessageBox.warning(self, APP_NAME, "這組預設要壓住的滑鼠鍵，和目前「壓住快捷鍵（滑鼠）」相同，請先改掉其中一個。")
            self.refresh_clicker_presets()
            return
        self.stop_clicker()  # never change a target under a running / counting-down action
        self.stop_hold()
        if click:
            target = str(click.get("target") or "mouse:left")
            self.click_target.value = target
            self.click_target._refresh()
            self.settings.clicker_target = target
            self.clicker.target = target
            self.click_interval.setValue(max(AutoClicker.MIN_INTERVAL, float(click.get("interval", 0.1))))
            self.click_duration.setValue(max(0, int(click.get("duration", 60))))
            self.click_delay.setValue(min(3.0, max(0.5, float(click.get("delay", 3.0)))))
        if hold:
            target = str(hold.get("target") or "key:v")
            self.hold_target_edit.value = target
            self.hold_target_edit._refresh()
            self.settings.hold_target = target
            self.hold_action.target = target
            self.hold_duration.setValue(max(0, int(hold.get("duration", 5))))
            self.hold_delay.setValue(min(3.0, max(0.5, float(hold.get("delay", 3.0)))))
        self.save_settings()
        self.click_status.setText(f"○ 已套用預設：{preset['name']}")
        self.hold_status.setText(f"○ 已套用預設：{preset['name']}")

    def delete_clicker_preset(self):
        index = self.preset_combo.currentIndex()
        presets = self.settings.clicker_presets
        if not (1 <= index <= len(presets)):
            QMessageBox.information(self, APP_NAME, "請先在上面的下拉選單選一組要刪除的預設。")
            return
        name = presets[index - 1]["name"]
        if QMessageBox.question(self, APP_NAME, f"刪除預設「{name}」？") != QMessageBox.StandardButton.Yes:
            return
        presets.pop(index - 1)
        self.save_settings()
        self.refresh_clicker_presets()
        self.click_status.setText(f"○ 已刪除預設：{name}")

    def clicker_delay_ms(self):
        return int(round(min(3.0, max(0.5, float(self.settings.clicker_delay))) * 1000))

    def clicker_button_clicked(self):
        self.toggle_clicker()

    def toggle_clicker(self):
        """Start (after the countdown) or stop: used by the button and both hotkeys."""
        if self.clicker.running or self._clicker_pending:
            self.stop_clicker()  # also cancels a countdown in progress
            return
        self._clicker_pending = True
        self._clicker_token += 1
        token = self._clicker_token
        delay = self.clicker_delay_ms()
        self.click_button.setText("■  取消（倒數中）")
        self.click_button.setObjectName("danger")
        restyle(self.click_button)
        self.click_status.setText(f"● {delay / 1000:g} 秒後開始，請把滑鼠移到目標位置…")
        QTimer.singleShot(delay, lambda: self._start_clicker_now(token))

    def _start_clicker_now(self, token=None):
        if not self._clicker_pending or (token is not None and token != self._clicker_token):
            return  # cancelled (or replaced by a newer start) during the countdown
        self._clicker_pending = False
        if not self.clicker.running:
            wait_for_modifiers_released()  # a hotkey like Alt+C must not turn the clicks into Alt+key
            self._begin_clicker()
        self.update_clicker_ui()

    def update_clicker_ui(self):
        if not hasattr(self, "click_button"):
            return
        if self.clicker.running:
            self.click_button.setText("■  停止連點")
            self.click_button.setObjectName("danger")
            self.click_status.setText("● 連點中（時間到、快捷鍵或按「停止連點」結束）")
        elif self._clicker_pending:
            return
        else:
            self.click_button.setText(f"▶  開始連點（{self.settings.clicker_delay:g} 秒後開始）")
            self.click_button.setObjectName("primary")
            self.click_status.setText("○ 已停止")
        restyle(self.click_button)

    # ---- hold key (press and hold, no repeated clicking) ----
    def hold_target_changed(self, value):
        kind, name = target_kind_name(value)
        hotkey_name = (self.settings.hold_mouse_hotkey or "").partition(":")[2]
        if kind == "mouse" and hotkey_name and name == hotkey_name:
            QMessageBox.warning(self, APP_NAME, "要壓住的滑鼠鍵不能和「快捷鍵（滑鼠）」相同，請換一個。")
            self.hold_target_edit.value = self.settings.hold_target
            self.hold_target_edit._refresh()
            return
        self.settings.hold_target = value
        self.hold_action.target = value
        self.save_settings()

    def hold_duration_changed(self, value):
        self.settings.hold_duration = int(value)
        self.save_settings()

    def _begin_hold(self):
        self.hold_action.start()
        seconds = int(self.settings.hold_duration)
        if seconds > 0:
            self._hold_deadline.start(seconds * 1000)

    def stop_hold(self):
        self._hold_pending = False
        self._hold_token += 1
        self._hold_deadline.stop()
        self.hold_action.stop()
        self.update_hold_ui()

    def hold_delay_changed(self, value):
        self.settings.hold_delay = round(float(value), 1)
        self.save_settings()
        self.update_hold_ui()

    def hold_delay_ms(self):
        return int(round(min(3.0, max(0.5, float(self.settings.hold_delay))) * 1000))

    def hold_button_clicked(self):
        self.toggle_hold()

    def toggle_hold(self):
        """Start (after the countdown) or stop: used by the button and both hotkeys."""
        if self.hold_action.running or self._hold_pending:
            self.stop_hold()  # also cancels a countdown in progress
            return
        self._hold_pending = True
        self._hold_token += 1
        token = self._hold_token
        delay = self.hold_delay_ms()
        self.hold_button.setText("■  取消（倒數中）")
        self.hold_button.setObjectName("danger")
        restyle(self.hold_button)
        self.hold_status.setText(f"● {delay / 1000:g} 秒後開始，請把滑鼠移到目標位置…")
        QTimer.singleShot(delay, lambda: self._start_hold_now(token))

    def _start_hold_now(self, token=None):
        if not self._hold_pending or (token is not None and token != self._hold_token):
            return  # cancelled (or replaced by a newer start) during the countdown
        self._hold_pending = False
        if not self.hold_action.running:
            wait_for_modifiers_released()  # a hotkey like Alt+H must not turn the held key into Alt+key
            self._begin_hold()
        self.update_hold_ui()

    # ---- mouse hotkey for the hold feature (polled, works inside games) ----
    _MOUSE_HOTKEY_VKS = {"middle": 0x04, "x1": 0x05, "x2": 0x06}

    def _update_mouse_hotkey_timer(self):
        if self.settings.hold_mouse_hotkey or self.settings.clicker_mouse_hotkey:
            self._mouse_hotkey_down = {}
            self._mouse_hotkey_timer.start()
        else:
            self._mouse_hotkey_timer.stop()

    def _poll_mouse_hotkey(self):
        for key, value, action in (("hold", self.settings.hold_mouse_hotkey, self.toggle_hold),
                                   ("clicker", self.settings.clicker_mouse_hotkey, self.toggle_clicker)):
            vk = self._MOUSE_HOTKEY_VKS.get((value or "").partition(":")[2])
            if not vk:
                continue
            down = vk_is_down(vk)
            pressed_now = down and not self._mouse_hotkey_down.get(key, False)
            self._mouse_hotkey_down[key] = down
            if pressed_now and not self._capturing:
                action()

    def clicker_mouse_hotkey_changed(self, value):
        name = (value or "").partition(":")[2]
        kind, target = (self.settings.clicker_target or "").partition(":")[0], (self.settings.clicker_target or "").partition(":")[2]
        message = ""
        if value and kind == "mouse" and target == name:
            message = "快捷鍵不能和「連點按鍵」是同一個滑鼠鍵，請換一個。"
        elif value and value == self.settings.hold_mouse_hotkey:
            message = "這個滑鼠鍵已被「壓住按鍵」的快捷鍵使用，請換一個。"
        if message:
            QMessageBox.warning(self, APP_NAME, message)
            self.click_mouse_edit.value = self.settings.clicker_mouse_hotkey
            self.click_mouse_edit._refresh()
            return
        self.settings.clicker_mouse_hotkey = value
        self.save_settings()
        self._update_mouse_hotkey_timer()

    def hold_mouse_hotkey_changed(self, value):
        name = (value or "").partition(":")[2]
        kind, held = target_kind_name(self.settings.hold_target)
        if value and kind == "mouse" and held == name:
            QMessageBox.warning(self, APP_NAME, "快捷鍵不能和「要壓住的按鍵」是同一個滑鼠鍵，請換一個。")
            self.hold_mouse_edit.value = self.settings.hold_mouse_hotkey
            self.hold_mouse_edit._refresh()
            return
        if value and value == self.settings.clicker_mouse_hotkey:
            QMessageBox.warning(self, APP_NAME, "這個滑鼠鍵已被「連點」的快捷鍵使用，請換一個。")
            self.hold_mouse_edit.value = self.settings.hold_mouse_hotkey
            self.hold_mouse_edit._refresh()
            return
        self.settings.hold_mouse_hotkey = value
        self.save_settings()
        self._update_mouse_hotkey_timer()

    def update_hold_ui(self):
        if not hasattr(self, "hold_button"):
            return
        if self.hold_action.running:
            self.hold_button.setText("■  停止壓住")
            self.hold_button.setObjectName("danger")
            self.hold_status.setText("● 壓住中（時間到或按「停止壓住」放開）")
        elif self._hold_pending:
            return
        else:
            self.hold_button.setText(f"▶  開始壓住（{self.settings.hold_delay:g} 秒後開始）")
            self.hold_button.setObjectName("primary")
            self.hold_status.setText("○ 已放開")
        restyle(self.hold_button)

    def load_settings(self):
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            known = {f.name for f in fields(Settings)}
            settings = Settings(**{k: v for k, v in data.items() if k in known})
        except (OSError, ValueError, TypeError, AttributeError):
            return Settings()
        for name in ("music_folders", "favorites"):
            value = getattr(settings, name)
            setattr(settings, name, [str(v) for v in value] if isinstance(value, list) else [])
        for name in ("radio_favorites", "yt_favorites"):
            value = getattr(settings, name)
            setattr(settings, name, [d for d in value if isinstance(d, dict)] if isinstance(value, list) else [])
        settings.radio_favorites = [d for d in settings.radio_favorites if isinstance(d.get("urls"), list) and d["urls"]]
        if settings.yt_after not in ("stop", "next", "loop"):
            settings.yt_after = "next"
        if settings.radio_view not in ("all", "fav"):
            settings.radio_view = "all"
        presets = settings.clicker_presets if isinstance(settings.clicker_presets, list) else []
        settings.clicker_presets = [p for p in presets if isinstance(p, dict) and str(p.get("name", "")).strip()]
        if settings.music_folder:  # one-time migration from the old single-folder setting
            if not any(path_key(f) == path_key(settings.music_folder) for f in settings.music_folders):
                settings.music_folders.insert(0, settings.music_folder)
            settings.music_folder = ""
        settings.yt_fav_dir = str(settings.yt_fav_dir or "") if isinstance(settings.yt_fav_dir, str) else ""
        _YT_FAV_CUSTOM[0] = settings.yt_fav_dir
        settings.tools_dir = settings.tools_dir if isinstance(settings.tools_dir, str) else ""
        _TOOLS_CUSTOM[0] = settings.tools_dir
        if settings.viz_style not in ("off", "bars", "mirror", "wave"):
            settings.viz_style = "bars"
        pls = []
        for pl in (settings.playlists if isinstance(settings.playlists, list) else []):
            if isinstance(pl, dict) and isinstance(pl.get("id"), str) and str(pl.get("name", "")).strip():
                tracks = pl.get("tracks")
                pls.append({"id": pl["id"], "name": str(pl["name"]).strip(),
                            "tracks": [str(t) for t in tracks] if isinstance(tracks, list) else []})
        settings.playlists = pls
        if not isinstance(settings.last_pl_tracks, dict):
            settings.last_pl_tracks = {}
        return settings

    def save_settings(self):
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(asdict(self.settings), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # A second, old EchoDeck process may temporarily hold the config.
            # Audio should stay usable rather than crash during device setup.
            pass

    def closeEvent(self, event):
        self.clicker.stop()
        self.hold_action.stop()
        self.stop()
        self.remove_hotkeys()
        self.mixer.close()
        event.accept()



# ---- colour system + artwork --------------------------------------------------
# A theme = a whole palette (generated from hues) + procedurally painted artwork
# behind the window. Panels are translucent "glass" so the artwork shows through.
import colorsys
import math
import random
from PySide6.QtGui import QImage, QPolygonF, QRadialGradient
from PySide6.QtCore import QRectF


def _hsl(h, s, l):
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _rgba(hexc, alpha):
    c = QColor(hexc)
    return f"rgba({c.red()},{c.green()},{c.blue()},{int(alpha)})"


DEFAULT_THEME = "tokyo"
# bh/bs = background hue/saturation, ah = accent hue, lum = base lightness,
# glass = panel opacity 0-255 (255 = solid), art = backdrop painter
THEMES = {
    "graphite": dict(label="石墨（素色）", bh=225, bs=.08, ah=262, lum=.125, glass=255, art="plain"),
    # ---- 柔光漸層系列：低對比、大面積柔焦光暈，內容區保持乾淨 ----
    "tokyo":    dict(label="東京夜雨",   bh=232, bs=.30, ah=255, lum=.095, glass=178, art="mesh",
                     base=("#12131d", "#191a2a", "#0e0f18"), deco="waves", deco_color="#7aa2f7",
                     orbs=[(.10, .02, .60, "#7aa2f7", 95), (.96, .22, .52, "#bb9af7", 75), (.72, 1.02, .62, "#f7768e", 48)]),
    "rose":     dict(label="玫瑰松林",   bh=252, bs=.24, ah=348, lum=.09,  glass=180, art="mesh",
                     base=("#15121f", "#1d1830", "#0f0c18"), deco="bokeh", deco_color="#ebbcba",
                     orbs=[(.08, .00, .62, "#c4a7e7", 80), (.96, .92, .58, "#eb6f92", 58), (.88, .06, .42, "#f6c177", 34)]),
    "nord":     dict(label="北歐冰川",   bh=206, bs=.34, ah=180, lum=.088, glass=178, art="mesh",
                     base=("#050f1a", "#0d2436", "#030810"), decos=[("aurora", "#49e8c0"), ("peaks", "#e4f4ff")],
                     orbs=[(.14, .04, .58, "#48b6d6", 66), (.90, .90, .52, "#2f6fa8", 64),
                           (.55, .28, .32, "#8fe3d0", 22)]),
    "sakura":   dict(label="櫻花墨夜",   bh=330, bs=.22, ah=338, lum=.085, glass=176, art="mesh",
                     base=("#17101a", "#22152a", "#0f0a13"), deco="petals", deco_color="#ffb7d1",
                     orbs=[(.92, .06, .62, "#ff8fb8", 78), (.06, .98, .56, "#8e5bd6", 58), (.5, .5, .40, "#4a1d4a", 40)]),
    "mocha":    dict(label="黑膠咖啡",   bh=24,  bs=.28, ah=34,  lum=.085, glass=186, art="mesh",
                     base=("#17100b", "#21160e", "#0f0a06"), deco="vinyl", deco_color="#e8b27a",
                     orbs=[(.08, .06, .62, "#d98c4a", 72), (.98, .98, .62, "#a8452f", 60)]),
    "forest":   dict(label="森林薄霧",   bh=165, bs=.32, ah=150, lum=.085, glass=182, art="mesh",
                     base=("#08130f", "#0e1f19", "#060e0b"), decos=[("rays", "#b8f2d4"), ("woods", "#6fd8b0"), ("fireflies", "#c6ff7a")],
                     orbs=[(.86, .04, .62, "#4fd1a5", 58), (.04, .92, .52, "#2f7f9d", 58), (.5, .35, .30, "#7ee0b8", 20)]),
    "obsidian": dict(label="黑曜鎏金",   bh=40,  bs=.07, ah=42,  lum=.08,  glass=204, art="mesh",
                     base=("#0b0a09", "#12100d", "#070605"), decos=[("marble", "#d4af6a")],
                     orbs=[(.50, -.10, .65, "#b98f45", 40), (.00, 1.00, .45, "#8a6a2f", 30), (1.0, 1.0, .45, "#6b4f1f", 26)]),
    # ---- 風景系列 ----
    "dusk":     dict(label="夕陽餘暉",   bh=285, bs=.34, ah=22,  lum=.09,  glass=180, art="mesh",
                     base=("#1b1028", "#33193a", "#0f0714"), decos=[("sunset", "#ff8a4c")],
                     orbs=[(.50, .93, .75, "#ff6b3d", 70), (.08, .02, .60, "#7a4bd6", 78), (.96, .26, .52, "#ff5f8f", 58)]),
    "skyline":  dict(label="東京霓虹",   bh=255, bs=.40, ah=325, lum=.085, glass=178, art="mesh",
                     base=("#0f0c24", "#1a1038", "#080616"), decos=[("skyline", "#6fd6ff")],
                     orbs=[(.12, .04, .62, "#4d7cff", 85), (.92, .20, .52, "#c455ff", 70), (.50, 1.0, .65, "#ff4f9a", 60)]),
    "fuji":     dict(label="富士月夜",   bh=228, bs=.36, ah=205, lum=.09,  glass=182, art="mesh",
                     base=("#0a1030", "#131c48", "#070a1c"), decos=[("fuji", "#bcd0ff")],
                     orbs=[(.80, .15, .55, "#8aa4ff", 62), (.05, .95, .55, "#4a3a9a", 55)]),
    "torii":    dict(label="京都鳥居",   bh=358, bs=.26, ah=14,  lum=.085, glass=184, art="mesh",
                     base=("#190c0e", "#27121a", "#0d0608"), decos=[("torii", "#c0392b"), ("petals", "#ff9a5a")],
                     orbs=[(.10, .00, .62, "#ff6a3d", 66), (.96, .96, .62, "#8a2a56", 62), (.50, .30, .30, "#ffb36b", 20)]),
    # ---- 淺色柔光系列：粉彩漸層、低飽和、不刺眼（面板是半透明白玻璃，文字自動換成深色）----
    "lilac":    dict(label="薰衣草（淺色）", light=True, bh=250, bs=.45, ah=247, lum=.9, glass=232, art="mesh",
                     base=("#f5f3fc", "#f4f2fb", "#f4f2fb"), vignette=0, grain=0,
                     orbs=[(.0, .0, .80, "#dfd9fa", 190), (1.0, .0, .72, "#dee9fb", 190), (.62, 1.08, .66, "#f3e0f3", 190)]),
    "skylight": dict(label="晴空（淺色）", light=True, bh=212, bs=.50, ah=214, lum=.9, glass=232, art="mesh",
                     base=("#f3f7fc", "#f1f5fb", "#f2f6fb"), vignette=0, grain=0,
                     orbs=[(.0, .0, .80, "#d6e7fb", 190), (1.0, .0, .72, "#d9f0f2", 190), (.62, 1.08, .66, "#e5e1f8", 190)]),
    "rosewater": dict(label="玫瑰（淺色）", light=True, bh=338, bs=.45, ah=338, lum=.9, glass=232, art="mesh",
                      base=("#fcf5f7", "#fbf3f6", "#fbf3f6"), vignette=0, grain=0,
                      orbs=[(.0, .0, .80, "#f8dde7", 190), (1.0, .0, .72, "#fbe9d8", 190), (.62, 1.08, .66, "#ece0f6", 190)]),
}


def palette_for(key):
    t = THEMES.get(key) or THEMES[DEFAULT_THEME]
    bh, bs, ah, lum, glass = t["bh"], t["bs"], t["ah"], t["lum"], t["glass"]
    if t.get("light"):
        d = _light_palette(bh, bs, ah, t.get("dim", 0))
    else:
        d = _dark_palette(bh, bs, ah, lum)
    # solid + translucent ("glass") variants used by the stylesheet
    d["PANEL_S"], d["RAISED_S"] = d["PANEL"], d["RAISED"]
    d["PANEL_A"] = _rgba(d["PANEL"], glass)
    d["BASE_A"] = _rgba(d["BASE"], min(255, glass + 30))
    d["RAISED_A"] = _rgba(d["RAISED"], min(255, glass + 25))
    d["RAISED_HI_A"] = _rgba(d["RAISED_HI"], min(255, glass + 40))
    return d


def _dark_palette(bh, bs, ah, lum):
    d = {
        "BASE": _hsl(bh, bs, lum), "PANEL": _hsl(bh, bs, lum * 1.4),
        "RAISED": _hsl(bh, bs, lum * 1.85), "RAISED_HI": _hsl(bh, bs, lum * 2.3),
        "LINE": _hsl(bh, bs * .9, lum + .17), "LINE_HI": _hsl(bh, bs * .8, lum + .25),
        "SEL": _hsl(bh, max(.30, bs * .75), .32), "SEL_LINE": _hsl(bh, max(.30, bs * .75), .47),
        "FOCUS": _hsl(ah, .55, .64), "FOCUS_HI": _hsl(ah, .72, .78),
        "TEXT": _hsl(bh, .35, .95), "TEXT2": _hsl(bh, .28, .87), "MUTED": _hsl(bh, .20, .74),
        "FAINT": _hsl(bh, .18, .66), "DISABLED": _hsl(bh, .12, .48),
        "SCROLL": _hsl(bh, .30, .32), "SCROLL_HI": _hsl(bh, .35, .44),
        "ACC_HI": _hsl(ah, .62, .58), "ACC_LO": _hsl(ah, .60, .44),
        "ACC_HOVER_HI": _hsl(ah, .68, .64), "ACC_HOVER_LO": _hsl(ah, .64, .50),
        "ACC_LINE": _hsl(ah, .70, .70), "ACC_BAR": _hsl(ah, .88, .70),
        "ACC_TEXT": _hsl(ah, .95, .85), "ACC_SOFT": _hsl(ah, .50, .25),
        "ACC_BORDER": _hsl(ah, .42, .36),
    }
    # colours that used to be hard-coded in the stylesheet (dark values = the original look)
    d.update({
        "HEAD": "#f3f1ff", "STRONG": "#ffffff", "BTN_TEXT": "#dfe2f5",
        "OK_FG": "#5fd9a4", "OK_FG2": "#7fd9b6", "OK_BG": "#12302a", "OK_LINE": "#1c4a3d",
        "LIVE_FG": "#ff8da1", "LIVE_BG": "#3a1f2b", "LIVE_LINE": "#6a3045",
        "WARN_FG": "#f5c26b", "WARN_BG": "#38301a", "WARN_LINE": "#5e4d22",
        "DANGER_FG": "#ff9fb4", "DANGER_BG": "#3a1f2e", "DANGER_LINE": "#6d3449",
        "DANGER_HOVER": "#4a2538", "DANGER_HOVER_LINE": "#a04a66", "DANGER_PRESSED": "#55293f",
        "PAUSE_BG": "#1c2738", "PAUSE_LINE": "#31465f", "PAUSE_FG": "#9fc0e0",
        "PAUSE_HOVER": "#223149", "PAUSE_HOVER_LINE": "#4a6a90",
        "CHEVRON": "#a3a8c3", "NAV_OFF": "#8d92b0", "NAV_ON": "#ffffff",
        "STAR": "#f5c26b", "STAR_MISSING": "#6b5a30", "TAG_MISSING": "#a86a78",
        "TEXT_MISSING_SEL": "#8a8fb0", "NOT_FOUND": "#5d6280",
        "LISTEN_QSS": "border-color: #ff9bb0; background: #2a2036;",
    })
    return d


def _light_palette(bh, bs, ah, dim=0.0):
    """Pastel light palette: near-white glass panels, dark readable text, accent from `ah`."""
    dl = -.10 if 65 <= ah <= 205 else 0     # green/teal accents are bright: darken so white button text stays readable
    d = {
        "BASE": _hsl(bh, bs, .955 - dim), "PANEL": _hsl(bh, bs * .4, .997 - dim),
        "RAISED": _hsl(bh, bs * .9, .945 - dim), "RAISED_HI": _hsl(bh, bs * .95, .905 - dim),
        "LINE": _hsl(bh, bs * .6, .895 - dim), "LINE_HI": _hsl(bh, bs * .55, .815 - dim),
        "SEL": _hsl(bh, max(.55, bs), .885 - dim), "SEL_LINE": _hsl(bh, max(.45, bs * .8), .72),
        "FOCUS": _hsl(ah, .62, .56), "FOCUS_HI": _hsl(ah, .66, .46),
        "TEXT": _hsl(bh, .32, .15), "TEXT2": _hsl(bh, .26, .23), "MUTED": _hsl(bh, .16, .38),
        "FAINT": _hsl(bh, .13, .46), "DISABLED": _hsl(bh, .10, .64),
        "SCROLL": _hsl(bh, .25, .78), "SCROLL_HI": _hsl(bh, .30, .65),
        "ACC_HI": _hsl(ah, .68, .58 + dl), "ACC_LO": _hsl(ah, .64, .47 + dl),
        "ACC_HOVER_HI": _hsl(ah, .72, .63 + dl), "ACC_HOVER_LO": _hsl(ah, .68, .52 + dl),
        "ACC_LINE": _hsl(ah, .60, .42), "ACC_BAR": _hsl(ah, .72, .55),
        "ACC_TEXT": _hsl(ah, .70, .36), "ACC_SOFT": _hsl(ah, .75, .925),
        "ACC_BORDER": _hsl(ah, .55, .80),
    }
    d.update({
        "HEAD": _hsl(bh, .38, .12), "STRONG": _hsl(bh, .40, .11), "BTN_TEXT": _hsl(bh, .30, .20),
        "OK_FG": "#10785a", "OK_FG2": "#10785a", "OK_BG": "#dcf5ea", "OK_LINE": "#a9e0c9",
        "LIVE_FG": "#c0274e", "LIVE_BG": "#ffe2e9", "LIVE_LINE": "#f3b4c4",
        "WARN_FG": "#946000", "WARN_BG": "#fff0cd", "WARN_LINE": "#efd08a",
        "DANGER_FG": "#c0274e", "DANGER_BG": "#ffe4ea", "DANGER_LINE": "#f0b3c3",
        "DANGER_HOVER": "#ffd6e0", "DANGER_HOVER_LINE": "#e58aa3", "DANGER_PRESSED": "#ffc7d5",
        "PAUSE_BG": "#e4eefb", "PAUSE_LINE": "#b6cdee", "PAUSE_FG": "#2f5f9a",
        "PAUSE_HOVER": "#d7e6f9", "PAUSE_HOVER_LINE": "#8fb2e0",
        "CHEVRON": "#6b7090", "NAV_OFF": "#6b7090", "NAV_ON": _hsl(ah, .62, .36),
        "STAR": "#e0a01c", "STAR_MISSING": "#e6d6aa", "TAG_MISSING": "#c0788a",
        "TEXT_MISSING_SEL": "#8d92b0", "NOT_FOUND": "#a3a7bd",
        "LISTEN_QSS": "border-color: #e0708f; background: #ffe9f0;",
    })
    return d


T = {}
CURRENT_THEME = [DEFAULT_THEME]


def set_theme(key):
    """Select a theme. The stylesheet, list rows and backdrop all read from it."""
    if key not in THEMES:
        key = DEFAULT_THEME
    T.clear()
    T.update(palette_for(key))
    CURRENT_THEME[0] = key
    return key


set_theme(DEFAULT_THEME)

# ---- procedural artwork (painted once at 1600x1000, cached, scaled to the window) ----
ART_W, ART_H = 1600, 1000
_ART_CACHE = {}


def _qc(color, alpha=255):
    c = QColor(color)
    c.setAlpha(int(alpha))
    return c


def _vgrad(stops, y0=0, y1=ART_H):
    g = QLinearGradient(0, y0, 0, y1)
    for pos, col in stops:
        g.setColorAt(pos, QColor(col))
    return g


def _canvas():
    img = QImage(ART_W, ART_H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.black)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    return img, p


def _finish(img, p, vignette=130, color="#000000"):
    """Tone the edges a little (dark themes: darker, light themes: a soft tint)."""
    g = QRadialGradient(QPointF(ART_W / 2, ART_H / 2), ART_W * .72)
    g.setColorAt(.5, _qc(color, 0))
    g.setColorAt(1, _qc(color, vignette))
    p.fillRect(img.rect(), QBrush(g))
    p.end()
    return QPixmap.fromImage(img)


def _art_plain(pal):
    img, p = _canvas()
    p.fillRect(img.rect(), _vgrad([(0, pal["PANEL"]), (1, pal["BASE"])]))
    g = QRadialGradient(QPointF(ART_W * .2, 0), ART_W * .8)
    g.setColorAt(0, _qc(pal["ACC_HI"], 38))
    g.setColorAt(1, _qc(pal["ACC_HI"], 0))
    p.fillRect(img.rect(), QBrush(g))
    return _finish(img, p, 60)


# ---- 柔光漸層（mesh）背景：多顆柔焦光暈 + 低調點綴 + 細顆粒 ----
def _orb(p, x, y, r, color, alpha):
    g = QRadialGradient(QPointF(ART_W * x, ART_H * y), ART_W * r)
    alpha = min(255, alpha * 1.4)
    g.setColorAt(0, _qc(color, alpha))
    g.setColorAt(.5, _qc(color, alpha * .38))
    g.setColorAt(1, _qc(color, 0))
    p.fillRect(QRectF(0, 0, ART_W, ART_H), QBrush(g))


def _deco_waves(p, col):
    """Very faint sound-wave lines along the bottom (music app feel)."""
    p.setBrush(Qt.BrushStyle.NoBrush)
    for i in range(8):
        p.setPen(QPen(_qc(col, 62 - i * 6), 1.3))
        path = QPainterPath()
        for k, x in enumerate(range(0, ART_W + 8, 8)):
            y = ART_H * (.80 + i * .02) + 26 * math.sin(x / 175 + i * .5) + 11 * math.sin(x / 58 + i * 1.3)
            path.moveTo(x, y) if k == 0 else path.lineTo(x, y)
        p.drawPath(path)


def _deco_ridges(p, col):
    """Layered, hazy mountain ridges for depth."""
    rng = random.Random(3)
    p.setPen(Qt.PenStyle.NoPen)
    for i, (base, amp, alpha) in enumerate(((.80, 70, 70), (.88, 55, 120), (.95, 40, 190))):
        poly = [QPointF(0, ART_H)]
        ph = rng.uniform(0, 6)
        for x in range(0, ART_W + 20, 20):
            poly.append(QPointF(x, ART_H * base - amp * (.5 + .5 * math.sin(x / (210 - i * 40) + ph))
                                - amp * .3 * math.sin(x / 63 + ph * 2)))
        poly.append(QPointF(ART_W, ART_H))
        p.setBrush(_qc(col, alpha))
        p.drawPolygon(QPolygonF(poly))


def _deco_petals(p, col):
    rng = random.Random(11)
    p.setPen(Qt.PenStyle.NoPen)
    for _ in range(46):
        x = ART_W * (1 - rng.random() ** 1.8)          # denser toward the right
        y = rng.uniform(0, ART_H * .9)
        s = rng.uniform(5, 13)
        p.save()
        p.translate(x, y)
        p.rotate(rng.uniform(0, 360))
        p.setBrush(_qc(col, rng.uniform(25, 95)))
        p.drawEllipse(QPointF(0, 0), s, s * .55)
        p.restore()


def _deco_bokeh(p, col):
    rng = random.Random(7)
    for _ in range(26):
        r = rng.uniform(10, 44)
        x, y = rng.uniform(0, ART_W), rng.uniform(0, ART_H)
        p.setBrush(_qc(col, rng.uniform(8, 26)))
        p.setPen(QPen(_qc(col, rng.uniform(14, 40)), 1))
        p.drawEllipse(QPointF(x, y), r, r)


def _deco_vinyl(p, col):
    """A giant vinyl record peeking in from the bottom-right corner."""
    c = QPointF(ART_W * 1.0, ART_H * 1.02)
    p.setBrush(Qt.BrushStyle.NoBrush)
    r = 120
    while r < 760:
        p.setPen(QPen(_qc(col, 13 if int(r) % 3 else 26), 1))
        p.drawEllipse(c, r, r)
        r += 9
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_qc(col, 34))
    p.drawEllipse(c, 96, 96)
    p.setBrush(_qc("#000000", 120))
    p.drawEllipse(c, 10, 10)


def _deco_beams(p, col):
    """Soft diagonal light beams + hairline, like a spotlit gallery."""
    p.setPen(Qt.PenStyle.NoPen)
    for x0, w, a in ((.30, .10, 26), (.46, .05, 20), (.62, .12, 22), (.80, .04, 18)):
        poly = QPolygonF([QPointF(ART_W * x0, 0), QPointF(ART_W * (x0 + w), 0),
                          QPointF(ART_W * (x0 + w) - 380, ART_H), QPointF(ART_W * x0 - 380, ART_H)])
        g = QLinearGradient(0, 0, 0, ART_H)
        g.setColorAt(0, _qc(col, a))
        g.setColorAt(1, _qc(col, 0))
        p.setBrush(QBrush(g))
        p.drawPolygon(poly)
    p.setPen(QPen(_qc(col, 38), 1))
    p.drawLine(QPointF(0, ART_H * .965), QPointF(ART_W, ART_H * .965))


def _bloom(p, draw, scale=6, gain=.8):
    """Paint `draw` small, scale it up additively -> soft glow."""
    im = QImage(ART_W // scale, ART_H // scale, QImage.Format.Format_ARGB32_Premultiplied)
    im.fill(0)
    q = QPainter(im)
    q.setRenderHint(QPainter.RenderHint.Antialiasing)
    q.scale(1 / scale, 1 / scale)
    draw(q)
    q.end()
    p.save()
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
    p.setOpacity(gain)
    p.drawImage(QRectF(0, 0, ART_W, ART_H), im)
    p.restore()


def _soft_ellipse(p, cx, cy, rx, ry, col, alpha):
    p.save()
    p.translate(cx, cy)
    p.scale(1, ry / rx)
    g = QRadialGradient(QPointF(0, 0), rx)
    g.setColorAt(0, _qc(col, alpha)); g.setColorAt(.45, _qc(col, alpha * .42)); g.setColorAt(1, _qc(col, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(0, 0), rx, rx)
    p.restore()


def _deco_sunset(p, col):
    """Sun already below the horizon: wide soft afterglow, streaky clouds, shimmering sea. No disc."""
    W, H = ART_W, ART_H
    hy = H * .86
    rng = random.Random(8)
    for rx, ry, a in ((1250, 300, 120), (800, 150, 160), (420, 60, 185)):
        _soft_ellipse(p, W * .5, hy, rx, ry, col, a)
    # long cloud streaks lit from below
    for _ in range(9):
        cy = hy - rng.uniform(70, 420)
        cx = rng.uniform(W * .1, W * .9)
        rx = rng.uniform(260, 620)
        h = rng.uniform(10, 26)
        warm = 1 - (hy - cy) / 440
        _soft_ellipse(p, cx, cy + h * .6, rx, h, "#ff9a5c" if warm > .45 else "#ff6fa0", 70 + 60 * warm)
        _soft_ellipse(p, cx, cy - h * .3, rx * .9, h * .8, "#2a1030", 70)
    # faint far island
    isl = QPainterPath()
    isl.moveTo(W * .72, hy)
    for x in range(int(W * .72), int(W * .93), 8):
        t = (x - W * .72) / (W * .21)
        isl.lineTo(x, hy - 34 * math.sin(t * math.pi) ** 1.4 - 6 * math.sin(t * math.pi) * math.sin(x / 7.0))
    isl.lineTo(W * .93, hy)
    isl.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_qc("#20102c", 170))
    p.drawPath(isl)
    # sea
    sea = QLinearGradient(0, hy, 0, H)
    sea.setColorAt(0, _qc("#2a1233", 225)); sea.setColorAt(1, _qc("#05020a", 252))
    p.fillRect(QRectF(0, hy, W, H - hy), QBrush(sea))
    # shimmering reflection column
    n = 46
    sh = (H - hy) / n
    for i in range(n):
        half = 90 + i * 13
        a = max(0, 165 - i * 3.2) * rng.uniform(.4, 1.0)
        g = QLinearGradient(W * .5 - half, 0, W * .5 + half, 0)
        g.setColorAt(0, _qc(col, 0)); g.setColorAt(.5, _qc(col, a)); g.setColorAt(1, _qc(col, 0))
        p.fillRect(QRectF(W * .5 - half, hy + i * sh, half * 2, sh * .62), QBrush(g))
    g = QLinearGradient(0, 0, W, 0)
    g.setColorAt(0, _qc("#ffd2a0", 0)); g.setColorAt(.5, _qc("#ffd2a0", 120)); g.setColorAt(1, _qc("#ffd2a0", 0))
    p.fillRect(QRectF(0, hy - 1, W, 2), QBrush(g))


def _tokyo_tower(q, tx, by, Ht, line, deck, lw=1.0, glow=False):
    """Tokyo Tower in the real proportions: lattice legs, two decks, banded antenna."""
    def hw(t):
        return max(.0065 * Ht, .125 * Ht * (1 - t) ** 1.95)
    ts = [0, .04, .09, .15, .22, .30, .37, .45, .52, .58, .64, .70, .75]
    rows = [(tx - hw(t), tx + hw(t), by - t * Ht) for t in ts]
    pen = QPen(_qc(line, 255 if not glow else 200), 3.2 * lw)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    q.setPen(pen)
    for side in (0, 1):
        for a, b in zip(rows, rows[1:]):
            q.drawLine(QPointF(a[side], a[2]), QPointF(b[side], b[2]))
    q.setPen(QPen(_qc(line, 200), 1.4 * lw))
    for a, b in zip(rows, rows[1:]):
        q.drawLine(QPointF(a[0], a[2]), QPointF(b[1], b[2]))
        q.drawLine(QPointF(a[1], a[2]), QPointF(b[0], b[2]))
        q.drawLine(QPointF(a[0], b[2]), QPointF(a[1], b[2]))
    # feet arch
    q.setPen(QPen(_qc(line, 220), 2.4 * lw))
    q.drawArc(QRectF(tx - .06 * Ht, by - .11 * Ht, .12 * Ht, .20 * Ht), 0, 180 * 16)
    # decks
    q.setPen(Qt.PenStyle.NoPen)
    for t, mult, hgt in ((.45, 2.5, .030), (.75, 3.4, .016)):
        w = hw(t) * mult
        y = by - t * Ht
        q.setBrush(_qc(deck, 255 if not glow else 230))
        q.drawRect(QRectF(tx - w, y - hgt * Ht * .5, w * 2, hgt * Ht))
        q.setBrush(_qc(line, 255))
        q.drawRect(QRectF(tx - w * .8, y + hgt * Ht * .5, w * 1.6, hgt * Ht * .35))
    # antenna with red/white bands
    y0, y1 = by - .75 * Ht, by - Ht
    seg = 9
    for i in range(seg):
        a = y0 + (y1 - y0) * i / seg
        b = y0 + (y1 - y0) * (i + 1) / seg
        q.setBrush(_qc("#ff4a3a" if i % 2 == 0 else "#fff2e0", 255))
        q.drawRect(QRectF(tx - 1.6 * lw - (1 - i / seg) * 1.4, b, 3.2 * lw + (1 - i / seg) * 2.8, a - b))


def _deco_skyline(p, col):
    """Night Tokyo: lit Tokyo Tower, layered towers with lit floors, neon and light trails."""
    W, H = ART_W, ART_H
    rng = random.Random(4)
    tx, by, Ht = W * .76, H * .985, H * .72
    # tower glow + sharp
    _bloom(p, lambda q: _tokyo_tower(q, tx, by, Ht, "#ff7a3a", "#ffd7a0", 3.0, True), 5, .75)
    _bloom(p, lambda q: _tokyo_tower(q, tx, by, Ht, "#ff7a3a", "#ffd7a0", 5.0, True), 14, .85)
    _tokyo_tower(p, tx, by, Ht, "#ff8a45", "#ffe2b0", 1.0)
    lights = []
    layers = ((.30, 170, 40, 100, .07, "#241a52", "#150f33", .0),
              (.20, 235, 46, 118, .15, "#120b2c", "#080418", .0),
              (.115, 252, 56, 140, .10, "#080412", "#04020a", .0))
    for li, (hmax, alpha, wmin, wmax, lit, c_top, c_bot, _) in enumerate(layers):
        x = -30
        while x < W:
            w = rng.uniform(wmin, wmax)
            h = rng.uniform(.35, 1.0) * hmax * H
            if abs(x + w / 2 - tx) < 230:                  # keep the tower's legs visible
                h = min(h, .085 * H * (li + 1) / 2)
            top = H - h
            g = QLinearGradient(0, top, 0, H)
            g.setColorAt(0, _qc(c_top, alpha)); g.setColorAt(1, _qc(c_bot, alpha))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawRect(QRectF(x, top, w, h))
            if rng.random() < .35:                       # stepped roof
                sw = w * rng.uniform(.4, .7)
                sh = rng.uniform(14, 40)
                p.drawRect(QRectF(x + (w - sw) / 2, top - sh, sw, sh))
                top -= sh
            if rng.random() < .30 and li < 2:            # aviation light
                lights.append((x + w / 2 - 2, top - 8, 4, 4, "#ff3b3b", 230))
                p.setBrush(_qc("#10081e", alpha))
                p.drawRect(QRectF(x + w / 2 - 1, top - 14, 2, 14))
            p.setPen(QPen(_qc("#9a8cff", 26 + li * 6), 1))
            p.drawLine(QPointF(x, top), QPointF(x, H))   # lit left edge
            p.setPen(Qt.PenStyle.NoPen)
            wy = top + 9
            while wy < H - 12:
                if rng.random() < lit * 2.6:             # a lit floor
                    wx = x + 7
                    base = rng.choice((col, "#ffd9a0", "#ffb36b", "#ff7ab8"))
                    while wx < x + w - 9:
                        if rng.random() < .72:
                            lights.append((wx, wy, 3.2, 4.2, base, rng.uniform(80, 215)))
                        wx += 8.5
                wy += 11.5
            if rng.random() < .11 and li == 1:           # neon sign strip
                nc = rng.choice(("#ff3d9a", "#33e0ff", "#b66bff"))
                lights.append((x + w - 9, top + 24, 4, rng.uniform(40, 90), nc, 235))
            x += w + rng.uniform(-8, 3)
    # road light trails
    for i in range(46):
        y = H * (.965 + .03 * rng.random())
        x0 = rng.uniform(0, W)
        ln = rng.uniform(40, 220)
        c = "#ff4a4a" if i % 2 else "#fff0d0"
        lights.append((x0, y, ln, 1.8, c, rng.uniform(90, 200)))

    def draw_lights(q):
        q.setPen(Qt.PenStyle.NoPen)
        for lx, ly, lw_, lh, lc, la in lights:
            q.setBrush(_qc(lc, la))
            q.drawRect(QRectF(lx, ly, lw_, lh))
    draw_lights(p)
    _bloom(p, draw_lights, 4, .55)
    _bloom(p, draw_lights, 12, .65)


def _deco_fuji(p, col):
    """Moonlit Mt. Fuji: concave cone, gullied snowcap, lens cloud, pines and a mirror lake."""
    W, H = ART_W, ART_H
    rng = random.Random(6)
    hy = H * .86
    cx, peak, half = W * .46, H * .47, W * .64
    for _ in range(110):
        r = rng.uniform(.5, 1.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_qc("#ffffff", rng.uniform(40, 200)))
        p.drawEllipse(QPointF(rng.uniform(0, W), rng.uniform(0, H * .5)), r, r)
    mx, my = W * .86, H * .16
    for r, a in ((340, 55), (150, 70)):
        g = QRadialGradient(QPointF(mx, my), r)
        g.setColorAt(0, _qc(col, a)); g.setColorAt(1, _qc(col, 0))
        p.setBrush(QBrush(g)); p.drawEllipse(QPointF(mx, my), r, r)
    p.setBrush(_qc("#fff7e2", 235)); p.drawEllipse(QPointF(mx, my), 30, 30)
    _soft_ellipse(p, cx, hy, 900, 190, "#ff8fb0", 85)           # dusk glow behind the mountain

    def prof(x):
        d = abs(x - cx) / half
        y = peak + (hy - peak) * (d ** .74)
        y += 7 * math.sin(x / 41.0) * d + 3 * math.sin(x / 13.0) * d
        if abs(x - cx) < 44:
            y += 7 * (1 - ((x - cx) / 44) ** 2)
        return y
    xs = list(range(int(cx - half), int(cx + half) + 1, 5))
    edge = [(x, prof(x)) for x in xs]
    mount = QPolygonF([QPointF(xs[0], hy)] + [QPointF(x, y) for x, y in edge] + [QPointF(xs[-1], hy)])
    mg = QLinearGradient(0, peak, 0, hy)
    mg.setColorAt(0, _qc("#6b7bb5", 255)); mg.setColorAt(.5, _qc("#2d3870", 255)); mg.setColorAt(1, _qc("#141838", 255))
    p.setBrush(QBrush(mg)); p.drawPolygon(mount)
    clip = QPainterPath(); clip.addPolygon(mount)
    p.save(); p.setClipPath(clip)
    lg = QLinearGradient(cx - half, 0, cx + half, 0)          # moonlit right flank, shaded left flank
    lg.setColorAt(0, _qc("#05071a", 120)); lg.setColorAt(.5, _qc("#05071a", 0)); lg.setColorAt(1, _qc("#c8d6ff", 60))
    p.fillRect(QRectF(0, peak, W, hy - peak), QBrush(lg))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for k in range(-7, 8):                                      # a few irregular ravines
        if rng.random() < .35:
            continue
        sx = cx + k * 11 + rng.uniform(-6, 6)
        ex = cx + k / 7.0 * half * rng.uniform(.72, .98)
        pth = QPainterPath(QPointF(sx, peak + 40))
        pth.cubicTo(QPointF(sx + (ex - sx) * .05, peak + 150), QPointF(sx + (ex - sx) * .5, hy - 130), QPointF(ex, hy))
        p.setPen(QPen(_qc("#070a22", rng.randint(22, 48)), rng.uniform(3, 9)))
        p.drawPath(pth)
    top = [(x, y) for x, y in edge if y < peak + 160]
    low = []
    for x, y in reversed(top):
        taper = max(0.0, 1 - (y - peak) / 160.0)
        f = 30 + 34 * abs(math.sin(x / 10.7) * math.sin(x / 3.9 + 1)) + 8 * math.sin(x / 6.0)
        low.append(QPointF(x, y + f * taper))
    sg = QLinearGradient(cx - 200, 0, cx + 200, 0)
    sg.setColorAt(0, _qc("#b9c6ee", 215)); sg.setColorAt(1, _qc("#f8faff", 245))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(sg))
    p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in top] + low))
    p.restore()
    _soft_ellipse(p, cx + 40, peak + 78, 300, 17, "#eef2ff", 150)    # lens cloud
    _soft_ellipse(p, cx - 120, peak + 112, 190, 10, "#eef2ff", 95)
    # lake + mirrored mountain
    lake = QLinearGradient(0, hy, 0, H)
    lake.setColorAt(0, _qc("#1b2250", 250)); lake.setColorAt(1, _qc("#04061a", 255))
    p.fillRect(QRectF(0, hy, W, H - hy), QBrush(lake))
    p.save()
    p.setClipRect(QRectF(0, hy, W, H - hy))
    p.translate(0, 2 * hy); p.scale(1, -1)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_qc("#5566ae", 125)); p.drawPolygon(mount)
    p.setBrush(_qc("#e8eeff", 60)); p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in top] + low))
    p.restore()
    for _ in range(70):
        y = rng.uniform(hy + 4, H)
        x = rng.uniform(0, W)
        p.setPen(QPen(_qc("#0a0e30" if rng.random() < .6 else "#9db0ff", rng.randint(14, 40)), 1.4))
        p.drawLine(QPointF(x, y), QPointF(x + rng.uniform(40, 200), y))
    for i in range(14):
        y = hy + 10 + i * 14
        half_w = 14 + i * 3
        p.setPen(QPen(_qc("#fff0d0", max(10, 110 - i * 8)), 1.6))
        p.drawLine(QPointF(mx - half_w, y), QPointF(mx + half_w, y))
    # pine shore
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_qc("#050716", 252))
    x = -20
    while x < W + 20:
        h = rng.uniform(24, 70)
        w = h * .42
        p.drawPolygon(QPolygonF([QPointF(x - w, hy + 6), QPointF(x, hy + 6 - h), QPointF(x + w, hy + 6)]))
        x += rng.uniform(10, 26)
    p.drawRect(QRectF(0, hy, W, 6))
    for yc, h, a in ((.84, 90, 70), (.74, 150, 32)):         # mist
        mg2 = QLinearGradient(0, H * yc - h / 2, 0, H * yc + h / 2)
        mg2.setColorAt(0, _qc("#aebcff", 0)); mg2.setColorAt(.5, _qc("#aebcff", a)); mg2.setColorAt(1, _qc("#aebcff", 0))
        p.fillRect(QRectF(0, H * yc - h / 2, W, h), QBrush(mg2))


def _deco_aurora(p, col):
    """Aurora curtains made of thin vertical rays (bright hem, fading upward) + stars."""
    W, H = ART_W, ART_H
    rng = random.Random(2)
    p.setPen(Qt.PenStyle.NoPen)
    for _ in range(150):
        r = rng.uniform(.5, 1.5)
        p.setBrush(_qc("#ffffff", rng.uniform(40, 200)))
        p.drawEllipse(QPointF(rng.uniform(0, W), rng.uniform(0, H * .55)), r, r)

    def curtain(q, y0, amp, f, ph, hgt, c1, c2, gain, step=3):
        for x in range(0, W + step, step):
            yb = y0 + amp * math.sin(x / W * f * 6.283 + ph) + amp * .45 * math.sin(x / W * f * 17 + ph * 2)
            h = hgt * (.5 + .5 * math.sin(x / W * f * 23 + ph * 3) ** 2)
            g = QLinearGradient(0, yb - h, 0, yb)
            g.setColorAt(0, _qc(c2, 0)); g.setColorAt(.65, _qc(c2, gain * .45))
            g.setColorAt(.94, _qc(c1, gain)); g.setColorAt(1, _qc(c1, 0))
            q.fillRect(QRectF(x, yb - h, step + 1, h), QBrush(g))
    for y0, amp, f, ph, hgt, c1, c2, gain in ((H * .30, 55, 1.1, .8, 270, col, "#7a6bff", 150),
                                               (H * .22, 40, 1.6, 2.6, 190, "#5ac8ff", "#8a7bff", 100)):
        _bloom(p, lambda q: curtain(q, y0, amp, f, ph, hgt, c1, c2, gain), 3, .55)
        _bloom(p, lambda q: curtain(q, y0, amp, f, ph, hgt, c1, c2, gain), 11, .45)


def _deco_peaks(p, col):
    """Long, smooth mountain ranges fading into fog, a pine edge, and a still fjord."""
    W, H = ART_W, ART_H
    hy = H * .90
    rng = random.Random(12)
    layers = ((.70, 120, "#1f4259", 232, 21), (.77, 105, "#173448", 238, 22),
              (.83, 85, "#0f2636", 244, 23), (.895, 55, "#07141d", 252, 24))
    fog = "#4f8aa8"
    built = []
    for base, amp, c_top, alpha, seed in layers:
        r = random.Random(seed)
        ph = [r.uniform(0, 6.3) for _ in range(3)]
        pts = []
        for x in range(-10, W + 11, 4):
            v = .55 * math.sin(x / 760.0 + ph[0]) + .30 * math.sin(x / 390.0 + ph[1]) + .15 * math.sin(x / 190.0 + ph[2])
            pts.append((x, min(hy, H * base - amp * v)))
        built.append((base, amp, c_top, alpha, pts))

    def draw_all(mirror):
        k = .30 if mirror else 1.0
        for i, (base, amp, c_top, alpha, pts) in enumerate(built):
            poly = QPolygonF([QPointF(pts[0][0], hy)] + [QPointF(x, y) for x, y in pts] + [QPointF(pts[-1][0], hy)])
            top_y = min(y for _x, y in pts)
            g = QLinearGradient(0, top_y, 0, hy)
            g.setColorAt(0, _qc(c_top, alpha * k))
            g.setColorAt(1, _qc(fog if i < 3 else c_top, (alpha * .55 if i < 3 else alpha) * k))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawPolygon(poly)
    draw_all(False)
    # pine edge on the nearest ridge
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_qc("#040b11", 252))
    pts = built[-1][4]
    i = 0
    while i < len(pts):
        x, y = pts[i]
        h = rng.uniform(16, 44)
        w = h * .34
        p.drawPolygon(QPolygonF([QPointF(x - w, y + 6), QPointF(x, y + 6 - h), QPointF(x + w, y + 6)]))
        i += rng.randint(2, 4)
    water = QLinearGradient(0, hy, 0, H)
    water.setColorAt(0, _qc("#0c2a3a", 235)); water.setColorAt(1, _qc("#030c13", 252))
    p.fillRect(QRectF(0, hy, W, H - hy), QBrush(water))
    p.save()
    p.setClipRect(QRectF(0, hy, W, H - hy))
    p.translate(0, 2 * hy); p.scale(1, -1)
    draw_all(True)
    p.restore()
    for _ in range(50):
        y = rng.uniform(hy + 3, H)
        x = rng.uniform(0, W)
        p.setPen(QPen(_qc("#bfe8ff" if rng.random() < .5 else "#031018", rng.randint(10, 26)), 1.3))
        p.drawLine(QPointF(x, y), QPointF(x + rng.uniform(60, 260), y))
    for yc, h, a in ((.80, 110, 40), (.88, 80, 34)):               # drifting fog
        mg = QLinearGradient(0, H * yc - h / 2, 0, H * yc + h / 2)
        mg.setColorAt(0, _qc(fog, 0)); mg.setColorAt(.5, _qc(fog, a)); mg.setColorAt(1, _qc(fog, 0))
        p.fillRect(QRectF(0, H * yc - h / 2, W, h), QBrush(mg))


def _deco_rays(p, col):
    """Soft light shafts drifting through the mist from the upper right.
    Drawn once through _bloom at two blur radii so the shafts fade into the
    canopy instead of cutting the hard-edged polygon lines the old version did."""
    W, H = ART_W, ART_H
    rng = random.Random(15)

    def draw(q):
        q.setPen(Qt.PenStyle.NoPen)
        for i in range(5):
            x0 = W * (.52 + .08 * i) + rng.uniform(-30, 30)
            w0 = rng.uniform(24, 56)
            spread = rng.uniform(100, 190)
            off = 520 + rng.uniform(-60, 60)
            poly = QPolygonF([QPointF(x0, -60), QPointF(x0 + w0, -60),
                              QPointF(x0 + w0 + spread - off, H), QPointF(x0 - off, H)])
            g = QLinearGradient(0, 0, 0, H * .85)
            g.setColorAt(0, _qc(col, rng.uniform(32, 52)))
            g.setColorAt(.55, _qc(col, 13))
            g.setColorAt(1, _qc(col, 0))
            q.setBrush(QBrush(g))
            q.drawPolygon(poly)
    _bloom(p, draw, 5, .5)
    _bloom(p, draw, 15, .42)


def _pine(p, x, y, h):
    w = h * .30
    pts = [QPointF(x, y - h)]
    tiers = 4
    for k in range(1, tiers + 1):
        ty = y - h + h * k / tiers * .96
        ww = w * (k / tiers) ** .85
        pts.append(QPointF(x + ww, ty)); pts.append(QPointF(x + ww * .45, ty - h * .035))
    pts.append(QPointF(x + w * .10, y)); pts.append(QPointF(x - w * .10, y))
    for k in range(tiers, 0, -1):
        ty = y - h + h * k / tiers * .96
        ww = w * (k / tiers) ** .85
        pts.append(QPointF(x - ww * .45, ty - h * .035)); pts.append(QPointF(x - ww, ty))
    p.drawPolygon(QPolygonF(pts))


def _deco_woods(p, col):
    """Three depths of pine forest separated by drifting mist; the near trees frame the sides."""
    W, H = ART_W, ART_H
    rng = random.Random(33)
    p.setPen(Qt.PenStyle.NoPen)
    for base, hmin, hmax, c, a, edge_only in ((.74, 70, 150, "#143a33", 150, False), (.85, 110, 230, "#0a251f", 210, False),
                                              (.98, 220, 430, "#03100d", 252, True)):
        by = H * base
        p.setBrush(_qc(c, a))
        x = -30
        while x < W + 30:
            near_edge = x < W * .20 or x > W * .76
            if edge_only and not near_edge:
                x += rng.uniform(40, 90)
                continue
            h = rng.uniform(hmin, hmax) * (1.0 if not edge_only else (1.15 if x < W * .20 else .95))
            _pine(p, x, by + rng.uniform(-8, 8), h)
            x += rng.uniform(14, 34) * (hmax / 150 if edge_only else 1.0) * .6
        p.fillRect(QRectF(0, by, W, H - by), _qc(c, a))
        mist = QLinearGradient(0, by - 150, 0, by + 20)
        mist.setColorAt(0, _qc(col, 0)); mist.setColorAt(.7, _qc(col, 30 if not edge_only else 14)); mist.setColorAt(1, _qc(col, 0))
        p.fillRect(QRectF(0, by - 150, W, 170), QBrush(mist))


def _deco_fireflies(p, col):
    rng = random.Random(77)
    dots = [(rng.uniform(W_ * .05, W_ * .95), rng.uniform(H_ * .45, H_ * .93), rng.uniform(1.2, 2.6), rng.uniform(150, 255))
            for W_, H_ in [(ART_W, ART_H)] for _ in range(46)]

    def draw(q):
        q.setPen(Qt.PenStyle.NoPen)
        for x, y, r, a in dots:
            q.setBrush(_qc(col, a))
            q.drawEllipse(QPointF(x, y), r * 2.2, r * 2.2)
    _bloom(p, draw, 3, .85)
    _bloom(p, draw, 9, .8)
    p.setPen(Qt.PenStyle.NoPen)
    for x, y, r, a in dots:
        p.setBrush(_qc("#f4ffc8", a))
        p.drawEllipse(QPointF(x, y), r * .8, r * .8)


def _deco_marble(p, col):
    """Black marble with flowing, tapering gold veins (luxury lobby feel)."""
    W, H = ART_W, ART_H
    rng = random.Random(21)
    p.setPen(Qt.PenStyle.NoPen)
    for i in range(26):                                          # cloudy mottling
        x, y, r = rng.uniform(0, W), rng.uniform(0, H), rng.uniform(200, 520)
        g = QRadialGradient(QPointF(x, y), r)
        c = "#ffffff" if i % 3 else "#d4af6a"
        g.setColorAt(0, _qc(c, rng.uniform(10, 24))); g.setColorAt(1, _qc(c, 0))
        p.fillRect(QRectF(0, 0, W, H), QBrush(g))
    veins = []

    def grow(x, y, ang, n, w, depth):
        pts, off, vel = [(x, y)], 0.0, 0.0
        for _ in range(n):
            vel = vel * .90 - off * .028 + rng.uniform(-.05, .05)   # springs back toward the main direction
            off += vel
            a_ = ang + off
            st = rng.uniform(7, 11)
            x += math.cos(a_) * st
            y += math.sin(a_) * st
            pts.append((x, y))
            if depth < 2 and rng.random() < .022:
                grow(x, y, a_ + rng.choice((-1, 1)) * rng.uniform(.35, .7), int(n * rng.uniform(.25, .45)), w * .6, depth + 1)
        veins.append((pts, w, depth))
    for sx, sy, a, n, w in ((-20, H * .86, -.55, 230, 3.0), (W + 20, H * .16, 2.65, 210, 2.6),
                            (W * .62, H + 20, -1.75, 120, 2.0), (W * .10, -20, 1.2, 90, 1.6)):
        grow(sx, sy, a, n, w, 0)
    p.setBrush(Qt.BrushStyle.NoBrush)

    def stroke(chunk, pw, al, c):
        pen = QPen(_qc(c, al), pw)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        path = QPainterPath(QPointF(*chunk[0]))
        for q in chunk[1:]:
            path.lineTo(QPointF(*q))
        p.drawPath(path)
    for pts, w, depth in veins:
        n = len(pts)
        for i in range(0, n - 1, 5):
            chunk = pts[i:i + 6]
            t = (i + 3) / max(1, n)
            ww = w * (.25 + .75 * math.sin(math.pi * min(1.0, t)) ** .7)
            ww *= .55
            stroke(chunk, ww * 9, 7, col)
            stroke(chunk, ww * 4, 16, col)
            stroke(chunk, ww, 105, "#e3c98a")
            stroke(chunk, ww * .35, 80, "#fff0c8")
        if depth == 0:                                           # hairline echoes beside main veins
            for off in (-10, 15):
                shifted = []
                for k in range(1, n - 1):
                    dx, dy = pts[k + 1][0] - pts[k - 1][0], pts[k + 1][1] - pts[k - 1][1]
                    ln = math.hypot(dx, dy) or 1
                    shifted.append((pts[k][0] - dy / ln * (off + 4 * math.sin(k / 6.0)), pts[k][1] + dx / ln * (off + 4 * math.sin(k / 6.0))))
                for i in range(0, len(shifted) - 1, 5):
                    stroke(shifted[i:i + 6], .7, 34, "#e8cc88")
    p.setPen(Qt.PenStyle.NoPen)
    for pts, w, depth in veins:                                  # gold dust
        for q in pts[::10]:
            r = rng.uniform(.6, 1.7)
            p.setBrush(_qc("#ffe6a8", rng.uniform(30, 100)))
            p.drawEllipse(QPointF(q[0] + rng.uniform(-16, 16), q[1] + rng.uniform(-16, 16)), r, r)


def _deco_torii(p, col):
    """A vermilion torii gate in the corner, fading into the ground."""
    W, H = ART_W, ART_H
    p.setPen(Qt.PenStyle.NoPen)
    for x0 in (W * .74, W * .88):
        g = QLinearGradient(0, H * .60, 0, H)
        g.setColorAt(0, _qc(col, 190)); g.setColorAt(1, _qc(col, 30))
        p.setBrush(QBrush(g)); p.drawRect(QRectF(x0 - 13, H * .60, 26, H * .40))
    g = QLinearGradient(0, H * .56, 0, H * .62)
    g.setColorAt(0, _qc(col, 215)); g.setColorAt(1, _qc(col, 150))
    p.setBrush(QBrush(g))
    p.drawPolygon(QPolygonF([QPointF(W * .70, H * .585), QPointF(W * .715, H * .555), QPointF(W * .905, H * .555),
                             QPointF(W * .92, H * .585), QPointF(W * .905, H * .60), QPointF(W * .715, H * .60)]))
    p.setBrush(_qc(col, 170)); p.drawRect(QRectF(W * .715, H * .655, W * .19, 20))
    p.setBrush(_qc(col, 140)); p.drawRect(QRectF(W * .805, H * .60, 18, H * .055))
    gl = QRadialGradient(QPointF(W * .81, H * .72), 330)
    gl.setColorAt(0, _qc(col, 60)); gl.setColorAt(1, _qc(col, 0))
    p.setBrush(QBrush(gl)); p.drawEllipse(QPointF(W * .81, H * .72), 330, 330)


def _silk_ribbons(p, col, seed, alpha, n):
    """Soft translucent satin ribbons sweeping across the canvas."""
    rng = random.Random(seed)
    p.setPen(Qt.PenStyle.NoPen)
    for i in range(n):
        y0 = ART_H * rng.uniform(.05, .95)
        amp = rng.uniform(90, 230)
        ph = rng.uniform(0, 6.28)
        k = rng.uniform(.0016, .0030)
        wid = rng.uniform(70, 170)
        top, bot = [], []
        for x in range(-20, ART_W + 41, 20):
            c = y0 + amp * math.sin(x * k + ph) + (x - ART_W / 2) * rng.uniform(-.10, .10)
            w = wid * (.55 + .45 * math.sin(x * k * 1.6 + ph * 1.7))
            top.append(QPointF(x, c - w / 2))
            bot.append(QPointF(x, c + w / 2))
        poly = QPolygonF(top + bot[::-1])
        g = QLinearGradient(0, 0, ART_W, 0)
        g.setColorAt(0, _qc(col, 0)); g.setColorAt(.35, _qc(col, alpha)); g.setColorAt(.7, _qc(col, alpha * .5)); g.setColorAt(1, _qc(col, 0))
        p.setBrush(QBrush(g))
        p.drawPolygon(poly)


def _deco_silk(p, col):
    _silk_ribbons(p, col, 11, 38, 5)


def _deco_silk2(p, col):
    _silk_ribbons(p, col, 29, 40, 3)


_DECOS = {"silk": _deco_silk, "silk2": _deco_silk2, "waves": _deco_waves, "ridges": _deco_ridges, "petals": _deco_petals,
          "bokeh": _deco_bokeh, "vinyl": _deco_vinyl, "beams": _deco_beams,
          "sunset": _deco_sunset, "skyline": _deco_skyline, "fuji": _deco_fuji, "torii": _deco_torii,
          "aurora": _deco_aurora, "peaks": _deco_peaks, "marble": _deco_marble,
          "rays": _deco_rays, "woods": _deco_woods, "fireflies": _deco_fireflies}


def _grain(p, strength=11):
    """Fine film grain: removes gradient banding and gives a printed-paper texture."""
    tile = QImage(256, 256, QImage.Format.Format_ARGB32)
    rng = random.Random(1)
    for y in range(256):
        for x in range(256):
            v = rng.randint(0, 255)
            tile.setPixelColor(x, y, QColor(v, v, v, rng.randint(0, strength)))
    p.drawTiledPixmap(QRectF(0, 0, ART_W, ART_H).toRect(), QPixmap.fromImage(tile))


def _art_mesh(pal, spec):
    img, p = _canvas()
    b = spec["base"]
    if spec.get("diag"):
        g = QLinearGradient(0, 0, ART_W, ART_H)
        for pos, col in ((0, b[0]), (.5, b[1]), (1, b[2])):
            g.setColorAt(pos, QColor(col))
        p.fillRect(img.rect(), QBrush(g))
    else:
        p.fillRect(img.rect(), _vgrad([(0, b[0]), (.5, b[1]), (1, b[2])]))
    for x, y, r, col, a in spec["orbs"]:
        _orb(p, x, y, r, col, a)
    for name, dcol in (spec.get("decos") or [(spec.get("deco"), spec.get("deco_color"))]):
        fn = _DECOS.get(name)
        if fn:
            fn(p, dcol)
    # a thin bright rim of light along the very top edge
    g = QLinearGradient(0, 0, ART_W, 0)
    g.setColorAt(0, _qc("#ffffff", 0)); g.setColorAt(.5, _qc("#ffffff", 16)); g.setColorAt(1, _qc("#ffffff", 0))
    p.fillRect(QRectF(0, 0, ART_W, 2), QBrush(g))
    if spec.get("grain", 11):
        _grain(p, spec.get("grain", 11))
    return _finish(img, p, spec.get("vignette", 120), spec.get("vcolor", "#000000"))


_ARTISTS = {"plain": _art_plain, "mesh": _art_mesh}


def get_art(key):
    key = key if key in THEMES else DEFAULT_THEME
    if key not in _ART_CACHE:
        spec = THEMES[key]
        if spec["art"] == "mesh":
            _ART_CACHE[key] = _art_mesh(palette_for(key), spec)
        else:
            _ART_CACHE[key] = _ARTISTS[spec["art"]](palette_for(key))
    return _ART_CACHE[key]


class Backdrop(QWidget):
    """Central widget: paints the active theme's artwork behind every panel."""

    def __init__(self):
        super().__init__()
        self._key = None
        self._pm = None

    def paintEvent(self, _event):
        size = self.size()
        key = (CURRENT_THEME[0], size.width(), size.height())
        if self._key != key:
            art = get_art(CURRENT_THEME[0]).scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                                   Qt.TransformationMode.SmoothTransformation)
            self._pm = art.copy((art.width() - size.width()) // 2, (art.height() - size.height()) // 2,
                                size.width(), size.height())
            self._key = key
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._pm)


STYLE = """
* { font-family: 'Segoe UI', 'Microsoft JhengHei UI', 'Microsoft JhengHei'; color: @TEXT@; font-size: 14px; }
QMainWindow { background: @BASE@; }
QWidget { background: transparent; }
QDialog, QMessageBox { background: @PANEL_S@; }
QComboBox QFrame { background: @PANEL_S@; }
QLabel, QCheckBox, QSlider, QWidget#row { background: transparent; }
QToolTip { background: @RAISED_S@; color: @TEXT@; border: 1px solid @LINE_HI@; padding: 6px 8px; }
QScrollArea { border: none; background: transparent; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: @SCROLL@; border-radius: 4px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: @SCROLL_HI@; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: @SCROLL@; border-radius: 4px; min-width: 36px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

QFrame#nav { background: @PANEL_A@; border: 1px solid @LINE@; border-radius: 20px; }
QFrame#nowPanel, QFrame#settingsCard { background: @PANEL_A@; border: 1px solid @LINE@; border-radius: 18px; }

QLabel#brand { font-size: 19px; font-weight: 800; color: @HEAD@; }
QLabel#brandSub { font-size: 11px; color: @FAINT@; }
QLabel#title { font-size: 28px; font-weight: 700; color: @HEAD@; }
QLabel#subtitle { color: @MUTED@; }
QLabel#muted, QLabel#hint { color: @MUTED@; }
QLabel#hint { font-size: 12px; }
QLabel#sectionTitle { font-size: 16px; font-weight: 700; color: @HEAD@; }
QLabel#eyebrow { color: @FAINT@; font-size: 11px; font-weight: 700; }
QLabel#eyebrowAccent { color: @ACC_TEXT@; font-size: 11px; font-weight: 700; }
QLabel#nowTitle { font-size: 14px; font-weight: 400; color: @TEXT2@; }
QLabel#nowPath { font-size: 12px; color: @FAINT@; }
QLabel#settingLabel { color: @TEXT2@; font-weight: 600; }
QLabel#valueBadge { color: @TEXT2@; background: @RAISED_HI@; border-radius: 8px; padding: 3px 8px; font-weight: 700; }
QLabel#tipBar { color: @MUTED@; background: @BASE_A@; border: 1px solid @LINE@; border-radius: 12px; padding: 10px 14px; font-size: 12px; }
QLabel#privacyNote { color: @OK_FG2@; background: @OK_BG@; border-radius: 12px; padding: 12px; font-size: 12px; }
QLabel#privacyBanner { color: @OK_FG2@; background: @OK_BG@; border-radius: 12px; padding: 14px; }
QLabel#statusIdle { color: @OK_FG@; background: @OK_BG@; border: 1px solid @OK_LINE@; border-radius: 14px; padding: 7px 15px; font-weight: 600; }
QLabel#statusLive { color: @LIVE_FG@; background: @LIVE_BG@; border: 1px solid @LIVE_LINE@; border-radius: 14px; padding: 7px 15px; font-weight: 600; }
QLabel#statusPaused { color: @WARN_FG@; background: @WARN_BG@; border: 1px solid @WARN_LINE@; border-radius: 14px; padding: 7px 15px; font-weight: 600; }
QLabel#diagOk { color: @OK_FG@; background: @OK_BG@; border-radius: 12px; padding: 4px 13px; font-weight: 600; }
QLabel#diagOff { color: @MUTED@; background: @RAISED_S@; border-radius: 12px; padding: 4px 13px; }

QPushButton { background: @RAISED_A@; border: 1px solid @LINE_HI@; border-radius: 10px; padding: 10px 16px; font-weight: 600; color: @BTN_TEXT@; }
QPushButton:hover { background: @RAISED_HI_A@; border-color: @FOCUS@; }
QPushButton:pressed { background: @SEL@; }
QPushButton:disabled { color: @DISABLED@; background: @PANEL_A@; border-color: @LINE@; }
QPushButton#navButton { text-align: left; padding: 12px 14px 12px 12px; border: none; border-left: 3px solid transparent; border-radius: 12px; background: transparent; color: @MUTED@; }
QPushButton#navButton:hover { background: @RAISED_A@; color: @STRONG@; }
QPushButton#navButton:checked { background: @RAISED_HI_A@; color: @STRONG@; border-left: 3px solid @FOCUS_HI@; }
QPushButton#primary { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 @ACC_HI@, stop:1 @ACC_LO@); border: 1px solid @ACC_LINE@; color: #ffffff; font-size: 15px; padding: 6px 14px; }
QPushButton#primary:hover { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 @ACC_HOVER_HI@, stop:1 @ACC_HOVER_LO@); }
QPushButton#primary:pressed { background: @ACC_LO@; }
QPushButton#primary:disabled { background: @RAISED_A@; border-color: @LINE@; color: @DISABLED@; }
QPushButton#danger { background: @DANGER_BG@; border: 1px solid @DANGER_LINE@; color: @DANGER_FG@; font-size: 15px; padding: 6px 14px; }
QPushButton#danger:hover { background: @DANGER_HOVER@; border-color: @DANGER_HOVER_LINE@; }
QPushButton#danger:pressed { background: @DANGER_PRESSED@; }
QPushButton#hotkeyEdit { background: @BASE_A@; border: 1px solid @LINE_HI@; border-bottom: 3px solid @LINE_HI@; border-radius: 9px; padding: 8px 12px; }
QPushButton#hotkeyEdit:hover { border-color: @FOCUS@; }

QListWidget#trackList { background: @PANEL_A@; border: 1px solid @LINE@; border-radius: 16px; padding: 8px; outline: none; }
QListWidget#folderList { background: @BASE_A@; border: 1px solid @LINE_HI@; border-radius: 10px; padding: 4px; outline: none; }
QListWidget::item { border-radius: 9px; padding: 11px 12px; margin: 1px 2px; color: @TEXT2@; }
QListWidget::item:selected { background: @SEL@; color: @STRONG@; }
QListWidget::item:hover:!selected { background: @RAISED_A@; }

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: @BASE_A@; border: 1px solid @LINE_HI@; border-radius: 10px; padding: 9px 12px; min-height: 20px; selection-background-color: @SEL_LINE@; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: @FOCUS_HI@; }
QComboBox::drop-down { border: none; width: 34px; background: transparent; }
QComboBox::down-arrow { image: url(__CHEVRON__); width: 12px; height: 8px; }
QComboBox QAbstractItemView { background: @PANEL_S@; border: 1px solid @LINE_HI@; selection-background-color: @SEL@; outline: none; padding: 4px; }

QCheckBox { spacing: 10px; padding: 4px 0; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 6px; border: 1px solid @LINE_HI@; background: @BASE_A@; }
QCheckBox::indicator:hover { border-color: @FOCUS@; }
QCheckBox::indicator:checked { background: @FOCUS@; border-color: @FOCUS@; image: url(__TICK__); }

QSlider::groove:horizontal { background: @SEL@; height: 6px; border-radius: 3px; }
QSlider::sub-page:horizontal { background: @FOCUS@; border-radius: 3px; }
QSlider::handle:horizontal { background: #ffffff; border: 3px solid @FOCUS@; width: 12px; height: 12px; margin: -6px 0; border-radius: 9px; }
QSlider::handle:horizontal:hover { border-color: @FOCUS_HI@; }

QFrame#nowCard { background: @RAISED_A@; border: 1px solid @ACC_BORDER@; border-radius: 14px; }
QLabel#cardTitle { font-size: 17px; font-weight: 700; color: @STRONG@; }
QSlider#cardSeek::groove:horizontal { background: @SEL@; height: 4px; border-radius: 2px; }
QSlider#cardSeek::sub-page:horizontal { background: @ACC_BAR@; border-radius: 2px; }
QSlider#cardSeek[paused="true"]::sub-page:horizontal { background: #f5c26b; }
QSlider#cardSeek::handle:horizontal { background: #ffffff; border: none; width: 10px; height: 10px; margin: -3px 0; border-radius: 5px; }
QPushButton#segL, QPushButton#segM, QPushButton#segR { background: @BASE_A@; border: 1px solid @LINE@; color: @MUTED@; padding: 9px 18px; border-radius: 0; font-weight: 600; }
QPushButton#segL { border-top-left-radius: 10px; border-bottom-left-radius: 10px; }
QPushButton#segM { border-left: none; }
QPushButton#segR { border-top-right-radius: 10px; border-bottom-right-radius: 10px; border-left: none; }
QPushButton#segL:hover, QPushButton#segM:hover, QPushButton#segR:hover { background: @RAISED_A@; color: @STRONG@; }
QPushButton#segL:checked, QPushButton#segM:checked, QPushButton#segR:checked { background: @SEL@; border-color: @SEL_LINE@; color: @STRONG@; }
QPushButton#chip { background: @BASE_A@; border: 1px solid @LINE@; color: @MUTED@; padding: 0 15px; border-radius: 15px; font-weight: 600; font-size: 12px; }
QPushButton#chip:hover { background: @RAISED_A@; color: @STRONG@; }
QPushButton#chip:checked { background: @SEL@; border-color: @SEL_LINE@; color: @STRONG@; }
QPushButton#chip:disabled { color: @DISABLED@; background: @PANEL_A@; }
QPushButton#pauseBtn { background: @PAUSE_BG@; border: 1px solid @PAUSE_LINE@; color: @PAUSE_FG@; font-size: 15px; padding: 6px 10px; }
QPushButton#pauseBtn:hover { background: @PAUSE_HOVER@; border-color: @PAUSE_HOVER_LINE@; }
QPushButton#pauseBtn:disabled { color: @DISABLED@; background: @PANEL_A@; border-color: @LINE@; }
QPushButton#pauseBtn[resume="true"] { background: #f5c26b; border-color: #f5c26b; color: #1a1405; }
QPushButton#pauseBtn[resume="true"]:hover { background: #ffd283; }
QPushButton#ghost { background: transparent; border: 1px solid @LINE_HI@; color: @MUTED@; font-size: 12px; padding: 6px 14px; }
QPushButton#ghost:hover { background: @RAISED_A@; color: @STRONG@; }
QPushButton#ghost:disabled { color: @DISABLED@; border-color: @LINE@; }
QLabel#emptyState { color: @MUTED@; background: @PANEL_A@; border: 1px dashed @LINE_HI@; border-radius: 16px; padding: 28px; }
QMenu { background: @PANEL_S@; border: 1px solid @LINE_HI@; border-radius: 12px; padding: 6px; }
QMenu::item { padding: 9px 26px 9px 14px; border-radius: 8px; color: @BTN_TEXT@; }
QMenu::item:selected { background: @SEL@; color: @STRONG@; }
QMenu::item:disabled { color: @DISABLED@; }
QMenu::separator { height: 1px; background: @LINE@; margin: 6px 8px; }
"""


def build_stylesheet():
    """STYLE plus two tiny generated images (combo-box arrow, checkbox tick)."""
    import tempfile
    sheet = STYLE
    for key in sorted(T, key=len, reverse=True):
        sheet = sheet.replace(f"@{key}@", T[key])
    try:
        folder = Path(tempfile.gettempdir()) / "EchoDeck_ui"
        folder.mkdir(parents=True, exist_ok=True)
        chevron = QPixmap(24, 16)
        chevron.fill(Qt.GlobalColor.transparent)
        tick = QPixmap(36, 36)
        tick.fill(Qt.GlobalColor.transparent)
        for pix, color, width, points in (
            (chevron, T.get("CHEVRON", "#a3a8c3"), 3.2, [(4, 4), (12, 12), (20, 4)]),
            (tick, "#ffffff", 5.0, [(9, 19), (15, 26), (27, 11)]),
        ):
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor(color), width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath(QPointF(*points[0]))
            for point in points[1:]:
                path.lineTo(QPointF(*point))
            painter.drawPath(path)
            painter.end()
        chevron_path, tick_path = folder / "chevron.png", folder / "tick.png"
        if not (chevron.save(str(chevron_path)) and tick.save(str(tick_path))):
            raise OSError("could not write UI images")
        sheet = sheet.replace("__CHEVRON__", chevron_path.as_posix()).replace("__TICK__", tick_path.as_posix())
    except Exception:
        sheet = sheet.replace("url(__CHEVRON__)", "none").replace("url(__TICK__)", "none")
    return sheet


if __name__ == "__main__":
    app = QApplication(sys.argv)
    # Single-instance guard: two EchoDeck processes would both write the
    # microphone into the virtual cable, and the old one keeps leaking voice.
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\EchoDeckSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        QMessageBox.warning(None, APP_NAME,
            "EchoDeck 已經在執行中。\n\n請到工作管理員結束所有 EchoDeck.exe 後再重新開啟，"
            "否則舊版會繼續把麥克風送進虛擬聲卡。")
        sys.exit(0)
    try:
        set_theme(json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("theme", DEFAULT_THEME))
    except (OSError, ValueError, AttributeError):
        pass
    app.setStyleSheet(build_stylesheet())
    app.setWindowIcon(make_app_icon())
    window = EchoDeck()
    window.show()
    sys.exit(app.exec())
