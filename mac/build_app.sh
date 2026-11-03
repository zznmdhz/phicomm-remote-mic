#!/bin/bash
# 重建「斐讯遥控器麦克风.app」（改了 launcher.applescript 或换机器后跑一次）
# 产物：本目录 斐讯遥控器麦克风.app（双击启动，不经过终端）
cd "$(dirname "$0")" || exit 1
rm -rf "斐讯遥控器麦克风.app"
osacompile -o "斐讯遥控器麦克风.app" launcher.applescript || exit 1
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.mini.phicomm.remotemic" \
  "斐讯遥控器麦克风.app/Contents/Info.plist" 2>/dev/null || true
codesign --force --deep -s - "斐讯遥控器麦克风.app"
echo "已生成: $(pwd)/斐讯遥控器麦克风.app"
echo "提示：首次运行要在 系统设置→隐私与安全性→辅助功能 里勾上它"
