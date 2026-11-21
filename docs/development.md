# 开发和复现

## 代码入口

| 文件 | 职责 |
|---|---|
| [mi_rc_motion_windows.py](../mi_rc_motion_windows.py) | 提权、适配器生命周期、后端进程、Windows UDP 和音频路由 |
| [mi_rc_motion_wsl.py](../mi_rc_motion_wsl.py) | 两种后端共用的 BLE 建连、配对、HID GATT 发现和六轴解析 |
| [usbip_hci.py](../usbip_hci.py) | Windows 本机 USB/IP 客户端和 HCI 收发 |
| [voice_reports.py](../voice_reports.py) | 语音键状态、控制写入、通知接收和音频分批 |
| [virtual_microphone.py](../virtual_microphone.py) | Dialog ADPCM、PCM 缓冲和 WASAPI |
| [mi_rc_motion_monitor.pyw](../mi_rc_motion_monitor.pyw)、[air_mouse.py](../air_mouse.py) | Tkinter 窗口和 Win32 鼠标输入 |
| [tools/usbpcap_keys.py](../tools/usbpcap_keys.py) | Windows 蓝牙栈下的早期被动按键工具 |

## 离线测试

```powershell
python -m pip install -r requirements-windows.txt
python -m unittest discover -s tests -v
```

测试使用十六进制固定样本和本机模拟 USB/IP 服务。它们覆盖键值、ADPCM 有状态解码、语音诊断报告不会被当成松键、空鼠暂停保留校准，以及同一 USB/IP 连接上的控制、批量和分片中断响应。模拟测试不需要管理员权限或真实蓝牙设备。

## 真机复测

1. 记录遥控器 PID、控制器 VID/PID、Windows 版本、usbipd-win 版本和使用的后端。从未配对状态测试首次连接，再停止并重开，验证复用配对。
2. 用 `--capture` 记录六轴和状态。让遥控器静置、左右转动、上下俯仰。检查六个字段和帧率。在空鼠开启及关闭两种状态下，各测一次确认、返回、↑ 和 ↓，区分 BLE 按键报告、Windows 鼠标事件和目标应用响应。
3. 在录音软件中显式选择 `CABLE Output`。录制按下前静音、按住说话、松开后静音三段。只有虚拟麦克风明显有信号时，才算应用侧通过。运动窗口上的电平只代表桥接接到了数据。
4. 测试并发时，在按住语音键期间持续转动，松键后继续转动。需要定位暂停来源时，在 WSL 侧用 BlueZ 的 `btmon` 观察 HCI ACL，按 `0x0037` 和三个音频句柄分别统计。分别记录首帧、最长间隔和恢复时间，应用端光标移动延迟另测。
5. 验证本机版时，查看 Windows 上 TCP `3240` 的客户端进程并执行 `wsl --list --running`。最后正常退出，检查 `usbipd list` 恢复 `Not shared`。

抓包与日志应保存在仓库外。提交问题时，给出匿名的设备 VID/PID、错误码、报告引用和必要的统计值。完整 HCI 抓包有可能包含蓝牙地址、配对材料、输入内容及语音。可以用报告长度和时间戳代替音频负载。
