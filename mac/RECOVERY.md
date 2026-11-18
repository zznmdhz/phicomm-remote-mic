# macOS 恢复候选

使用 BLE 语音遥控器、FFmpeg 和 BlackHole 组成输入链路； Windows 的 VB-CABLE 不适用于 macOS。准备一套含 Tk 的独立 Python 环境，安装 `requirements-recovery.txt`。FFmpeg 可从 `PATH` 查找，也可使用 `N1_FFMPEG` 指定可执行文件。设置 `N1_DATA_DIR` 和 `N1_LOG_DIR` 为各自独立的私有位置，再从此目录执行 `python3 remote_mic_mac.py`。对于实际部署，固定允许蓝牙或辅助功能的具体 App 身份，而不是授权通用 Python；本分支未提供可分发的签名 App。

先配置 BlackHole 作为目标输入并授权具体应用蓝牙。先验证连接与本应用的测试回放，再分别验证输入法能否唤起、收音和松开结束。使用快捷键之前启用正确的左/右修饰键设置，且只在需要时授权辅助功能。F5 守卫可能影响实体键盘 F5；若不需要联动，保持守卫关闭。直接关闭程序即可停止；不需要清除或重置蓝牙设备。

本机恢复记录曾确认 BLE 连线和退出关流；并不代表所有设备或输入法的端到端兼容。旧 `launch_mic.sh` 和 `build_app.sh` 不作为本候选的安装指引；不要直接用于覆盖现有应用。私有启动器与设备状态不在同步范围。
