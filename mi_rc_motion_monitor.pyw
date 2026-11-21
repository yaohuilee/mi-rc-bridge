"""Windows monitor for the MI RC motion bridge."""

from collections import deque
import ctypes
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import tkinter as tk

from air_mouse import AirMouse, WindowsMouse
from remote_keys import Key

HERE = Path(__file__).resolve().parent
AXES = ('ax', 'ay', 'az', 'gx', 'gy', 'gz')
WINDOW_SECONDS = 8
BG, PANEL, BORDER = '#101622', '#171f2e', '#28354a'
TEXT, MUTED = '#edf3ff', '#9baec8'
ACCEL, GYRO = '#52dccc', '#ffbe70'
FONT = 'Microsoft YaHei UI'

STATUS = {
    'bridge_starting': ('正在启动', '正在准备 WSL 和 Intel 蓝牙适配器。'),
    'pairing_ready': ('等待首次配对', '请把遥控器靠近电脑，同时按住「小房子＋菜单」。'),
    'paired': ('已配对', '已保存配对，正在准备六轴采样。'),
    'reconnecting': ('正在重连', '正在使用保存的配对。遥控器休眠时，按一下方向键唤醒。'),
    'wake_scan': ('等待遥控器', '正在扫描遥控器。请按一下普通方向键唤醒。'),
    'bond_reused': ('已连接', '已使用保存的配对建立加密连接。'),
    'accelerometer_on': ('准备采样', '加速度计已启用，稍后开启陀螺仪。'),
    'streaming': ('等待数据', '两组传感器已开启，正在等待读数。'),
    'retry': ('正在重试', '桥接正在重新建立连接。'),
    'sampling_stopped': ('正在停止', '已停止采样，正在交还蓝牙适配器。'),
    'bridge_stopped': ('已停止', 'Windows 已收回蓝牙适配器。'),
}

VOICE_STATUS = {
    'waiting': '等待遥控器连接',
    'ready': '按住语音键说话 · 应用中选择 CABLE Output',
    'speaking': '正在传输 · 松开语音键结束',
    'disabled': '已关闭',
}


