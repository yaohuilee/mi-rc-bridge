"""Convert Xiaomi gyroscope reports into relative Windows pointer motion."""

from collections import deque
import ctypes
from ctypes import wintypes
import math
from statistics import median
import time

from remote_keys import Key

CALIBRATION_SAMPLES = 45
SAMPLE_PERIOD = 1 / 52
SMOOTHING = 1 - math.exp(-SAMPLE_PERIOD / 0.035)
DEAD_ZONE = 45
POINTER_BUTTONS = (Key.CONFIRM, Key.BACK)
SCROLL_KEYS = Key.UP | Key.DOWN


class MouseInput(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class Input(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('mouse', MouseInput)]


class WindowsMouse:
    def __init__(self):
        self.dll = ctypes.WinDLL('user32', use_last_error=True)
        self.dll.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.dll.SendInput.restype = wintypes.UINT

    def send(self, flags, x=0, y=0, data=0):
        packet = Input(0, MouseInput(x, y, data, flags, 0, 0))
        if self.dll.SendInput(1, ctypes.byref(packet), ctypes.sizeof(packet)) != 1:
            raise OSError('Windows did not accept the mouse event.')

    def move(self, x, y):
        self.send(0x0001 | 0x2000, x, y)

    def button(self, key, pressed):
        down, up = (0x0002, 0x0004) if key == Key.CONFIRM else (0x0008, 0x0010)
        self.send(down if pressed else up)

    def wheel(self, direction):
        self.send(0x0800, data=120 * direction)


class AirMouse:
    def __init__(self, mouse):
        self.mouse = mouse
        self.enabled = False
        self.source_keys = self.buttons_down = Key(0)
        self.wheel_direction = 0
        self.wheel_due = 0.0
        self.bias = None
        self.calibration = deque(maxlen=CALIBRATION_SAMPLES)
        self.filtered = [0.0, 0.0]
        self.remainder = [0.0, 0.0]

    @property
    def ready(self):
        return self.enabled and self.bias is not None

    def release(self):
        for key in POINTER_BUTTONS:
            if self.buttons_down & key:
                self.mouse.button(key, False)
                self.buttons_down &= ~key
        self.wheel_direction = 0

    def pause(self):
        self.release()
        self.calibration.clear()
        self.filtered[:] = (0, 0)
        self.remainder[:] = (0, 0)

    def calibrate(self):
        self.pause()
        self.bias = None

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.calibrate()

    def set_keys(self, keys):
        previous, self.source_keys = self.source_keys, keys
        for key in POINTER_BUTTONS:
            if self.buttons_down & key and not keys & key:
                self.mouse.button(key, False)
                self.buttons_down &= ~key
            elif self.ready and keys & key and not previous & key:
                self.mouse.button(key, True)
                self.buttons_down |= key

        scroll = keys & SCROLL_KEYS
        if scroll not in (Key.UP, Key.DOWN):
            self.wheel_direction = 0
        elif self.ready and not previous & scroll:
            self.wheel_direction = 1 if scroll == Key.UP else -1
            self.mouse.wheel(self.wheel_direction)
            self.wheel_due = time.monotonic() + 0.4

    def repeat_scroll(self, now):
        if self.ready and self.wheel_direction and now >= self.wheel_due:
            self.mouse.wheel(self.wheel_direction)
            self.wheel_due = now + 0.13

    def update(self, gyro, sensitivity):
        if not self.enabled:
            return
        if self.bias is None:
            if max(map(abs, gyro)) > 250:
                self.calibration.clear()
                return
            self.calibration.append(gyro)
            if len(self.calibration) == CALIBRATION_SAMPLES:
                columns = list(zip(*self.calibration))
                if all(max(axis) - min(axis) < 65 for axis in columns):
                    self.bias = [float(median(axis)) for axis in columns]
            return

        corrected = [value - bias for value, bias in zip(gyro, self.bias)]
        if max(map(abs, corrected)) <= DEAD_ZONE:
            for axis in range(3):
                self.bias[axis] += corrected[axis] * 0.006
            self.filtered[:] = (0, 0)
            self.remainder[:] = (0, 0)
            return

        delta = []
        # With the buttons facing up, yaw about Z moves sideways and pitch about X moves vertically.
        for axis, raw in enumerate((-corrected[2], -corrected[0])):
            self.filtered[axis] += (raw - self.filtered[axis]) * SMOOTHING
            filtered = self.filtered[axis]
            speed = math.copysign(max(0, abs(filtered) - DEAD_ZONE), filtered)
            pixels = max(-72, min(72, speed * SAMPLE_PERIOD * 0.52 * sensitivity))
            total = pixels + self.remainder[axis]
            step = math.trunc(total)
            self.remainder[axis] = total - step
            delta.append(step)
        if any(delta):
            self.mouse.move(*delta)
