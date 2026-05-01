"""Port of `Base.KEY_MAP` and `KVMUtil.javaCodeToUSB`.

Java's `translateToUSBCode(KeyEvent)` chooses between two paths:

1. For "action keys" (modifiers, function keys, navigation), or any
   key on `KeyEvent.KEY_LOCATION_NUMPAD` (==4), or specific virtual
   codes (Ctrl=17 / Shift=16 / Alt=18 / AltGraph=65406): use
   `javaCodeToUSB`, which looks up the Java AWT virtual key code in
   `KVMUtil.keyCode[][]`.

2. Otherwise (printable letters/digits/symbols): use the OS-level
   physical scancode in `Base.KEY_MAP[][]` (Linux) or the Windows
   equivalent. This is what gives PT-BR / DE-QWERTZ / etc. keyboards
   their layout-specific mapping.

In a browser the JavaScript runtime gives us either `e.code`
(physical position, US-QWERTY-relative) or `e.key` (logical,
layout-aware). For parity with the Java applet we want the OS
scancode path — the closest browser equivalent is `e.code`, which is
layout-neutral. This module exposes BOTH paths so callers can pick.
"""

from __future__ import annotations

# --- Linux scancode → USB HID Usage ID --------------------------------------
# Direct port of `Base.KEY_MAP` (Base.java:289). Pairs (scancode, hid).
# USB_KEY_CTRL=224, USB_KEY_SHIFT=225, USB_KEY_ALT=226 per Base.java:286-288.
USB_KEY_CTRL = 224
USB_KEY_SHIFT = 225
USB_KEY_ALT = 226

LINUX_SCAN_TO_HID: dict[int, int] = {
    1: 41,  # Esc
    2: 30,
    3: 31,
    4: 32,
    5: 33,
    6: 34,  # 1..5
    7: 35,
    8: 36,
    9: 37,
    10: 38,
    11: 39,  # 6..0
    12: 45,
    13: 46,  # - =
    14: 42,
    15: 43,  # Backspace, Tab
    16: 20,
    17: 26,
    18: 8,
    19: 21,
    20: 23,  # Q W E R T
    21: 28,
    22: 24,
    23: 12,
    24: 18,
    25: 19,  # Y U I O P
    26: 47,
    27: 48,  # [ ]
    28: 40,  # Enter
    29: USB_KEY_CTRL,  # LCtrl
    30: 4,
    31: 22,
    32: 7,
    33: 9,
    34: 10,  # A S D F G
    35: 11,
    36: 13,
    37: 14,
    38: 15,  # H J K L
    39: 51,
    40: 52,
    41: 53,  # ; ' `
    42: USB_KEY_SHIFT,  # LShift
    43: 49,  # backslash
    44: 29,
    45: 27,
    46: 6,
    47: 25,
    48: 5,  # Z X C V B
    49: 17,
    50: 16,
    51: 54,
    52: 55,
    53: 56,  # N M , . /
    54: USB_KEY_SHIFT,  # RShift
    55: 70,  # Numpad *
    56: USB_KEY_ALT,  # LAlt (and RAlt — Java has both)
    57: 44,  # Space
    58: 57,  # CapsLock
    82: 73,  # Insert
    83: 76,  # Delete
    86: 100,  # IntlBackslash
}


