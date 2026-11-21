"""Bridge the Xiaomi 2717:32B0 BLE remote through an existing USBPcap driver."""

import argparse
from collections import deque
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import datetime
import json
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import sys
import threading
import time


USBPCAP = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "USBPcap" / "USBPcapCMD.exe"
REPORT_HANDLE = 0x0030
MOTION_REPORT_HANDLE = 0x0037
EXTENDED = 0x0001
KEY_UP = 0x0002
INPUT_MARKER = 0x4D495243

# HID usage: (display name, Windows virtual key, extended key, repeat).
KEYS = {
    0x52: ("Up", 0x26, True, True),
    0x51: ("Down", 0x28, True, True),
    0x50: ("Left", 0x25, True, True),
    0x4F: ("Right", 0x27, True, True),
    0x28: ("Enter", 0x0D, False, False),
    0x80: ("Volume up", 0xAF, True, True),
    0x81: ("Volume down", 0xAE, True, True),
    0x3E: ("Application menu", 0x5D, True, False),
}


class KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t),
    ]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t),
    ]


class InputData(ctypes.Union):
    _fields_ = [("keyboard", KeyboardInput), ("mouse", MouseInput)]


class Input(ctypes.Structure):
    _fields_ = [("kind", wintypes.DWORD), ("data", InputData)]


def inject(vk, *flags):
    batch = (Input * len(flags))(*(
        Input(1, InputData(keyboard=KeyboardInput(vk, 0, flag, 0, INPUT_MARKER)))
        for flag in flags
    ))
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    send_input = user32.SendInput
    send_input.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    send_input.restype = wintypes.UINT
    sent = send_input(len(batch), batch, ctypes.sizeof(Input))
    if sent != len(batch):
        error = ctypes.get_last_error()
        if sent == 1 and len(batch) == 2:
            send_input(1, ctypes.byref(batch[1]), ctypes.sizeof(Input))
        raise OSError(f"SendInput accepted {sent}/{len(batch)} actions (Win32 {error})")


