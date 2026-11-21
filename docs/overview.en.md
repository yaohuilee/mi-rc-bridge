# Xiaomi 2717:32B0 remote bridge for Windows

The bridge supports six-axis monitoring, an air mouse, and push-to-talk from the older Xiaomi BLE voice remote tested as VID/PID `2717:32B0`. The verified Windows host used an Intel `8087:0026` Bluetooth controller.

Install Python 3.12 with Tkinter, [usbipd-win](https://github.com/dorssel/usbipd-win), and optionally [VB-CABLE](https://vb-audio.com/Cable/) for the microphone. Install [the Python dependencies](../requirements-windows.txt), write your remote's BLE address to an untracked `remote-address.txt`, and run [Start-MiRcNativeMonitor.cmd](../Start-MiRcNativeMonitor.cmd). Approve elevation. On the first pairing, hold Home + Menu. Later, a direction key can wake the remote for reconnection.

The native backend runs Bumble on Windows through USB/IP over loopback. The WSL2 backend remains available via [Start-MiRcMonitor.cmd](../Start-MiRcMonitor.cmd). Either backend temporarily owns the Bluetooth controller. Use **Stop** to release it. Select **CABLE Output (VB-Audio Virtual Cable)** in the recording application's microphone menu. Live capture from this endpoint was verified with no WSL distribution running.

The tested remote pauses motion reports during voice streaming. The monitor retains mouse calibration and resumes input when fresh reports arrive. Other remote revisions and controllers require verification.

The [Chinese setup guide](setup.md), [protocol reference](protocol.md), [field notes](field-notes.md), and [reproduction guide](development.md) document the configuration and measurement details.
