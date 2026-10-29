# 研究过程与复盘：把斐讯蓝牙遥控器变成 Windows 麦克风

本文记录 phicomm-remote-mic 背后的协议逆向全过程（2026-10-29 完成），包括走过的弯路与负结果。协议结论本身见 [PROTOCOL.md](PROTOCOL.md)。

## 1. 目标与约束

- 目标：让已有的斐讯 N1 蓝牙语音遥控器（电池仓标注 DBR1，属 DB1-R 家族）在 Windows 上变成真正的麦克风，供 AI 应用使用。
- 约束：用户接受逆向与时间成本，**不接受**"买个麦克风/换遥控器"这类替代方案；N1 盒子未拆封且无法配置，全程不能依赖盒子。

## 2. 时间线

1. **真机勘察**：设备名"斐讯遥控器"，MAC `00:E0:4C:2C:04:B6`，VID/PID `005D:0001`，BLE DIS 显示 Realtek BT、Model 0.9、`RTL_618_V0001`、软件版本 `100011_201908241450`。HID 描述符只有键盘与 consumer controls 两个 collection，无 HID audio；Windows 活动输入端点里也没有遥控器麦克风——语音必然走自定义 GATT。
2. **GATT 服务表测绘**：标准 1800/1801/180A/180F/1812 之外，发现公模 `d0ff`/`6287`（base `3C17-D293-8E48-14FE-2E4DA212`，疑似 OTA/DFU，全程未写入）与自定义服务 `1920`：特征 `2B00`（notify/read）下行、`2B01`（write/read/write-no-response）上行，CCCD `0100`。
3. **负结果：被动订阅 60 秒零包**。订阅 `2B00`，用户 physically 按住语音键持续 60 秒，**收到 0 个包**。该结果只否定"被动订阅即可拿到音频"这一条路径，**不证明**语音功能不存在，也不**证明** 1920 一定是语音通道。后续证明真相是：必须主机先握手开流，遥控器才推音频。
4. **转向厂商参考实现**：放弃盲试 GATT，改为找官方驱动。恩山论坛固件作者原帖（<https://www.right.com.cn/forum/thread-4078508-1-1.html>）明确指出 DB1-R 的语音支持在 `KehwinBLE.apk` 与 `lib/arm/libmbleRuntime.so` 中。
5. **拿到驱动包**：旧帖中的 MEGA（`GFsj0SgQ`，已失效 -9）、亿云（跳 generic 首页）、旧百度盘（`1uXZx0opc1PMoeQLWAwVwkQ`，已失效）全部作废；从新帖评论拿到可用公开镜像：<https://pan.baidu.com/s/1VeW-mah8VoXRtzYu5rXmrw?pwd=jasu>（提取码 `jasu`），路径 `PhicommP1_LiliN_Monarch / Phi_LiliN_54_CastleBravo / PATCH_FILE / DB1-R_VOICE / DB1-R_VOICE.7z`，14,316,816 字节。用户手动下载后本地校验 7z magic/大小/hash，安全列目录后解出 APK 与 .so。
6. **静态逆向**：JADX 1.5.6 反编译 APK（对照 GitHub release digest 校验 SHA256 `545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974`），capstone/pyelftools 分析 ARM 库，定位 UUID `1920`/`2B00`/`2B01`、命令字节与开停流序列。**全程未执行**下载的 APK 或 native 库。
7. **真机验证**：重建握手（`08 01` 挑战 → 300 ms → `08 0C` → `08 06 01` 开流），收到 120 字节通知包 = 2×60 字节 H2 mSBC 帧，FFmpeg 解码成功。环回测试 8.43 s / 562 包 / 0 丢包，重建相关度 0.998；真人语音 12.42 s / 828 包 / 0 丢包，Vosk 离线识别内容可辨。
8. **工程化落地**：PCM 经 WASAPI 写入 VB-CABLE 的 `CABLE Input`，AI 应用选 `CABLE Output` 作麦克风；音频线程先 `CoInitializeEx(MTA)`，关闭时必发 `08 06 00` 停流。

