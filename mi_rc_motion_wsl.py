"""Run the Xiaomi motion remote over Bumble and emit newline-delimited JSON."""

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct

from bumble.core import TimeoutError as BumbleTimeoutError, UUID
from bumble.device import Device
from bumble.hci import Address, OwnAddressType
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport import open_transport

from remote_keys import Key, decode_keys
from voice_reports import VoiceReports

PUBLIC = OwnAddressType.PUBLIC
STATE_DIR = Path('/root/.local/state/mi-rc-motion')
REPORT_MAP = Path(__file__).with_name('mi-rc-2717-32b0-report-map.bin')


def emit(event_type, **fields):
    print(json.dumps({'type': event_type, **fields}, separators=(',', ':')), flush=True)


def status(state, message):
    emit('status', state=state, message=message)


async def find_remote(host, stop, remote, timeout=None):
    found = asyncio.get_running_loop().create_future()

    def advertisement(report):
        if report.address.to_string(False).upper() == remote.to_string(False).upper() and not found.done():
            found.set_result(report.address)

    host.on('advertisement', advertisement)
    try:
        await host.start_scanning(own_address_type=PUBLIC, scanning_phys=(1,))
        if timeout is None:
            status('pairing_ready', 'For pairing, hold Home + Menu near the computer.')
        else:
            status('wake_scan', 'Scanning while retaining the bond. If needed, press a direction key.')
        done, _ = await asyncio.wait((found, stop), timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        return found.result() if found in done and stop not in done else None
    finally:
        host.remove_listener('advertisement', advertisement)
        with suppress(Exception):
            await asyncio.wait_for(host.stop_scanning(), 4)


async def discover_reports(connection, expected_map, with_confirm, with_voice):
    service, = await connection.gatt_client.discover_service(UUID.from_16_bits(0x1812))
    await service.discover_characteristics()
    report_map, = service.get_characteristics_by_uuid(UUID.from_16_bits(0x2A4B))
    if await report_map.read_value() != expected_map:
        raise ValueError('The remote HID Report Map did not match 2717:32B0.')

    required = {b'\x03\x01', b'\x04\x03'}
    if with_confirm:
        required.add(b'\x01\x01')
    voice_refs = tuple(bytes((number, 3)) for number in range(5, 9))
    wanted = required.union(voice_refs) if with_voice else required
    reports = {}
    for characteristic in service.get_characteristics_by_uuid(UUID.from_16_bits(0x2A4D)):
        await characteristic.discover_descriptors()
        descriptor = characteristic.get_descriptor(UUID.from_16_bits(0x2908))
        if descriptor:
            reference = await descriptor.read_value()
            if reference in wanted:
                reports[reference] = characteristic
                if reports.keys() == wanted:
                    break
    motion = reports[b'\x03\x01']
    feature = reports[b'\x04\x03']
    if not (motion.properties & 0x10 and feature.properties & 0x08):
        raise ValueError('The HID input and feature reports lack Notify or Write.')
    voice = None
    if with_voice:
        if all(ref in reports for ref in voice_refs):
            button, *audio = (reports[ref] for ref in voice_refs)
            voice = VoiceReports(connection.gatt_client, feature, button, audio, emit)
        else:
            emit('voice', state='error', message='The remote does not expose all voice reports.')
    return motion, feature, reports[b'\x01\x01'] if with_confirm else None, voice


async def set_sensors(feature, accel, gyro):
    value = bytes((1, 0, 0, 0x80, accel, gyro)) + bytes(14)
    await asyncio.wait_for(feature.write_value(value, with_response=True), 6)


async def stream(connection, stop, expected_map, with_confirm, with_voice):
    motion, feature, keys, voice = await asyncio.wait_for(
        discover_reports(connection, expected_map, with_confirm, with_voice), 15
    )
    loop = asyncio.get_running_loop()
    disconnected = loop.create_future()
    streaming = False
    pressed_keys = Key(0)
    last_sample = loop.time()

    def on_disconnect(_):
        if not disconnected.done():
            disconnected.set_result(None)

    def on_report(payload):
        nonlocal last_sample
        if not streaming or len(payload) != 12:
            return
        last_sample = loop.time()
        ax, ay, az, gx, gy, gz = struct.unpack('<6h', payload)
        buttons = {'confirm_down': bool(pressed_keys & Key.CONFIRM), 'buttons': int(pressed_keys)} if keys is not None else {}
        emit('motion', utc=datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
             ax=ax, ay=ay, az=az, gx=gx, gy=gy, gz=gz, **buttons)

    def on_keys(payload):
        nonlocal pressed_keys
        pressed = decode_keys(payload)
        if pressed is not None and pressed != pressed_keys:
            pressed_keys = pressed
            emit('buttons', buttons=int(pressed_keys))

    connection.on(connection.EVENT_DISCONNECTION, on_disconnect)
    subscribed = False
    keys_subscribed = False
    attempted_start = False
    try:
        await asyncio.wait_for(motion.subscribe(on_report), 6)
        subscribed = True
        if keys is not None:
            await asyncio.wait_for(keys.subscribe(on_keys), 6)
            keys_subscribed = True
        attempted_start = True
        await set_sensors(feature, 1, 2)
        status('accelerometer_on', 'Accelerometer enabled. Waiting before starting the gyroscope.')
        done, _ = await asyncio.wait((stop, disconnected), timeout=4,
                                     return_when=asyncio.FIRST_COMPLETED)
        if done:
            return
        await set_sensors(feature, 0, 1)
        streaming = True
        last_sample = loop.time()
        status('streaming', f'Both sensors enabled; listening on 0x{motion.handle:04x}.')
        if voice:
            voice.listen()
        while not stop.done() and not disconnected.done():
            await asyncio.wait((stop, disconnected), timeout=1, return_when=asyncio.FIRST_COMPLETED)
            if voice and voice.active:
                last_sample = loop.time()
            if loop.time() - last_sample > 12:
                raise TimeoutError('No motion reports for 12 seconds.')
    finally:
        streaming = False
        if voice and voice.task is not None:
            await voice.close(not disconnected.done())
        if pressed_keys:
            emit('buttons', buttons=0)
        connection.remove_listener(connection.EVENT_DISCONNECTION, on_disconnect)
        if attempted_start and not disconnected.done():
            try:
                await set_sensors(feature, 2, 2)
                status('sampling_stopped', 'The remote acknowledged the stop command.')
            except Exception as error:
                status('stop_error', f'The stop command was not confirmed: {error}')
        if subscribed and not disconnected.done():
            with suppress(Exception):
                await asyncio.wait_for(motion.unsubscribe(on_report), 4)
        if keys_subscribed and not disconnected.done():
            with suppress(Exception):
                await asyncio.wait_for(keys.unsubscribe(on_keys), 4)


async def watch_stop(path, seconds):
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds if seconds else float('inf')
    while loop.time() < end and not path.exists():
        await asyncio.sleep(0.2)


async def run(args):
    expected_map = REPORT_MAP.read_bytes()
    os.umask(0o077)
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = asyncio.create_task(watch_stop(args.stop_file, args.seconds))
    repair = args.repair
    try:
        if args.usbip_bus:
            from usbip_hci import open_usbip_hci

            transport = await open_usbip_hci(args.usbip_bus)
            transport.source.terminated.add_done_callback(lambda _: stop.cancel())
        else:
            transport = await open_transport('hci-socket:0')
        async with transport:
            host = Device.with_hci('MI RC motion bridge', Address.generate_static_address(),
                                   transport.source, transport.sink)
            host.config.keystore = f'JsonKeyStore:{args.state_dir / "bonds.json"}'
            host.pairing_config_factory = lambda _: PairingConfig(
                sc=False, mitm=False, bonding=True,
                delegate=PairingDelegate(PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT),
                identity_address_type=PairingConfig.AddressType.PUBLIC,
            )
            await asyncio.wait_for(host.power_on(), 12)
            remote = Address(args.address, Address.PUBLIC_DEVICE_ADDRESS)
            while not stop.done():
                connection = None
                try:
                    keys = await host.keystore.get(str(remote))
                    bonded = bool(keys and (keys.ltk or keys.ltk_central)) and not repair
                    if bonded:
                        status('reconnecting', 'Using the saved BLE bond. Press a direction key if the remote is asleep.')
                        try:
                            connection = await host.connect(remote, own_address_type=PUBLIC, timeout=8)
                        except (TimeoutError, BumbleTimeoutError):
                            target = await find_remote(host, stop, remote, timeout=12)
                            if target is None:
                                continue
                    else:
                        target = await find_remote(host, stop, remote)
                        if target is None:
                            break
                    if connection is None:
                        connection = await host.connect(target, own_address_type=PUBLIC, timeout=8)
                    if bonded:
                        await asyncio.wait_for(connection.encrypt(), 8)
                        status('bond_reused', 'BLE encryption established with the saved bond.')
                    else:
                        await asyncio.wait_for(connection.pair(), 15)
                        saved = await host.keystore.get(str(connection.peer_address))
                        if not saved or not (saved.ltk or saved.ltk_central):
                            raise RuntimeError('Pairing finished without a reusable BLE key.')
                        repair = False
                        status('paired', 'Paired and saved the BLE bond for this backend.')
                    if not stop.done():
                        await stream(connection, stop, expected_map, args.confirm, args.voice)
                except Exception as error:
                    status('retry', str(error) or f'{type(error).__name__} while connecting or starting sampling.')
                finally:
                    if connection is not None and connection.handle in host.connections:
                        with suppress(Exception):
                            await asyncio.wait_for(connection.disconnect(), 4)
                if not stop.done():
                    await asyncio.wait((stop,), timeout=1.5)
            if args.usbip_bus and transport.error:
                raise OSError(f'Windows USB/IP Bluetooth transport ended: {transport.error}')
    finally:
        stop.cancel()
        with suppress(asyncio.CancelledError):
            await stop


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop-file', type=Path, required=True)
    parser.add_argument('--address', required=True, help='Remote BLE public address.')
    parser.add_argument('--seconds', type=int, default=0)
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--confirm', action='store_true')
    parser.add_argument('--voice', action='store_true')
    parser.add_argument('--usbip-bus')
    parser.add_argument('--state-dir', type=Path, default=STATE_DIR)
    asyncio.run(run(parser.parse_args()))