class Monitor:
    def __init__(self, window, native=False):
        self.window = window
        self.native = native
        self.receiver = None
        self.process = None
        self.session = None
        self.stop_file = None
        self.running = self.stopping = self.closing = self.started = False
        self.stop_deadline = 0
        self.state = 'idle'
        self.history = deque(maxlen=1500)
        self.total = 0
        self.mouse = AirMouse(WindowsMouse())
        self.air_enabled = tk.BooleanVar(window, False)
        self.sensitivity = tk.DoubleVar(window, 1.0)
        self.last_motion = None
        self.mouse_error = ''
        self.voice_speaking = False
        window.title('小米遥控器 · Windows 本机桥接' if native else '小米遥控器 · 六轴和麦克风')
        window.geometry('1080x790')
        window.minsize(810, 680)
        window.configure(bg=BG)
        window.protocol('WM_DELETE_WINDOW', self.close)

        header = tk.Frame(window, bg=BG)
        header.pack(fill='x', padx=26, pady=(20, 13))
        tk.Label(header, text='小米遥控器监视', font=(FONT, 23, 'bold'), bg=BG, fg=TEXT).pack(side='left')
        self.stop_button = self.button(header, '停止', self.stop, PANEL, TEXT)
        self.stop_button.pack(side='right')
        self.start_button = self.button(header, '启动', self.start, ACCEL, BG)
        self.start_button.pack(side='right', padx=(0, 8))

        status_panel = tk.Frame(window, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        status_panel.pack(fill='x', padx=26, pady=(0, 10))
        self.connection = tk.Label(status_panel, font=(FONT, 13, 'bold'), bg=PANEL, fg=ACCEL)
        self.connection.pack(side='left', padx=15, pady=13)
        self.detail = tk.Label(status_panel, font=(FONT, 10), bg=PANEL, fg=MUTED, justify='left', anchor='w')
        self.detail.pack(side='left', fill='x', expand=True, padx=(0, 10))
        self.rate = tk.Label(status_panel, font=('Consolas', 11), bg=PANEL, fg=TEXT)
        self.rate.pack(side='right', padx=15)

        controls = tk.Frame(window, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        controls.pack(fill='x', padx=26, pady=(0, 10))
        tk.Checkbutton(controls, text='空鼠', variable=self.air_enabled, command=self.toggle_mouse,
                       bg=PANEL, fg=TEXT, activebackground=PANEL, activeforeground=ACCEL,
                       selectcolor=BG, highlightthickness=0, font=(FONT, 11, 'bold')).pack(side='left', padx=14, pady=6)
        tk.Label(controls, text='灵敏度', bg=PANEL, fg=MUTED, font=(FONT, 10)).pack(side='left', padx=(6, 5))
        tk.Scale(controls, variable=self.sensitivity, from_=0.3, to=3, resolution=0.1, orient='horizontal',
                 length=148, showvalue=False, bg=PANEL, fg=TEXT, troughcolor=BORDER, activebackground=ACCEL,
                 highlightthickness=0, bd=0).pack(side='left')
        self.gain = tk.Label(controls, width=6, bg=PANEL, fg=TEXT, font=('Consolas', 10))
        self.gain.pack(side='left')
        self.calibrate_button = self.button(controls, '校准', self.mouse.calibrate, BORDER, TEXT)
        self.calibrate_button.pack(side='right', padx=8, pady=4)
        self.mouse_status = tk.Label(controls, bg=PANEL, fg=MUTED, font=(FONT, 9), anchor='w')
        self.mouse_status.pack(side='left', fill='x', expand=True, padx=10)

        voice_panel = tk.Frame(window, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        voice_panel.pack(fill='x', padx=26, pady=(0, 16))
        tk.Label(voice_panel, text='麦克风', bg=PANEL, fg=TEXT, font=(FONT, 11, 'bold')).pack(
            side='left', padx=14, pady=8)
        self.voice_meter = tk.Canvas(voice_panel, width=110, height=7, bg=BORDER, highlightthickness=0)
        self.voice_meter.pack(side='right', padx=14)
        self.voice_fill = self.voice_meter.create_rectangle(0, 0, 0, 7, fill=ACCEL, width=0)
        self.voice_label = tk.Label(voice_panel, text='待启动', bg=PANEL, fg=MUTED, font=(FONT, 10),
                                    anchor='w')
        self.voice_label.pack(side='left', fill='x', expand=True)

        plots = tk.Frame(window, bg=BG)
        plots.pack(fill='both', expand=True, padx=26)
        for column, (title, color) in enumerate((('加速度计', ACCEL), ('陀螺仪', GYRO))):
            plots.columnconfigure(column, weight=1, uniform='sensor')
            tk.Label(plots, text=title, font=(FONT, 13, 'bold'), fg=color, bg=BG, anchor='w').grid(
                row=0, column=column, sticky='ew', padx=(0, 10) if column == 0 else (10, 0), pady=(0, 7))
        self.canvases = []
        for index in range(len(AXES)):
            column, axis = divmod(index, 3)
            row = axis + 1
            plots.rowconfigure(row, weight=1, uniform='axis')
            canvas = tk.Canvas(plots, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, height=94)
            canvas.grid(row=row, column=column, sticky='nsew', padx=(0, 10) if column == 0 else (10, 0), pady=(0, 8))
            self.canvases.append(canvas)

        tk.Label(window, text='最近 8 秒  ·  原始计数，纵轴自动缩放', font=(FONT, 9), bg=BG, fg=MUTED,
                 anchor='w').pack(fill='x', padx=26, pady=(1, 13))
        self.show_status('待启动', '正在打开六轴桥接。')
        self.stop_button.configure(state='disabled')
        window.after(8, self.poll_input)
        window.after(40, self.tick)
        window.after_idle(self.start)

    @staticmethod
    def button(parent, title, action, background, foreground):
        return tk.Button(parent, text=title, command=action, background=background, foreground=foreground,
                         activebackground='#7de4d8' if background == ACCEL else BORDER,
                         activeforeground=foreground, disabledforeground=MUTED, relief='flat',
                         highlightthickness=0, font=(FONT, 10, 'bold'), padx=18, pady=7, cursor='hand2')

    def show_status(self, name, detail, color=ACCEL):
        self.connection.configure(text=f'●  {name}', fg=color)
        self.detail.configure(text=detail)

    def show_voice(self, state, message=''):
        self.voice_speaking = state == 'speaking'
        color = {'speaking': ACCEL, 'error': GYRO}.get(state, MUTED)
        self.voice_label.configure(text=VOICE_STATUS.get(state, message), fg=color)
        self.show_voice_level(0)

    def show_voice_level(self, level):
        width = round(110 * min(1.0, max(0.0, level) ** 0.5))
        self.voice_meter.coords(self.voice_fill, 0, 0, width, 7)

    def toggle_mouse(self):
        self.mouse_error = ''
        self.mouse.set_enabled(self.air_enabled.get())

    def disable_mouse(self):
        self.air_enabled.set(False)
        self.mouse.set_enabled(False)

    def poll_input(self):
        try:
            if self.mouse.enabled and ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000:
                self.disable_mouse()
            self.receive()
            if self.last_motion is not None and time.monotonic() - self.last_motion > 0.5:
                self.last_motion = None
                if self.voice_speaking:
                    self.mouse.pause()
                    if self.state == 'live':
                        self.state = 'stalled'
                        self.show_status('语音传输中', '遥控器暂未发送六轴数据。', GYRO)
                else:
                    self.mouse.calibrate()
                self.mouse.source_keys = Key(0)
        except OSError as error:
            self.disable_mouse()
            self.mouse_error = str(error)
        self.window.after(8, self.poll_input)

    def start(self):
        if self.running:
            return
        if self.receiver is not None:
            self.receiver.close()
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receiver.bind(('127.0.0.1', 0))
        self.receiver.setblocking(False)
        self.session = Path(tempfile.mkdtemp(prefix='mi-rc-monitor-'))
        self.stop_file = self.session / 'stop'
        self.history.clear()
        self.total = 0
        self.running = True
        self.started = self.stopping = False
        self.state = 'bridge_starting'
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.show_status('正在启动', '若出现管理员提示，请确认启动。')
        self.show_voice('initial', '正在检查虚拟麦克风')
        python = Path(sys.executable).with_name('python.exe')
        try:
            command = [str(python), str(HERE / 'mi_rc_motion_windows.py'),
                       '--monitor-port', str(self.receiver.getsockname()[1]), '--stop-file', str(self.stop_file)]
            if self.native:
                command.append('--native')
            self.process = subprocess.Popen(
                command,
                cwd=HERE, creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            self.finish('启动失败', str(error), '#ff8787')

    def stop(self):
        if self.running and not self.stopping:
            self.disable_mouse()
            self.stop_file.touch()
            self.stopping = True
            self.stop_deadline = time.monotonic() + 95
            self.stop_button.configure(state='disabled')
            self.show_status('正在停止', '等待程序停止采样并交还蓝牙适配器。')

    def close(self):
        if self.running:
            self.closing = True
            self.stop()
        else:
            self.disable_mouse()
            if self.receiver is not None:
                self.receiver.close()
            self.window.destroy()

    def finish(self, name, detail, color=MUTED, confirmed=True):
        self.disable_mouse()
        self.running = self.stopping = False
        self.state = 'idle'
        self.show_status(name, detail, color)
        self.show_voice('idle', '桥接已停止')
        self.start_button.configure(state='normal')
        self.stop_button.configure(state='disabled')
        if confirmed and self.session is not None:
            self.stop_file.unlink(missing_ok=True)
            self.session.rmdir()
        self.session = None
        if self.closing:
            self.window.after_idle(self.close)

    def receive(self):
        if not self.running or self.receiver is None:
            return
        for _ in range(400):
            try:
                packet = self.receiver.recv(4096)
            except BlockingIOError:
                break
            try:
                event = json.loads(packet)
                if event['type'] == 'voice':
                    self.show_voice(event['state'], event.get('message', ''))
                elif event['type'] == 'voice_level':
                    self.show_voice_level(float(event['level']))
                elif event['type'] == 'motion':
                    values = tuple(int(event[axis]) for axis in AXES)
                    stamp = time.monotonic()
                    if self.history and stamp - self.history[-1][0] > 0.8:
                        self.history.clear()
                    self.history.append((stamp, values))
                    self.last_motion = stamp
                    if not self.stopping:
                        self.mouse.update(values[3:], self.sensitivity.get())
                        self.mouse.set_keys(Key(event.get('buttons', 0)))
                        self.mouse.repeat_scroll(stamp)
                    self.total += 1
                    self.started = True
                    if not self.stopping and self.state != 'live':
                        self.state = 'live'
                        self.show_status('已连接 · 实时采样', '六轴数据正在接收。')
                elif event['type'] == 'buttons' and not self.stopping:
                    self.mouse.set_keys(Key(event['buttons']))
                elif event['type'] == 'status':
                    self.started = True
                    state = event['state']
                    if state == 'bridge_stopped':
                        self.finish(*STATUS[state])
                        break
                    if state == 'bridge_error':
                        self.finish('桥接异常', event.get('message', ''), '#ff8787')
                        break
                    if not self.stopping and state in STATUS:
                        if state in ('reconnecting', 'retry', 'wake_scan', 'sampling_stopped'):
                            self.last_motion = None
                            self.mouse.calibrate()
                            self.mouse.source_keys = Key(0)
                        self.state = state
                        name, detail = STATUS[state]
                        if state == 'bridge_starting' and self.native:
                            detail = '正在通过本机 USB/IP 连接 Intel 蓝牙适配器。'
                        self.show_status(name, detail, GYRO if state == 'retry' else ACCEL)
            except (TypeError, KeyError, ValueError):
                continue

    def tick(self):
        now = time.monotonic()
        if self.running and not self.started and self.process is not None and self.process.poll() not in (None, 0):
            self.finish('启动失败', '启动器已退出，请确认管理员提示或查看命令行启动日志。', '#ff8787')
        if self.stopping and now >= self.stop_deadline:
            self.finish('未确认停止', '请用 Start-MiRcMotion.cmd --restore 检查蓝牙适配器。', '#ff8787', confirmed=False)
        rate = sum(stamp >= now - 1 for stamp, _ in self.history) if self.running else 0
        if self.state == 'live' and not self.stopping and self.history and now - self.history[-1][0] > 2:
            self.state = 'stalled'
            self.show_status('等待新数据', '暂未收到新报告，桥接会继续检查连接。', GYRO)
        self.rate.configure(text=f'{rate:>3} Hz  /  {self.total:,} 帧')
        self.gain.configure(text=f'{self.sensitivity.get():.1f}×')
        self.calibrate_button.configure(state='normal' if self.mouse.enabled else 'disabled')
        if self.mouse_error:
            message = self.mouse_error
        elif not self.mouse.enabled:
            message = '确认键左键 · 返回键右键 · ↑↓ 滚轮'
        elif self.last_motion is None and self.voice_speaking:
            message = '六轴暂无数据 · 恢复后继续控制指针'
        elif self.last_motion is None:
            message = '等待遥控器数据 · Esc 关闭'
        elif not self.mouse.ready:
            message = '请将遥控器按键朝上、静放约 1 秒'
        else:
            message = '空鼠已开启 · ↑↓ 滚轮 · Esc 关闭'
        self.mouse_status.configure(text=message)
        if self.history:
            anchor = min(now, self.history[-1][0] + 0.25) if self.running else self.history[-1][0]
            points = [(stamp, axes) for stamp, axes in self.history if stamp >= anchor - WINDOW_SECONDS]
        else:
            anchor, points = now, []
        for index, canvas in enumerate(self.canvases):
            self.draw_axis(canvas, index, points, anchor)
        self.window.after(65, self.tick)

    @staticmethod
    def draw_axis(canvas, index, points, anchor):
        canvas.delete('all')
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 100 or height < 60:
            return
        color = ACCEL if index < 3 else GYRO
        latest = f'{points[-1][1][index]:,}' if points else '—'
        canvas.create_text(13, 13, anchor='nw', text=f'{"XYZ"[index % 3]} 轴', fill=MUTED, font=(FONT, 10))
        canvas.create_text(width - 13, 10, anchor='ne', text=latest, fill=color, font=('Consolas', 19, 'bold'))
        left, right, top, bottom = 52, width - 15, 45, height - 23
        low = high = 0
        if points:
            values = [axes[index] for _, axes in points]
            span = max(16, max(values) - min(values))
            middle = (max(values) + min(values)) / 2
            low, high = middle - span * 0.57, middle + span * 0.57
        for y in (top, (top + bottom) / 2, bottom):
            canvas.create_line(left, y, right, y, fill=BORDER)
        for tick in range(5):
            x = left + (right - left) * tick / 4
            canvas.create_line(x, top, x, bottom, fill=BORDER)
        if points:
            canvas.create_text(left - 5, top, anchor='e', text=f'{high:,.0f}', fill=MUTED, font=('Consolas', 8))
            canvas.create_text(left - 5, bottom, anchor='e', text=f'{low:,.0f}', fill=MUTED, font=('Consolas', 8))
            line = []
            for (stamp, _), value in zip(points, values):
                x = right - (anchor - stamp) * (right - left) / WINDOW_SECONDS
                y = bottom - (value - low) / (high - low) * (bottom - top)
                line.extend((x, y))
            if len(line) >= 4:
                canvas.create_line(*line, fill=color, width=2)
            if line:
                x, y = line[-2:]
                canvas.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=color, outline='')
        canvas.create_text(left, height - 13, anchor='w', text='−8 s', fill=MUTED, font=('Consolas', 8))
        canvas.create_text(right, height - 13, anchor='e', text='0 s', fill=MUTED, font=('Consolas', 8))


if __name__ == '__main__':
    app = tk.Tk()
    Monitor(app, native='--native' in sys.argv[1:])
    app.mainloop()