## 3. 值得记录的坑

- **寻址坑**：已配对/已连接的 BLE 设备可能不再广播，bleak 用字符串地址扫描会找不到；改用 `BLEDevice(address, '名', None)` 直接构造对象立刻连上。WinRT 侧需 `use_cached_services=False`。
- **OS 保护的 GATT**：服务 `1812`（HID）在 bleak 枚举中被省略/保护，不能因枚举缺失就判定服务不存在（PnP/HID 描述符可交叉证明）。
- **论坛抓取**：恩山页面用常规抓取工具返回 403，用 Python `requests` 直接请求成功，且响应必须按原始字节以 gbk/gb18030 解码，不能猜编码。
- **弯路一：厂商 SDK 文档不通用**。Realtek 较新 SDK 设计文档里的语音参考用自定义 vendor service `000003FD`（特征 `3A40`/`3A41`），与本遥控器的 `0x1920` 完全不同，命令与编解码不能照搬。
- **弯路二：AOSP 仓库无收获**。检查了 khadas/amlogic-common 与 TheMuppets/vendor_amlogic 两个稀疏仓库，其 `BluetoothRemote.apk` 只是固件/配置应用，无 Kehwin 语音驱动，此分支不必重做。
- **弯路三：REA 不必要**。评估过官方上游 <https://github.com/morluto/rea>（编排 JADX 等工具的编排器），但直接用 JADX + 静态分析即可，REA 并不直接理解 BLE 外设，最终未采用。
- **弯路四：同类项目不兼容**。公开检索没有现成的"斐讯遥控器 Windows 麦克风桥"；已知 RemoteMic 类项目支持 Xiaomi/Chromecast ATVV 方案，不能假定互通。
- **浏览器自动化不可用**：控制 Chrome 标签页的 create/get/IAB 调用均 30 s 超时（盘点可以、控制不行），不要重复同样的失败，也不要从磁盘读浏览器 cookie 绕过登录。

## 4. 工具链

| 工具 | 用途 |
| --- | --- |
| Python 3.12（`C:\Python312\python.exe`） | 主脚本；本地 `./deps` 装 bleak、hidapi、py7zr、pyelftools、capstone，不污染全局 |
| JADX 1.5.6（Java 22 在 PATH，另装 JDK 17） | APK 静态反编译，ZIP 按 GitHub release digest 校验 SHA256 |
| capstone / pyelftools | `libmbleRuntime.so` 的 ELF 检查与 ARM 指令分析 |
| Python `requests` + gb18030 解码 | 恩山论坛页面抓取（绕过 403 的可用路径） |
| FFmpeg（`-f sbc`） | mSBC → pcm_s16le 解码 |
| Vosk（离线 ASR） | 真人语音可懂度的独立验证 |
| VB-CABLE + WASAPI（sounddevice） | 虚拟声卡桥接 |

## 5. 复盘教训

1. **协议逆向先挖厂商参考实现，后碰设备**。前 2 小时在真机上盲试 GATT 只换来一个 60 秒零包的负结果；而拿到官方 APK/so 做静态分析后，命令序列、开停流、编解码几分钟内全部明确，真机仅用于交叉验证。设备实验台的价值是**验证**，不是**发现**。
2. **负结果要精确表述**。"订阅 60 秒零包"的正确结论是"被动路径不通，需主机主动开流"，而不是"语音不可能"或"1920 就是语音"。过早把负结果当定论会直接终结项目。
3. **先证明音频，再上驱动**。在遥控器音频链路跑通之前不安装任何音频驱动；VB-CABLE 在协议验证成功后才装。
4. **第三方二进制只做静态分析**。下载的 APK 与 ARM .so 从不执行，避免供应链风险。
5. **关闭路径与打开路径同等重要**。停止命令 `08 06 00` 必须在任何退出路径上发送，否则设备停留在开流状态。
