# 小米语音遥控器 Windows 桥接

让旧款小米蓝牙语音遥控器 **2717:32B0** 在 Windows 上提供六轴监视、空鼠和按住说话。优先使用 **Windows 本机版**。WSL2 版保留用于复现和诊断。

Windows 本机版已用一台 Intel Bluetooth `8087:0026` 主机完成真机录音。Windows 录音程序从 `CABLE Output` 收到语音，松开按键后恢复静音。测试时没有 WSL 发行版在运行。其他小米遥控器 PID、其他蓝牙控制器和固件版本有待验证。

| 功能 | 当前支持 |
|---|---|
| 连接 | 重用保存的 BLE 配对，遥控器休眠时可按方向键唤醒 |
| 体感 | 实时显示三轴加速度和三轴陀螺仪，静置约 50 Hz，输出原始计数 |
| 空鼠 | 转动控制指针，确认键左键，返回键右键，↑↓ 滚动，支持灵敏度和校准 |
| 麦克风 | 按住语音键启动传输，松开停止。Windows 软件可选择 VB-CABLE 的录音端 |
| 语音与体感 | 真机录音期间六轴暂停。空鼠保留校准，等六轴恢复后继续工作 |

## 安装和首次配对

1. 安装带 Tkinter 的 Windows Python 3.12 和 [usbipd-win](https://github.com/dorssel/usbipd-win)。可用 `usbipd list` 确认本机 Intel 蓝牙控制器为 `8087:0026`。启用麦克风时，另外安装 [VB-CABLE](https://vb-audio.com/Cable/)，按厂商步骤完成驱动安装。
2. 在项目目录执行 `python -m pip install -r requirements-windows.txt`。若 Python 安装在用户目录，确认提权后使用同一安装和用户。
3. 在项目根目录创建本地文件 `remote-address.txt`，只写遥控器的 BLE 地址，格式为 `XX:XX:XX:XX:XX:XX`。地址可从 Windows 已识别的 Bluetooth LE 设备实例 ID 中取得，见[查找遥控器地址](docs/setup.md#查找遥控器地址)。可复制 [remote-address.example.txt](remote-address.example.txt) 后修改。该文件已被 Git 忽略。
4. 双击 [Start-MiRcNativeMonitor.cmd](Start-MiRcNativeMonitor.cmd)，确认管理员提示。首次启动收到配对提示时，把遥控器靠近电脑，按住「小房子＋菜单」进入配对。以后重连一般只需短按方向键唤醒。
5. 要用空鼠，勾选「空鼠」后让遥控器按键朝上静放约一秒。按 `Esc` 关闭。要用麦克风，在会议、录音或语音输入软件里选择 **CABLE Output (VB-Audio Virtual Cable)**，按住语音键说话。

运行期间桥接独占这块蓝牙适配器。使用窗口的「停止」按钮交还适配器。如果上一次异常退出，在没有其他桥接运行时用 `Start-MiRcMotion.cmd --restore` 恢复。VB-CABLE 安装后可在 Windows 声音设置中检查默认播放和录音设备，再按应用指定虚拟麦克风。

## 文档

- [安装、使用及排障](docs/setup.md)覆盖地址获取、重连、两个后端、虚拟麦克风和实验按键工具。
- [BLE、六轴、语音和 USB/IP 协议](docs/protocol.md)包含报告布局、启动命令、解码与连接生命周期。
- [踩坑记录与验证结果](docs/field-notes.md)记录不同方案的失败点、修复、实测数据和限制。
- [开发和复现](docs/development.md)提供代码地图、离线测试和真机复测步骤。
- [English summary](docs/overview.en.md)

代码采用 [MIT 许可证](LICENSE)。Bumble、usbipd-win 和 VB-CABLE 由各自项目提供。仓库附带实测 HID Report Map（109 字节），用于校验设备类型。它只描述报告格式。