class NativeRightFilter:
    """Correlate the physical Right event with the remote's Confirm report."""

    WINDOW = 0.080

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32")
        self.ready = threading.Event()
        self.events = queue.SimpleQueue()
        self.pending = deque()
        self.enabled = False
        self.confirm = False
        self.press_time = float("-inf")
        self.release_time = float("-inf")
        self.native_held = False
        self.error = None

    def __enter__(self):
        self.thread = threading.Thread(target=self.hook, daemon=True)
        self.thread.start()
        if not self.ready.wait(3):
            raise OSError("Timed out installing the Right-key filter")
        if self.error:
            raise self.error
        return self

    def hook(self):
        class HookKey(ctypes.Structure):
            _fields_ = [
                ("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ctypes.c_size_t),
            ]

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        api = self.user32
        api.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
        api.SetWindowsHookExW.restype = wintypes.HHOOK
        api.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        api.CallNextHookEx.restype = ctypes.c_ssize_t
        api.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        api.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        api.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.thread_id = self.kernel32.GetCurrentThreadId()

        @callback_type
        def callback(code, message, data):
            if code == 0 and self.enabled:
                key = HookKey.from_address(data)
                if key.vk == 0x27 and not key.flags & 0x10:
                    self.events.put((time.monotonic(), bool(key.flags & 0x80), bool(key.flags & 1)))
                    return 1
            return api.CallNextHookEx(None, code, message, data)

        hook = api.SetWindowsHookExW(13, callback, self.kernel32.GetModuleHandleW(None), 0)
        if not hook:
            self.error = ctypes.WinError(ctypes.get_last_error())
            self.ready.set()
            return
        message = wintypes.MSG()
        api.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self.ready.set()
        try:
            while api.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                pass
        finally:
            api.UnhookWindowsHookEx(hook)

    def update(self, confirm, now):
        if confirm and not self.confirm:
            self.press_time = now
        if self.confirm and not confirm:
            self.release_time = now
        self.confirm = confirm

    def flush(self, now, *, force=False):
        while not self.events.empty():
            self.pending.append(self.events.get_nowait())
        while self.pending and (force or self.pending[0][0] <= now - self.WINDOW):
            stamp, up, extended = self.pending.popleft()
            near_press = abs(stamp - self.press_time) <= self.WINDOW
            near_release = abs(stamp - self.release_time) <= self.WINDOW
            during_hold = self.press_time <= stamp <= (
                self.press_time + 5 if self.confirm else self.release_time
            )
            suppress = (up and near_release) or (not up and (near_press or during_hold))
            if up and self.native_held:
                suppress = False
            if suppress:
                log(f"Filtered duplicate native Right {'up' if up else 'down'}.")
                continue
            inject(0x27, (EXTENDED if extended else 0) | (KEY_UP if up else 0))
            self.native_held = not up

    def __exit__(self, *exception):
        self.enabled = False
        self.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        self.thread.join(timeout=1)
        self.flush(time.monotonic(), force=True)


def log(message):
    print(f"{datetime.datetime.now().astimezone().isoformat(timespec='milliseconds')} {message}", flush=True)


def read_packets(stream, on_ready=lambda: None):
    header = stream.read(24)
    if len(header) != 24 or header[:4] not in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        raise ValueError("Expected a little-endian USBPcap .pcap stream")
    if struct.unpack_from("<I", header, 20)[0] != 249:
        raise ValueError("Expected USBPcap link type 249")
    fraction_scale = 1_000_000 if header[0] == 0x4D else 1_000
    on_ready()
    while record := stream.read(16):
        if len(record) != 16:
            raise ValueError("Incomplete pcap record header")
        seconds, fraction, size, original_size = struct.unpack("<IIII", record)
        if size > min(original_size, 1_048_576):
            raise ValueError(f"Invalid pcap record length: {size}")
        packet = stream.read(size)
        if len(packet) != size:
            raise ValueError("Incomplete pcap record")
        yield seconds * 1000 + fraction / fraction_scale, packet


class RemoteReports:
    def __init__(self, usb_address, connection_handle=None, on_link_change=log, trace_reports=False,
                 remote_address=None):
        self.usb_address = usb_address
        self.connection_handle = connection_handle
        self.remote_address = remote_address
        self.remote_address_bytes = bytes.fromhex(remote_address.replace(":", ""))[::-1] if remote_address else None
        self.on_link_change = on_link_change
        self.trace_reports = trace_reports
        self.fragment = b""
        self.read_requests = deque(maxlen=512)
        self.disconnected_at = {}

    def receive(self, packet, timestamp=0):
        if len(packet) < 27:
            return None
        header_len, _, status, _, info, _, device, endpoint, transfer, data_len = struct.unpack_from(
            "<HQIHBHHBBI", packet
        )
        if device != self.usb_address or status != 0:
            return None
        if header_len < 27 or header_len + data_len > len(packet):
            return None
        data = packet[header_len:header_len + data_len]
        if endpoint == 0x02 and transfer == 3 and not info & 1:
            self.record_att_read(data, timestamp)
            return None
        if not info & 1:
            return None
        if endpoint == 0x81 and transfer == 1:
            return self.check_connection(data, timestamp)
        if endpoint != 0x82 or transfer != 3 or len(data) < 4:
            return None
        if self.connection_handle is None:
            return None
        handle_flags, length = struct.unpack_from("<HH", data)
        if handle_flags & 0x0FFF != self.connection_handle or len(data) != length + 4:
            return None
        boundary = (handle_flags >> 12) & 3
        if boundary == 1:
            if not self.fragment:
                return None
            self.fragment += data[4:]
        else:
            self.fragment = data[4:]
        if len(self.fragment) < 4:
            return None
        size, channel = struct.unpack_from("<HH", self.fragment)
        if len(self.fragment) < size + 4:
            return None
        att, self.fragment = self.fragment[4:], b""
        if channel != 4 or len(att) != size or len(att) < 3 or att[0] not in (0x1B, 0x1D):
            return None
        attribute = struct.unpack_from("<H", att, 1)[0]
        if self.trace_reports:
            payload = att[3:]
            if attribute == MOTION_REPORT_HANDLE and len(payload) == 12:
                ax, ay, az, gx, gy, gz = struct.unpack("<6h", payload)
                log(f"Motion raw: acceleration=({ax}, {ay}, {az}) angular_velocity=({gx}, {gy}, {gz})")
            contents = payload.hex(" ").upper() if len(payload) <= 8 or attribute == MOTION_REPORT_HANDLE else "(payload omitted)"
            log(f"ATT 0x{attribute:04X}, {len(payload)} bytes: {contents}")
        if attribute != REPORT_HANDLE or len(att) != 11 or att[0] != 0x1B:
            return None
        usages = struct.unpack_from("<3H", att, 5)
        return tuple(dict.fromkeys(usage for usage in usages if usage in KEYS))

    def record_att_read(self, data, timestamp):
        if len(data) != 11:
            return
        handle_flags, length, channel_size, channel, opcode, attribute = struct.unpack("<HHHHBH", data)
        if length == 7 and channel_size == 3 and channel == 4 and opcode == 0x0A:
            self.read_requests.append((timestamp, handle_flags & 0x0FFF, attribute))

    def bind_from_probe(self, observations):
        if self.connection_handle is not None:
            return True
        if len({item["handle"] for item in observations}) < 2:
            return False
        candidates = None
        for item in observations:
            matching = {
                handle for stamp, handle, attribute in self.read_requests
                if attribute == item["handle"] and item["start"] - 10 <= stamp <= item["end"] + 10
                and self.disconnected_at.get(handle, 0) < stamp
            }
            candidates = matching if candidates is None else candidates & matching
        if len(candidates) != 1:
            return False
        self.connection_handle = candidates.pop()
        self.on_link_change(f"MI RC bound to existing HCI connection 0x{self.connection_handle:04X}.")
        return True

    def check_connection(self, event, timestamp=0):
        if len(event) >= 6 and event[0] == 0x05 and event[2] == 0:
            handle = struct.unpack_from("<H", event, 3)[0]
            self.disconnected_at[handle] = timestamp
            if handle == self.connection_handle:
                self.connection_handle = None
                self.fragment = b""
                self.on_link_change("Remote disconnected; waiting for the same Bluetooth address.")
                return ()
        if len(event) >= 14 and event[0] == 0x3E and event[2] in (0x01, 0x0A, 0x29):
            if event[3] == 0 and event[8:14] == self.remote_address_bytes:
                self.connection_handle = struct.unpack_from("<H", event, 4)[0]
                self.fragment = b""
                self.on_link_change(f"MI RC bound to HCI connection 0x{self.connection_handle:04X}.")
                return ()
        return None


class Bridge:
    def __init__(self, emit):
        self.emit = emit
        self.held = {}
        self.last_report = 0

    def update(self, usages, now):
        previous = self.held
        self.held = {}
        for usage in usages:
            if usage not in previous:
                self.emit(usage, "press")
            self.held[usage] = previous.get(usage, now + 0.50)
        self.last_report = now

    def repeat(self, now):
        # Limit repeating if the USB capture or link loses a release report.
        if now - self.last_report > 5.0:
            return
        for usage, due in self.held.items():
            if KEYS[usage][3] and now >= due:
                self.emit(usage, "repeat")
                self.held[usage] = now + 0.09


def process_capture(stream, reports, bridge, seconds, stop_file, right_filter=None, identify_existing=False):
    messages = queue.Queue(maxsize=256)
    ready = object()

    def read():
        try:
            for packet in read_packets(stream, lambda: messages.put(ready)):
                messages.put(packet)
        except (OSError, ValueError) as error:
            messages.put(error)
        finally:
            messages.put(None)

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + seconds if seconds else float("inf")
    probe = None
    observations = None
    probe_deadline = 0
    try:
        while time.monotonic() < deadline and not (stop_file and stop_file.exists()):
            try:
                packet = messages.get(timeout=0.010)
            except queue.Empty:
                packet = ()
            if packet is None:
                return
            if isinstance(packet, Exception):
                raise packet
            if packet is ready and identify_existing and reports.connection_handle is None:
                log("Looking up the already-connected MI RC.")
                probe = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-File",
                     str(Path(__file__).with_name("Probe-MiRcConnection.ps1")), reports.remote_address],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
                )
                probe_deadline = time.monotonic() + 20
            now = time.monotonic()
            usages = None
            if isinstance(packet, tuple) and packet:
                stamp, data = packet
                usages = reports.receive(data, stamp)
            if probe is not None and probe.poll() is not None:
                output, error = probe.communicate()
                probe = None
                observations = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
                if error and not observations:
                    log(error.splitlines()[0])
                probe_deadline = now + 0.5
            if observations is not None and reports.bind_from_probe(observations):
                observations = None
            elif probe_deadline and now > probe_deadline and (probe is not None or observations is not None):
                if probe is not None:
                    probe.terminate()
                    probe.communicate()
                    probe = None
                observations = None
                if reports.connection_handle is None:
                    log("Existing link not identified. Press a remote key to wake it; new BLE connections are detected automatically.")
            if right_filter:
                right_filter.enabled = reports.connection_handle is not None
                if usages is not None:
                    right_filter.update(0x28 in usages, now)
                right_filter.flush(now)
            if usages is not None:
                bridge.update(usages, now)
            bridge.repeat(now)
    finally:
        if probe is not None:
            if probe.poll() is None:
                probe.terminate()
            probe.communicate()


