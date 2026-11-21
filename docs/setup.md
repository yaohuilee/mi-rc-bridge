# 安装、使用及排障

## 查找遥控器地址

如仍不知道地址，可先在 Windows 蓝牙设置里配对一次，在 PowerShell 查看当前实例 ID。只查找 `2717:32B0`。

```powershell
Get-PnpDevice -PresentOnly |
    Where-Object InstanceId -Match 'VID&0*2717_PID&32B0' |
    Select-Object FriendlyName, InstanceId
```

实例 ID 中，`REV&<版本>_` 后的连续 12 个十六进制字符是设备地址。把它每两字符分成一组，以冒号连接。若有多个遥控器，逐个连接或断开确认实例。也可用能显示 BLE 设备地址的扫描器查找广告名称 `MI RC` 或「小米语音遥控器」。将地址写入项目根目录的本地 `remote-address.txt`，或者在命令行提供 `--address`。

## 选择后端

| 后端 | 启动入口 | 传输与配对存储 |
|---|---|---|
| Windows 本机 | `Start-MiRcNativeMonitor.cmd` 或 `Start-MiRcMotion.cmd --native` | Windows Python → 本机 USB/IP → Intel 控制器。配对存于 `%LOCALAPPDATA%\MiRcMotion\bonds.json` |
| WSL2 | `Start-MiRcMonitor.cmd` 或 `Start-MiRcMotion.cmd` | Windows → usbipd-win → WSL2 → Bumble HCI socket。配对存于该发行版 root 的 `/root/.local/state/mi-rc-motion/bonds.json` |

两者共用一块物理适配器。先停止正在运行的桥接，再启动另一个。Windows 的蓝牙功能在桥接期间暂时无法使用该适配器，包括通过它连接的鼠标、键盘或耳机。确保有其他可用的输入方式。

WSL 版默认使用 `Ubuntu-24.04`。如需运行在其他 Ubuntu 发行版，在命令行传 `--distro <名称>`。首次会尝试安装缺少的 Python 虚拟环境、Bumble 和 BlueZ 工具。启动器会在所选发行版停止 `bluetooth` 服务，让 Bumble 访问 `hci0`。若发行版已有其他使用该服务的任务，优先使用专用发行版或 Windows 本机版。

本机版会连接已经运行的 Windows `usbipd` 服务，目标固定为 `127.0.0.1:3240`。服务的防火墙和监听范围由 usbipd-win 管理，桥接仅在运行期间共享这块适配器。 [usbipd-win 文档](https://github.com/dorssel/usbipd-win)

## 配对、重连和停止

- 首次看见「等待首次配对」时，把遥控器放到电脑附近，按住「小房子＋菜单」。固件可能没有可见指示灯，可看窗口状态。旧的 Windows 配对和 Bumble 保存的配对属于不同的蓝牙栈。如果旧配对妨碍首次连接，再在 Windows 移除这只遥控器的旧条目。每台设备的加密密钥由后端单独保存。
- 后续显示「等待遥控器」时，先短按方向键唤醒，留一点时间给定向连接和扫描。只有已有配对确实失效时才加 `--repair`。曾观察到个别首次配对阶段长时间无广播，短暂取下一节电池再按配对组合键可重新唤醒。常规重连无需拆电池。
- 正常停止用窗口的「停止」或命令行 `Ctrl+C`。桥接停止采样、关闭传输，再解除共享。如果程序异常终止且 `usbipd list` 仍显示 Shared 或 Attached，确认是本项目留下的占用后运行 `Start-MiRcMotion.cmd --restore`，再检查设备是否为 Not shared。
- 管理员提示取消或超时会使窗口报桥接异常。重新点击「启动」并确认提示。提权用于绑定适配器。
- 安装过 USBPcap 时，usbipd-win 可能提示要用 `bind --force`。启动器已经处理，且只选择 Intel `8087:0026`。若电脑上检测到多个相同控制器，程序会报错以免接管错误设备。

## 空鼠和录音

空鼠默认关闭。静止校准时把按键面朝上，暂时不要按键或旋转。校准成功后，绕 Z 轴左右转动控制指针水平移动，绕 X 轴俯仰控制垂直移动。确认、返回和上下键只有在启用并校准后才转换成左键、右键和滚轮。每次短按方向键滚动一格，长按有重复。按 `Esc` 关闭空鼠。

如果应用选择录音设备，选 **CABLE Output (VB-Audio Virtual Cable)**。桥接内部写入的是同一驱动的播放端 `CABLE Input`。设备名称方向以虚拟线缆为参照。先观察监视窗口的语音电平，再用独立录音应用确认输入。两处都无电平时，检查 VB-CABLE 端点是否可用、是否选了正确的设备，以及窗口是否显示麦克风就绪。安装后若 Windows 更改了默认扬声器，可在声音设置中恢复正常扬声器。

默认语音只在按下和松开之间解码，桥接不保存 WAV。语音关闭用 `--no-microphone`。部分应用会记住上次选择的录音设备，启动桥接后可回到应用的设备菜单确认。

## 命令行示例

```powershell
# 本机版，仅体感，接收 500 帧后停止
.\Start-MiRcMotion.cmd --native --no-microphone --samples 500

# 输出 JSON，另将状态及六轴存到一个新文件
.\Start-MiRcMotion.cmd --native --json --capture motion.ndjson --seconds 30

# 明确要求重建这只遥控器的配对
.\Start-MiRcMotion.cmd --native --repair
```

运动数据默认发给本机 UDP `127.0.0.1:8765`，可用 `--port` 更改。语音内容不会发往六轴端口或写入诊断文件。脚本在已提升权限的终端里运行时，标准输出可直接供管道读取。

## 早期 USBPcap 按键工具

[tools/usbpcap_keys.py](../tools/usbpcap_keys.py) 保留了 Windows 自己维持 BLE 配对时的诊断和按键映射，可用于复现早期实验。需要 [USBPcap](https://desowin.org/usbpcap/)，与两个主桥接后端分开使用。先在管理员终端查看 USBPcap 界面，选中 Intel 蓝牙所在 USB 根集线器和正确的 USB 设备地址。下面的数字仅为示例。

```powershell
python .\tools\usbpcap_keys.py --usb-interface '\\.\USBPcap1' --usb-address 5 --trace-reports --seconds 30
```

`--trace-reports` 只记录，不注入按键。已有连接通过 [Probe-MiRcConnection.ps1](../tools/Probe-MiRcConnection.ps1) 的公开 GATT 读取关联到 HCI handle，也可手工传 `--connection-handle`。旧抓包回放支持经典 USBPcap `.pcap`，需提供 `--usb-address`，不支持直接读 pcapng。

确认键映射为 Enter，四个方向键映射为箭头，音量键映射为 Windows 音量按键。Windows 若已经映射某个键，会和工具同时产生输入。历史版本用全局钩子处理右方向键重复，公开版仅在明确开启 `--filter-native-right` 时启用该实验钩子，它也会暂时拦截普通键盘的原生右方向键。日常六轴、空鼠和麦克风用主桥接。
