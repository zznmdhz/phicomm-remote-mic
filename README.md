# phicomm-remote-mic

把斐讯 N1 / DB1-R 蓝牙语音遥控器变成 Windows 无线麦克风，喂给 AI 应用（Codex / Claude / 微信语音输入等）——不用买麦克风，也不需要 N1 盒子或官方 App。

## 效果

连接成功后，在任意 AI 应用的麦克风列表里选 **CABLE Output**，按住遥控器上的语音键说话，声音即进入该应用；松开即停。真人语音实测可被离线 ASR（Vosk）清晰识别，链路环回重建相关度 0.998、零丢包。

## 工作原理

```
┌────────────────┐  BLE   ┌─────────────────────┐ 120B 通知包 ┌────────────┐
│ 斐讯语音遥控器   │ ─────► │ phicomm-remote-mic   │ ─────────► │  FFmpeg    │
│ DB1-R / N1     │ Kehwin │ (Python)             │  2×60B     │  -f sbc    │
│ 语音键 + 麦克风 │ 私有协议│ 握手 / 开流 / 帧重组   │  mSBC 帧   │  解码 PCM  │
└────────────────┘        └──────────┬───────────┘            └─────┬──────┘
                                     │ WASAPI                       │ pcm_s16le
                                     ▼                              ▼
                          ┌──────────────────────┐  PCM   ┌─────────────────┐
                          │ VB-CABLE             │ ◄───── │ sounddevice     │
                          │ CABLE Input（声卡输入）│        │ 音频桥接线程     │
                          └──────────┬───────────┘        └─────────────────┘
                                     │ 系统内部环回
                                     ▼
                          ┌──────────────────────────────┐
                          │ AI 应用：麦克风选 CABLE Output │
                          └──────────────────────────────┘
```

一句话链路：**遥控器 → BLE → Kehwin 协议 → mSBC → FFmpeg → VB-CABLE → AI 应用**。

## 快速开始

1. **安装 VB-CABLE**：<https://vb-audio.com/Cable/>（官方签名安装包，装完系统会出现 `CABLE Input` / `CABLE Output` 两个音频端点）。
2. **安装 Python 3.9+**。
3. **安装依赖**：`pip install -r requirements.txt`
4. **确认 FFmpeg 在系统 PATH 中**：命令行执行 `ffmpeg -version` 能出版本号即可（本项目用 `ffmpeg -f sbc` 解码 mSBC）。
5. **启动 app**：运行仓库 `app/` 下的主程序。

## 使用

1. **连接**：遥控器闲置会休眠，先按一下任意**方向键**唤醒它，再在程序里点「连接」。
2. **验证**：用「测试输入」功能确认链路通了（有音频数据进来）。
3. **选麦克风**：在 AI 应用的音频设置里，把麦克风选成 **CABLE Output**（注意不是 CABLE Input）。
4. **说话**：**按住**遥控器上的语音键说话，松开即停止发送。
5. **退出**：关闭程序会自动发送停止命令并断开连接，不会把遥控器留在开流状态。

## 项目结构

| 路径 | 说明 |
| --- | --- |
| `app/` | 主程序：BLE 直连、握手开流、mSBC 帧重组、FFmpeg 解码、WASAPI 桥接到 VB-CABLE（协议实现核心见 `app/remote_mic.py`） |
| `tools/` | 辅助脚本（抓包、环回/实录测试等） |
| `docs/PROTOCOL.md` | Kehwin 私有语音协议完整文档（服务表、命令表、帧格式） |
| `docs/RESEARCH.md` | 协议逆向的研究过程、负结果与复盘 |
| `requirements.txt` | Python 依赖（另需系统级 ffmpeg 与 VB-CABLE） |
| `LICENSE` | MIT |

## 协议摘要

遥控器的语音通道是自定义 GATT 服务 `0x1920`：特征 `2B00` 负责下行（音频 + 控制，notify），特征 `2B01` 负责上行（20 字节命令，write with response）。

关键事实：**语音键按下时遥控器不会主动发任何音频**——必须主机先完成握手（随机数挑战 + 应答）再发开流命令，音频才会开始推送。音频以 120 字节通知包下发，内含 2 帧 60 字节的 mSBC（16 kHz 单声道，7.5 ms/帧）。完整命令表与帧格式见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

## 兼容性（诚实声明）

- 仅在 **DB1-R**（斐讯 N1 语音遥控器，固件 `RTL_618_V0001`，软件版本 `100011_201908241450`）上完成真机端到端验证。
- 其他使用 Kehwin 方案（同一 APK `KehwinBLE.apk` + `libmbleRuntime.so`）的遥控器**理论上兼容，但未验证**，本仓库不承诺任何未列出的型号。
- 仅支持 **Windows**（依赖 VB-CABLE 与 WASAPI）。

## 致谢

- 协议逆向：**Codex CLI** —— 对官方驱动包内 `KehwinBLE.apk` 做 JADX 静态反编译、对 `libmbleRuntime.so` 做 capstone/pyelftools 分析，全程未执行任何第三方 APK 或 native 库。
- 工程化与文档：**Hermes Agent**。
- 真机验证记录与完整研究过程：见 [docs/RESEARCH.md](docs/RESEARCH.md)。

## License

[MIT](LICENSE) © 2026 zznmdhz。本仓库**不包含**任何 Kehwin APK / .so 或 VB-CABLE 驱动文件，请从官方或原分享渠道获取。