# --- KeyboardEvent.code (Web) → USB HID Usage ID ----------------------------
# `e.code` is layout-neutral (always reports the physical key position
# based on a US-QWERTY layout). USB HID is also layout-neutral, so
# this is the cleanest mapping for the web client.
WEB_CODE_TO_HID: dict[str, int] = {
    "KeyA": 0x04,
    "KeyB": 0x05,
    "KeyC": 0x06,
    "KeyD": 0x07,
    "KeyE": 0x08,
    "KeyF": 0x09,
    "KeyG": 0x0A,
    "KeyH": 0x0B,
    "KeyI": 0x0C,
    "KeyJ": 0x0D,
    "KeyK": 0x0E,
    "KeyL": 0x0F,
    "KeyM": 0x10,
    "KeyN": 0x11,
    "KeyO": 0x12,
    "KeyP": 0x13,
    "KeyQ": 0x14,
    "KeyR": 0x15,
    "KeyS": 0x16,
    "KeyT": 0x17,
    "KeyU": 0x18,
    "KeyV": 0x19,
    "KeyW": 0x1A,
    "KeyX": 0x1B,
    "KeyY": 0x1C,
    "KeyZ": 0x1D,
    "Digit1": 0x1E,
    "Digit2": 0x1F,
    "Digit3": 0x20,
    "Digit4": 0x21,
    "Digit5": 0x22,
    "Digit6": 0x23,
    "Digit7": 0x24,
    "Digit8": 0x25,
    "Digit9": 0x26,
    "Digit0": 0x27,
    "Enter": 0x28,
    "Escape": 0x29,
    "Backspace": 0x2A,
    "Tab": 0x2B,
    "Space": 0x2C,
    "Minus": 0x2D,
    "Equal": 0x2E,
    "BracketLeft": 0x2F,
    "BracketRight": 0x30,
    "Backslash": 0x31,
    "Semicolon": 0x33,
    "Quote": 0x34,
    "Backquote": 0x35,
    "Comma": 0x36,
    "Period": 0x37,
    "Slash": 0x38,
    "CapsLock": 0x39,
    "F1": 0x3A,
    "F2": 0x3B,
    "F3": 0x3C,
    "F4": 0x3D,
    "F5": 0x3E,
    "F6": 0x3F,
    "F7": 0x40,
    "F8": 0x41,
    "F9": 0x42,
    "F10": 0x43,
    "F11": 0x44,
    "F12": 0x45,
    "PrintScreen": 0x46,
    "ScrollLock": 0x47,
    "Pause": 0x48,
    "Insert": 0x49,
    "Home": 0x4A,
    "PageUp": 0x4B,
    "Delete": 0x4C,
    "End": 0x4D,
    "PageDown": 0x4E,
    "ArrowRight": 0x4F,
    "ArrowLeft": 0x50,
    "ArrowDown": 0x51,
    "ArrowUp": 0x52,
    "NumLock": 0x53,
    "NumpadDivide": 0x54,
    "NumpadMultiply": 0x55,
    "NumpadSubtract": 0x56,
    "NumpadAdd": 0x57,
    "NumpadEnter": 0x58,
    "Numpad1": 0x59,
    "Numpad2": 0x5A,
    "Numpad3": 0x5B,
    "Numpad4": 0x5C,
    "Numpad5": 0x5D,
    "Numpad6": 0x5E,
    "Numpad7": 0x5F,
    "Numpad8": 0x60,
    "Numpad9": 0x61,
    "Numpad0": 0x62,
    "NumpadDecimal": 0x63,
    "IntlBackslash": 0x64,
    "ContextMenu": 0x65,
    "IntlRo": 0x87,
    "IntlYen": 0x89,
}


# --- USB HID modifier mask --------------------------------------------------
# The Java applet derives the modifier byte from `KeyEvent.getKeyLocation()`
# + `isControlDown/isShiftDown/isAltDown/isMetaDown`. Browsers give us
# `e.code` directly with discrete codes for left/right modifiers —
# same as the USB HID spec.
WEB_CODE_TO_MOD: dict[str, int] = {
    "ControlLeft": 0x01,
    "ShiftLeft": 0x02,
    "AltLeft": 0x04,
    "MetaLeft": 0x08,
    "ControlRight": 0x10,
    "ShiftRight": 0x20,
    "AltRight": 0x40,
    "MetaRight": 0x80,
}


def hid_for_web_code(code: str) -> int | None:
    """Look up an HID usage ID for a `KeyboardEvent.code` string.

    Returns `None` for keys we don't map (caller should ignore the
    event rather than send a stray scancode).
    """
    return WEB_CODE_TO_HID.get(code)


def mod_for_web_code(code: str) -> int | None:
    """Return the HID modifier bit for a modifier key, else `None`."""
    return WEB_CODE_TO_MOD.get(code)
