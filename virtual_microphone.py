"""Decode Xiaomi voice and feed the Windows VB-CABLE recording endpoint."""

from array import array
from threading import Lock
import time

RATE = 16000
STEPS = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37,
    41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173,
    190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
    724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
    6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
    18500, 20350, 22385, 24623, 27086, 29794, 32767,
)
INDEX_DELTAS = (-1, -1, -1, -1, 2, 4, 6, 8)
BUFFER_LIMIT = RATE * 2 // 4
PREBUFFER = RATE * 2 * 40 // 1000


def pcm16(sample):
    return max(-32768, min(32767, sample))


class DialogAdpcm:
    def __init__(self):
        self.sample = self.index = 0

    def decode(self, packets):
        sample, index = self.sample, self.index
        samples = array('h')
        for byte in packets:
            for code in (byte >> 4, byte & 15):
                magnitude = code & 7
                delta = (STEPS[index] * (1 + 2 * magnitude)) >> 3
                sample = pcm16(sample - delta if code & 8 else sample + delta)
                index = max(0, min(88, index + INDEX_DELTAS[magnitude]))
                samples.append(sample)
        self.sample, self.index = sample, index
        return samples


class VirtualMicrophone:
    def __init__(self):
        try:
            import sounddevice as sd
        except ImportError as error:
            raise RuntimeError('请安装 Python 依赖 sounddevice。') from error
        except OSError as error:
            raise RuntimeError(f'无法加载 Windows 音频依赖（{error}）') from error

        self.lock = Lock()
        self.buffer = bytearray()
        self.decoder = DialogAdpcm()
        self.speaking = self.playing = False
        self.next_level = 0
        self.peak = 0
        try:
            devices = sd.query_devices()
            host_apis = sd.query_hostapis()
            cable = next((index for index, device in enumerate(devices)
                          if host_apis[device['hostapi']]['name'] == 'Windows WASAPI'
                          and device['max_output_channels']
                          and device['name'].startswith('CABLE Input (')), None)
            if cable is None:
                raise RuntimeError('未找到 VB-CABLE。安装后请重新启动桥接。')
            self.stream = sd.RawOutputStream(
                device=cable, samplerate=RATE, channels=1, dtype='int16',
                latency=0.04, extra_settings=sd.WasapiSettings(auto_convert=True),
                callback=self.render,
            )
            try:
                self.stream.start()
            except sd.PortAudioError:
                self.stream.close()
                raise
        except sd.PortAudioError as error:
            raise RuntimeError(f'无法打开 VB-CABLE（{error}）') from error

    def render(self, output, frames, time_info, status):
        length = frames * 2
        with self.lock:
            if not self.speaking or len(self.buffer) >= PREBUFFER:
                self.playing = True
            data = self.buffer[:length] if self.playing else b''
            del self.buffer[:len(data)]
            if len(data) < length:
                self.playing = False
        output[:] = data + bytes(length - len(data))

    def event(self, event):
        if not self.stream.active:
            raise RuntimeError('VB-CABLE 音频设备已断开。')
        if event['type'] == 'voice':
            state = event['state']
            with self.lock:
                self.speaking = state == 'speaking'
                if state in ('waiting', 'error'):
                    self.buffer.clear()
                    self.playing = False
            if state == 'speaking':
                self.decoder = DialogAdpcm()
                self.next_level = self.peak = 0
            return event
        if not self.speaking:
            return None
        samples = self.decoder.decode(bytes.fromhex(event['hex']))
        # Compensate for the low speech level on the remote's PCM stream.
        pcm = array('h', (pcm16(sample * 4) for sample in samples))
        data = pcm.tobytes()
        with self.lock:
            self.buffer.extend(data)
            excess = len(self.buffer) - BUFFER_LIMIT
            if excess > 0:
                del self.buffer[:excess]
        self.peak = max(self.peak, max(map(abs, pcm), default=0))
        now = time.monotonic()
        if now >= self.next_level:
            level = self.peak / 32768
            self.peak = 0
            self.next_level = now + 0.07
            return {'type': 'voice_level', 'level': level}
        return None

    def close(self):
        try:
            self.stream.abort()
        finally:
            self.stream.close()