def find_intel_adapter(interface):
    config = subprocess.check_output(
        [str(USBPCAP), "--extcap-version", "4.6", "--extcap-interface", interface, "--extcap-config"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    matches = re.findall(rb"(?im)^value .*?\{value=(\d+)\}\{display=\[\d+\][^\r\n]*?Bluetooth\(R\)", config)
    if len(matches) != 1:
        raise OSError(f"Expected one Intel Bluetooth adapter on {interface}; found {len(matches)}")
    return int(matches[0])


@contextmanager
def single_instance():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, r"Local\MiRcUsbPcapKeys")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:
            raise OSError("An MI RC bridge is already running in this Windows session.")
        yield
    finally:
        kernel.CloseHandle(mutex)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", help="BLE address; defaults to the repository's remote-address.txt")
    parser.add_argument("--usb-interface", default=r"\\.\USBPcap1")
    parser.add_argument("--usb-address", type=int)
    parser.add_argument("--connection-handle", type=lambda value: int(value, 0))
    parser.add_argument("--seconds", type=int, default=0)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--replay", type=Path, help="decode an existing USBPcap .pcap without injecting keys")
    parser.add_argument("--dry-run", action="store_true", help="log actions without injecting keys")
    parser.add_argument("--trace-reports", action="store_true", help="inspect remote reports without injecting keys")
    parser.add_argument("--filter-native-right", action="store_true",
                        help="enable the historical global right-arrow deduplication hook")
    args = parser.parse_args()
    args.dry_run = args.dry_run or args.trace_reports
    if (args.usb_address is not None and not 1 <= args.usb_address <= 127) or args.seconds < 0:
        parser.error("USB address must be 1–127 and seconds must be non-negative")
    if args.connection_handle is not None and not 0 <= args.connection_handle <= 0xEFF:
        parser.error("Invalid HCI connection handle")
    address_file = Path(__file__).resolve().parent.parent / "remote-address.txt"
    address = args.address
    if address is None and address_file.is_file():
        address = address_file.read_text(encoding="utf-8-sig")
    if address is not None:
        address = address.strip().upper()
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
            parser.error("Bluetooth address must be in XX:XX:XX:XX:XX:XX form")
    if address is None and args.connection_handle is None:
        parser.error("Supply a remote address or a known HCI connection handle")

    def emit(usage, action):
        if usage == 0x3E and not (args.dry_run or args.replay):
            log("Application menu: Windows handles its native event.")
            return
        if not (args.dry_run or args.replay):
            _, vk, extended, _ = KEYS[usage]
            flags = EXTENDED if extended else 0
            inject(vk, flags, flags | KEY_UP)
        log(f"{action}: {KEYS[usage][0]} (HID 0x{usage:02X}, VK 0x{KEYS[usage][1]:02X})")

    bridge = Bridge(emit)
    if args.replay:
        if args.usb_address is None:
            parser.error("--replay requires --usb-address")
        reports = RemoteReports(args.usb_address, args.connection_handle, trace_reports=args.trace_reports,
                                remote_address=address)
        with args.replay.open("rb") as stream:
            process_capture(stream, reports, bridge, args.seconds, args.stop_file)
        return
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise PermissionError("Run the bridge as Administrator to open USBPcap.")
    usb_address = args.usb_address or find_intel_adapter(args.usb_interface)
    reports = RemoteReports(usb_address, args.connection_handle, trace_reports=args.trace_reports,
                            remote_address=address)
    command = [str(USBPCAP), "-d", args.usb_interface, "--devices", str(usb_address), "-o", "-"]
    with single_instance(), subprocess.Popen(
        command, stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW
    ) as capture:
        try:
            log("USBPcap started.")
            if args.dry_run or not args.filter_native_right:
                process_capture(capture.stdout, reports, bridge, args.seconds, args.stop_file,
                                identify_existing=args.connection_handle is None)
            else:
                with NativeRightFilter() as right_filter:
                    process_capture(capture.stdout, reports, bridge, args.seconds, args.stop_file,
                                    right_filter, identify_existing=args.connection_handle is None)
            if capture.poll() is not None and capture.returncode != 0:
                raise OSError(f"USBPcap exited with code {capture.returncode}")
        finally:
            if capture.poll() is None:
                capture.terminate()
    log("Bridge stopped.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Bridge stopped.")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"MI RC: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
