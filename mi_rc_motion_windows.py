"""Start the MI RC motion bridge and forward samples to Windows localhost."""

import argparse
from contextlib import ExitStack
import ctypes
from importlib.util import find_spec
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

from virtual_microphone import VirtualMicrophone

HERE = Path(__file__).resolve().parent
USBIPD = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'usbipd-win' / 'usbipd.exe'
ADAPTER_PREFIX = 'USB\\VID_8087&PID_0026\\'
PYTHON_WSL = '/root/mi-rc-bumble-venv/bin/python'
AXES = ('ax', 'ay', 'az', 'gx', 'gy', 'gz')


def monitor_status(port, state, message):
    monitor_event(port, {'type': 'status', 'state': state, 'message': message})


def monitor_event(port, event):
    if port is not None:
        packet = json.dumps(event)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.sendto(packet.encode('utf-8'), ('127.0.0.1', port))


def run(command, timeout=35, **options):
    return subprocess.run([str(part) for part in command], check=True, timeout=timeout, **options)


def adapter():
    output = run([USBIPD, 'state'], encoding='utf-8-sig', capture_output=True).stdout
    adapters = [device for device in json.loads(output)['Devices']
                if device['InstanceId'].upper().startswith(ADAPTER_PREFIX) and device['BusId']]
    if len(adapters) != 1:
        raise RuntimeError('Expected exactly one Intel Bluetooth adapter (8087:0026).')
    return adapters[0]


def restore_adapter():
    device = adapter()
    bus_id = device['BusId']
    if device['ClientIPAddress']:
        try:
            run([USBIPD, 'detach', '--busid', bus_id])
        except (subprocess.SubprocessError, OSError) as error:
            print(f'Detach did not finish: {error}. Restoring the Windows driver.', file=sys.stderr)
    unbind_error = None
    if device['PersistedGuid']:
        try:
            run([USBIPD, 'unbind', '--busid', bus_id])
        except (subprocess.SubprocessError, OSError) as error:
            unbind_error = error
    device = adapter()
    if device['PersistedGuid'] or device['ClientIPAddress']:
        raise RuntimeError('The Intel adapter is still shared. Run the launcher with --restore.') from unbind_error
    print('Intel Bluetooth adapter returned to Windows.', file=sys.stderr, flush=True)


def wsl(distro):
    return ['wsl.exe', '-d', distro, '-u', 'root']


def setup(distro):
    command = '''set -eu
if ! /root/mi-rc-bumble-venv/bin/python -c 'import bumble.device' 2>/dev/null; then
    if ! python3 -m venv /root/mi-rc-bumble-venv; then
        apt-get install -y --no-install-recommends python3-venv
        python3 -m venv /root/mi-rc-bumble-venv
    fi
    /root/mi-rc-bumble-venv/bin/pip install bumble==0.0.236
fi
if ! command -v hciconfig >/dev/null; then
    apt-get install -y --no-install-recommends bluez
fi
'''
    run(wsl(distro) + ['--exec', 'sh', '-c', command], timeout=300, stdout=sys.stderr)


def read_bridge_output(stream, queue):
    try:
        for line in stream:
            queue.put(line.strip())
    finally:
        queue.put(None)


def voice_status(port, state, message=''):
    monitor_event(port, {'type': 'voice', 'state': state, 'message': message})
    if message:
        print(f'[microphone] {message}', file=sys.stderr, flush=True)


def remote_address(override):
    if override is None:
        config = HERE / 'remote-address.txt'
        try:
            override = config.read_text(encoding='utf-8-sig')
        except FileNotFoundError as error:
            raise RuntimeError('请在项目目录创建 remote-address.txt，填写遥控器蓝牙地址。') from error
    value = override.strip().upper()
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', value):
        raise RuntimeError('遥控器地址应为 XX:XX:XX:XX:XX:XX。')
    return value


