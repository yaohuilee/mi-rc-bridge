"""Push-to-talk transport for the Xiaomi 2717:32B0 voice reports."""

import asyncio
from contextlib import suppress


def voice_command(pressed):
    return bytes((0, 0, 0, 0x80, int(pressed))) + bytes(15)


class VoiceReports:
    def __init__(self, client, control, button, audio, emit):
        self.client = client
        self.control = control
        self.button = button
        self.audio = audio
        self.emit = emit
        self.keys = asyncio.Queue()
        self.packets = bytearray()
        self.pressed = False
        self.active = False
        self.needs_stop = False
        self.failed = False
        self.task = None

    def listen(self):
        # The 32B0 sends these notifications without a CCCD or Notify property.
        for report, callback in self.subscriptions():
            self.client.notification_subscribers.setdefault(report.handle, set()).add(callback)
        self.task = asyncio.create_task(self.run())
        self.emit('voice', state='ready')

    def subscriptions(self):
        yield self.button, self.on_key
        for report in self.audio:
            yield report, self.on_audio

    def on_key(self, data):
        # Only type 0 reports carry the push-to-talk key. Type 3 diagnostics
        # also begin with zero, including immediately before the first audio packet.
        if self.failed or len(data) != 20 or data[0] not in (0, 1) or data[1] != 0:
            return
        pressed = bool(data[0])
        if pressed != self.pressed:
            self.pressed = pressed
            self.keys.put_nowait(pressed)

    def on_audio(self, data):
        if self.active and len(data) == 20:
            self.packets.extend(data)
            if len(self.packets) >= 160:
                self.flush()

    def flush(self):
        if self.packets:
            self.emit('voice_audio', hex=self.packets.hex())
            self.packets.clear()

    async def write(self, pressed):
        await asyncio.wait_for(self.control.write_value(voice_command(pressed), with_response=True), 4)

    async def run(self):
        try:
            while True:
                pressed = await self.keys.get()
                if pressed:
                    self.needs_stop = self.active = True
                    self.emit('voice', state='speaking')
                    await self.write(True)
                else:
                    self.active = False
                    self.flush()
                    await self.write(False)
                    self.needs_stop = False
                    self.emit('voice', state='ready')
        except Exception as error:
            self.failed = True
            self.active = False
            self.packets.clear()
            self.emit('voice', state='error', message=f'语音指令未完成（{error or type(error).__name__}）')
            if self.needs_stop:
                with suppress(Exception):
                    await self.write(False)
                    self.needs_stop = False

    async def close(self, connected):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        self.active = False
        self.packets.clear()
        if connected and self.needs_stop:
            with suppress(Exception):
                await self.write(False)
        for report, callback in self.subscriptions():
            self.client.notification_subscribers[report.handle].discard(callback)
        self.emit('voice', state='waiting')
