#!/bin/bash
# 斐讯遥控器麦克风 (Mac) 启动器
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  echo "缺少 venv，请先运行：/Volumes/Data/Agent/.system/runtime/Python/bin/python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  exit 1
fi
exec ./venv/bin/python remote_mic_mac.py