def forward(queue, udp, args, capture, stop_file, microphone, deadline=float('inf')):
    next_display = 0
    sample_count = 0
    while time.monotonic() < deadline:
        try:
            line = queue.get(timeout=0.25)
        except Empty:
            continue
        if line is None:
            return
        try:
            event = json.loads(line)
        except ValueError:
            if line:
                print(line, file=sys.stderr, flush=True)
            continue
        if event['type'] in ('voice', 'voice_audio'):
            if microphone is not None:
                try:
                    feedback = microphone.event(event)
                except (RuntimeError, ValueError) as error:
                    voice_status(args.monitor_port, 'error', str(error))
                    microphone = None
                else:
                    if feedback is not None:
                        monitor_event(args.monitor_port, feedback)
            continue
        if capture:
            capture.write(line + '\n')
        if args.monitor_port is not None:
            udp.sendto(line.encode('utf-8'), ('127.0.0.1', args.monitor_port))
        if args.json:
            print(line, flush=True)
        if event['type'] == 'status':
            print(f"[{event['state']}] {event['message']}", file=sys.stderr, flush=True)
        elif event['type'] == 'motion':
            udp.sendto(line.encode('utf-8'), ('127.0.0.1', args.port))
            sample_count += 1
            if args.samples and sample_count == args.samples:
                stop_file.touch()
            now = time.monotonic()
            if not args.json and now >= next_display:
                next_display = now + 0.5
                axes = '  '.join(f'{axis}={event[axis]:6d}' for axis in AXES)
                print(axes, flush=True)


