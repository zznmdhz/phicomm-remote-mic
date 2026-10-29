"""一次性抓包/录音参考工具：连接斐讯遥控器，抓原始 BLE 语音包并落盘。

从原型 capture.py 改造：支持 --addr（默认扫描）与 --seconds（默认 10 秒），
输出写到当前工作目录：capture-<时间戳>.json / .msbc / .wav。

用法：
    python capture.py                       # 扫描遥控器，录 10 秒
    python capture.py --addr 00:E0:4C:2C:04:B6 --seconds 30
"""
import argparse
import asyncio
import collections
import json
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / 'deps'))
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

RX = '00002b00-0000-1000-8000-00805f9b34fb'  # 遥控器 → 电脑（通知语音/控制包）
TX = '00002b01-0000-1000-8000-00805f9b34fb'  # 电脑 → 遥控器（写控制命令）
NAME_HINTS = ('斐讯', 'Phicomm', 'DB1-R', 'BD1-R')


async def resolve_address(addr, timeout=8.0):
    """未指定 --addr 时扫描匹配名字的遥控器；返回蓝牙地址。"""
    if addr:
        return addr
    print('SCANNING', flush=True)
    for dev in await BleakScanner.discover(timeout=timeout):
        name = dev.name or ''
        if any(hint in name for hint in NAME_HINTS):
            print('FOUND', dev.address, name, flush=True)
            return dev.address
    raise RuntimeError('未扫描到遥控器，请按遥控器方向键唤醒后重试。')


async def main(addr, seconds):
    address = await resolve_address(addr)
    packets = []
    stamp = time.strftime('%Y%m%d-%H%M%S')
    base = Path.cwd() / ('capture-' + stamp)
    async with BleakClient(BLEDevice(address, 'Phicomm', None), timeout=15,
                           winrt={'use_cached_services': False}) as client:
        def incoming(char, data):
            packets.append({'t': time.time(), 'length': len(data), 'hex': data.hex()})
            if len(data) != 120:
                print('CONTROL', data.hex(), flush=True)

        async def send(data):
            print('TX', data.hex(), flush=True)
            await client.write_gatt_char(TX, data, response=True)

        await client.start_notify(RX, incoming)
        try:
            # 握手命令字节含义（均为 20 字节包，写入 TX=2B01 特征）：
            # 1) 08 01 + 4 字节随机数(31bit, uint32LE) + 01 03 + 12 字节 0
            #    08=Kehwin 私有命令域；01=握手/会话建立；随机数作一次性挑战值防重放；
            #    01 03=版本/类型标记；其余补 0 凑满 20 字节。
            await send(bytes([8, 1]) + struct.pack('<I', secrets.randbits(31))
                       + bytes([1, 3]) + bytes(12))
            await asyncio.sleep(.3)
            # 2) 08 0C + 4 字节 0 + 01 + 13 字节 0
            #    0C=第二阶段协商（能力/格式确认），随后遥控器才允许开语音流。
            await send(bytes([8, 12]) + bytes(4) + bytes([1]) + bytes(13))
            # 3) 08 06 01 + 17 字节 0
            #    06=语音流控制；参数 01=开始上报 mSBC 音频（此后 2B00 上收 120 字节包）。
            await send(bytes([8, 6, 1]) + bytes(17))
            print('READY_RECORDING_%d_SECONDS' % seconds, str(base), flush=True)
            await asyncio.sleep(seconds)
        finally:
            try:
                # 4) 08 06 00 + 17 字节 0
                #    06 同上；参数 00=停止上报语音，断开前必发，否则遥控器会持续发包耗电。
                await send(bytes([8, 6, 0]) + bytes(17))
                await asyncio.sleep(.2)
            except Exception as e:
                print('STOP_ERROR', str(e), flush=True)
    base.with_suffix('.json').write_text(json.dumps(packets, indent=2), encoding='utf-8')
    frames = []
    for row in packets:
        b = bytes.fromhex(row['hex'])
        if len(b) == 120:  # 每个 120 字节通知含两个 60 字节 H2 mSBC 帧
            for i in [0, 60]:
                block = b[i:i+60]
                if block[0] == 1 and block[2] == 0xad:  # H2 头 + mSBC 同步字
                    frames.append(block[2:59])          # 取 57 字节 mSBC 载荷
    base.with_suffix('.msbc').write_bytes(b''.join(frames))
    print('RESULT', dict(collections.Counter(x['length'] for x in packets)),
          'AUDIO_SECONDS', len(frames) * .0075, flush=True)
    if frames:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'sbc',
             '-i', str(base.with_suffix('.msbc')),
             '-acodec', 'pcm_s16le', str(base.with_suffix('.wav'))],
            capture_output=True, text=True)
        print('DECODE', result.returncode, result.stderr,
              'WAV', base.with_suffix('.wav'), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='斐讯遥控器 BLE 语音抓包/录音参考工具')
    parser.add_argument('--addr', default=None,
                        help='遥控器蓝牙地址；不指定则自动扫描（名字含 斐讯/Phicomm/DB1-R/BD1-R）')
    parser.add_argument('--seconds', type=int, default=10,
                        help='录音时长（秒），默认 10')
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(main(args.addr, args.seconds), args.seconds + 15))
    except Exception as e:
        print(type(e).__name__, str(e), flush=True)
        sys.exit(1)
