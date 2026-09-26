# MouseLink source / MouseLink 源码

See [English instructions](../README.md) or [简体中文说明](../README.zh-CN.md) for devices, installation, use, firmware flashing and development.

- `desktop_app.py`: PySide6 desktop interface / 桌面界面。
- `bridge.py`, `native_edges.py`, `trial_bridge.py`: Windows input and mixed mouse transport / Windows 输入与混合鼠标通道。
- `main/`: ESP32-C3 BLE HID firmware source / 开发板固件源码。
- `flash_helper.py`, `firmware_bundle.py`, `flash_dialog.py`: backup, flashing and restoration / 备份、刷写与恢复。
- `device-calibration.json`: blank public template; personal settings stay local / 公开空白模板，个人设置保存在本机。
- `firmware/manifest.json`: firmware package metadata; generate binaries using `tools/Build-Firmware.ps1` / 固件元数据，二进制需从源码构建。

The normal 1.0.2 desktop entry has no session timer. The explicit `--native-edges` diagnostic entry retains its isolated settings and bounded sessions for development. 日常入口不限时；显式诊断入口仍使用隔离设置和限时会话。