def bridge(args):
    address = remote_address(args.address)
    print(f'Samples → UDP 127.0.0.1:{args.port}. Press Ctrl+C to stop.', file=sys.stderr, flush=True)
    with ExitStack() as stack:
        capture = stack.enter_context(args.capture.open('x', encoding='utf-8', buffering=1)) if args.capture else None
        udp = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        stop_file = args.stop_file
        if stop_file is None:
            run_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='mi-rc-motion-')))
            stop_file = run_dir / 'stop'
        if args.native:
            if find_spec('bumble') is None:
                raise RuntimeError('Install the Windows dependencies from requirements-windows.txt.')
        else:
            stop_path = run(wsl(args.distro) + ['--exec', 'wslpath', '-u', str(stop_file)],
                            encoding='utf-8', errors='replace', capture_output=True).stdout.strip()
            setup(args.distro)
        if stop_file.exists():
            return
        microphone = None
        if args.no_microphone:
            voice_status(args.monitor_port, 'disabled')
        else:
            try:
                microphone = VirtualMicrophone()
            except RuntimeError as error:
                voice_status(args.monitor_port, 'error', str(error))
            else:
                stack.callback(microphone.close)
                voice_status(args.monitor_port, 'waiting')
        device = adapter()
        if device['ClientIPAddress'] or device['PersistedGuid']:
            raise RuntimeError('The Intel adapter is already shared. Use --restore if a previous run was interrupted.')
        bus_id = device['BusId']
        child = None
        keepalive = None
        try:
            run([USBIPD, 'bind', '--force', '--busid', bus_id], stdout=sys.stderr)
            if stop_file.exists():
                return
            if args.native:
                state_dir = Path(os.environ['LOCALAPPDATA']) / 'MiRcMotion'
                command = [sys.executable, '-X', 'utf8', '-u', str(HERE / 'mi_rc_motion_wsl.py'),
                           '--stop-file', str(stop_file), '--usbip-bus', bus_id, '--state-dir', str(state_dir)]
            else:
                keepalive = subprocess.Popen(wsl(args.distro) + ['--exec', 'sleep', '120'],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
                run([USBIPD, 'attach', '--wsl', args.distro, '--busid', bus_id], stdout=sys.stderr)
                prepare = '''set -eu
systemctl stop bluetooth
for attempt in $(seq 1 10); do
    if [ -e /sys/class/bluetooth/hci0 ]; then break; fi
    sleep 1
done
hciconfig hci0 down
exec "$@"
'''
                command = wsl(args.distro) + ['--cd', str(HERE), '--exec', 'sh', '-c', prepare, 'mi-rc',
                                           PYTHON_WSL, '-u', './mi_rc_motion_wsl.py', '--stop-file', stop_path]
            command.extend(['--seconds', str(args.seconds), '--address', address])
            if args.repair:
                command.append('--repair')
            if args.monitor_port is not None:
                command.append('--confirm')
            if microphone is not None:
                command.append('--voice')
            child = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, encoding='utf-8',
                                     errors='replace', creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            with child.stdout:
                queue = SimpleQueue()
                threading.Thread(target=read_bridge_output, args=(child.stdout, queue), daemon=True).start()
                try:
                    forward(queue, udp, args, capture, stop_file, microphone)
                except KeyboardInterrupt:
                    stop_file.touch()
                    forward(queue, udp, args, capture, stop_file, microphone, time.monotonic() + 35)
            if child.wait(timeout=10):
                raise RuntimeError(f'The Bumble process exited with code {child.returncode}.')
        finally:
            try:
                if child is not None and child.poll() is None:
                    stop_file.touch()
                    try:
                        child.wait(timeout=38)
                    except subprocess.TimeoutExpired:
                        child.terminate()
                        child.wait(timeout=8)
            finally:
                try:
                    restore_adapter()
                finally:
                    if keepalive is not None:
                        if keepalive.poll() is None:
                            keepalive.terminate()
                        keepalive.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', help='Run Bumble directly on Windows via local USB/IP.')
    parser.add_argument('--address', help='Remote BLE address. Defaults to the value in remote-address.txt.')
    parser.add_argument('--distro', default='Ubuntu-24.04')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--seconds', type=int, default=0, help='Stop after this many seconds; zero runs until stopped.')
    parser.add_argument('--samples', type=int, default=0, help='Stop after forwarding at least this many samples.')
    parser.add_argument('--json', action='store_true', help='Write every sample as JSON on stdout.')
    parser.add_argument('--capture', type=Path, help='Save Windows-side NDJSON to a new file.')
    parser.add_argument('--repair', action='store_true', help='Pair again instead of using a saved bond on the next connection.')
    parser.add_argument('--restore', action='store_true', help='Return the Intel adapter to Windows after an interrupted run.')
    parser.add_argument('--no-microphone', action='store_true', help='Disable the virtual microphone.')
    parser.add_argument('--monitor-port', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.seconds < 0 or args.samples < 0:
        parser.error('Port must be 1–65535, and limits must be nonnegative.')
    if args.monitor_port is not None and not 1 <= args.monitor_port <= 65535:
        parser.error('Invalid monitor port.')
    if not USBIPD.is_file():
        monitor_status(args.monitor_port, 'bridge_error', 'usbipd-win is not installed.')
        parser.error('usbipd-win is not installed.')
    if not ctypes.windll.shell32.IsUserAnAdmin():
        command = subprocess.list2cmdline([str(Path(__file__).resolve()), *sys.argv[1:]])
        execute = ctypes.windll.shell32.ShellExecuteW
        execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        execute.restype = ctypes.c_void_p
        result = execute(None, 'runas', sys.executable, command, str(HERE), 0 if args.monitor_port else 1)
        if not result or result <= 32:
            monitor_status(args.monitor_port, 'bridge_error', 'Administrator approval was cancelled or unavailable.')
            parser.error('The administrator prompt was cancelled or could not open.')
        return

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, 'Local\\MiRcMotionUsbAdapter')
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:
            raise RuntimeError('A motion bridge is already running in this Windows session.')
        if args.restore:
            restore_adapter()
        else:
            message = 'Preparing the Intel Bluetooth adapter in Windows.' if args.native else 'Preparing WSL and the Intel Bluetooth adapter.'
            monitor_status(args.monitor_port, 'bridge_starting', message)
            bridge(args)
            monitor_status(args.monitor_port, 'bridge_stopped', 'Bluetooth adapter returned to Windows.')
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        monitor_status(args.monitor_port, 'bridge_error', str(error))
        raise
    finally:
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Error: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
