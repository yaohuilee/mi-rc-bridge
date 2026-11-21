import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

from air_mouse import AirMouse
from mi_rc_motion_windows import remote_address
from remote_keys import Key, decode_keys
from virtual_microphone import DialogAdpcm
from voice_reports import VoiceReports, voice_command


class CoreTests(unittest.TestCase):
    def test_address_validation(self):
        self.assertEqual(remote_address(' aa:bb:cc:dd:ee:ff '), 'AA:BB:CC:DD:EE:FF')
        with self.assertRaises(RuntimeError):
            remote_address('not an address')

    def test_reports(self):
        descriptor = Path(__file__).resolve().parents[1] / 'mi-rc-2717-32b0-report-map.bin'
        self.assertEqual(len(descriptor.read_bytes()), 109)
        self.assertEqual(decode_keys(bytes.fromhex('0000280065005200')), Key.CONFIRM | Key.BACK | Key.UP)
        self.assertEqual(decode_keys(bytes.fromhex('0000510000000000')), Key.DOWN)
        self.assertIsNone(decode_keys(bytes(7)))

    def test_adpcm_retains_state(self):
        decoder = DialogAdpcm()
        first = decoder.decode(bytes.fromhex('70'))
        second = decoder.decode(bytes.fromhex('81'))
        self.assertEqual(list(first) + list(second), [13, 15, 14, 18])
        self.assertEqual(len(decoder.decode(bytes(20))), 40)

    def test_pause_retains_air_mouse_calibration(self):
        events = []
        mouse = SimpleNamespace(
            move=lambda x, y: events.append(('move', x, y)),
            button=lambda key, pressed: events.append(('button', key, pressed)),
        )
        air = AirMouse(mouse)
        air.set_enabled(True)
        for _ in range(45):
            air.update((-8, -4, -3), 1)
        self.assertTrue(air.ready)
        air.set_keys(Key.CONFIRM)
        air.pause()
        self.assertTrue(air.ready)
        self.assertEqual(events[-1], ('button', Key.CONFIRM, False))
        air.update((-8, -4, -3), 1)
        self.assertEqual(len(events), 2)
        air.update((1250, 0, 0), 1)
        self.assertEqual(events[-1][0], 'move')


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_packet_does_not_end_voice(self):
        writes = asyncio.Queue()

        async def write_value(value, *, with_response):
            self.assertTrue(with_response)
            writes.put_nowait(value)

        control = SimpleNamespace(handle=59, write_value=write_value)
        button = SimpleNamespace(handle=62)
        audio = [SimpleNamespace(handle=handle) for handle in (65, 68, 71)]
        client = SimpleNamespace(notification_subscribers={})
        emitted = []
        voice = VoiceReports(client, control, button, audio, lambda kind, **fields: emitted.append((kind, fields)))
        voice.listen()
        try:
            voice.on_key(b'\x01' + bytes(19))
            self.assertEqual(await asyncio.wait_for(writes.get(), 1), voice_command(True))
            voice.on_key(bytes.fromhex('0003000003010006067800000000000000000000'))
            await asyncio.sleep(0)
            self.assertTrue(voice.active)
            self.assertTrue(writes.empty())
            voice.on_audio(bytes([0x17]) * 20)
            voice.on_key(bytes(20))
            self.assertEqual(await asyncio.wait_for(writes.get(), 1), voice_command(False))
            self.assertIn(('voice_audio', {'hex': '17' * 20}), emitted)
        finally:
            await voice.close(True)
