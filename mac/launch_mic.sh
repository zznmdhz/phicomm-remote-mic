#!/bin/bash
# 斐讯遥控器麦克风 —— 无终端窗口启动器
# 说明：本脚本由「斐讯遥控器麦克风.app」调用，不要直接双击。
set -u
APP_DIR="/Volumes/Data/User Files/Personal/n1_remote_mic/repo/mac"
cd "$APP_DIR" || exit 1

# 结束残留实例（只匹配 python 解释器，不会误杀本脚本）
for pid in $(pgrep -f "venv/bin/python remote_mic_mac.py"); do
  kill "$pid" 2>/dev/null
done
sleep 0.4

# 后台拉起 GUI，日志落文件；父进程立即返回，不占用终端
nohup ./venv/bin/python remote_mic_mac.py >> gui-out.txt 2>&1 &
disown 2>/dev/null || true
exit 0
