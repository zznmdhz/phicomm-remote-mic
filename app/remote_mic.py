"""斐讯 DB1-R 遥控器 → Windows VB-CABLE 虚拟麦克风（增强版 GUI）。

保留已真机验证的核心链路：BLEDevice 直连、Kehwin 握手、mSBC 分片重组、
ffmpeg -f sbc 解码、WASAPI CABLE Input 推流（48k 立体声）、工作线程
MTA CoInitializeEx、单实例 mutex、RotatingFileHandler 日志。
增强：地址可配置（--addr / config.json / 自动扫描）、测试输入（CABLE
Output 回环录音并回放）、录音列表、声音设置与切换指引。
只用 stdlib + bleak + sounddevice；ffmpeg 走子进程。
"""
import argparse
import asyncio
import collections
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'deps'))  # 便携 deps 目录方式（若存在）
import sounddevice as sd
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

RX = '00002b00-0000-1000-8000-00805f9b34fb'
TX = '00002b01-0000-1000-8000-00805f9b34fb'
RATE = 48000
CHANNELS = 2
TEST_SECONDS = 5
CONFIG_PATH = ROOT / 'config.json'
RECORDINGS_DIR = ROOT / 'recordings'
NAME_HINTS = ('斐讯', 'Phicomm', 'DB1-R', 'BD1-R')


def cable_device(capture=False):
    needle = 'CABLE Output' if capture else 'CABLE Input'
    field = 'max_input_channels' if capture else 'max_output_channels'
    for i, dev in enumerate(sd.query_devices()):
        if (needle in dev['name'] and '16ch' not in dev['name']
                and dev[field] >= 2
                and sd.query_hostapis(dev['hostapi'])['name'] == 'Windows WASAPI'):
            return i
    raise RuntimeError('找不到 VB-CABLE，请检查虚拟声卡是否已安装。')


def load_config():
    """读取 app/config.json；缺失/损坏时返回空 dict。"""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_config_value(key, value):
    data = load_config()
    data[key] = value
    try:
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False,
                                          indent=2) + '\n', encoding='utf-8')
    except OSError:
        logging.warning('无法写入配置 %s', CONFIG_PATH, exc_info=True)


def load_saved_address():
    """读取 config.json 中持久化的遥控器地址；未配置返回 None。"""
    return load_config().get('addr') or None


def save_address(address):
    """首次连接成功后把地址持久化到 app/config.json，下次免扫描。"""
    save_config_value('addr', address)


def load_dictation_enabled():
    """语音键联动开关状态，默认开启。"""
    return bool(load_config().get('dictation_hotkey_enabled', True))


def save_dictation_enabled(value):
    save_config_value('dictation_hotkey_enabled', bool(value))


VK_RMENU = 0xA5  # 右 Alt 虚拟键码
KEYEVENTF_KEYUP = 0x0002
DEFAULT_HOTKEY = {'mods': [], 'key': 'ralt', 'vk': VK_RMENU, 'mod_vks': []}

_KEYSYM_VK = {**{chr(c): c for c in range(ord('a'), ord('z') + 1)},
              **{str(d): 0x30 + d for d in range(10)},
              'space': 0x20, 'return': 0x0D, 'escape': 0x1B, 'tab': 0x09,
              'minus': 0xBD, 'equal': 0xBB, 'comma': 0xBC, 'period': 0xBE,
              'slash': 0xBF, 'grave': 0xC0, 'bracketleft': 0xDB,
              'bracketright': 0xDD, 'semicolon': 0xBA, 'quote': 0xDE,
              'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28,
              'home': 0x24, 'end': 0x23, 'prior': 0x21, 'next': 0x22,
              'insert': 0x2D, 'delete': 0x2E}
_KEYSYM_VK.update({'f%d' % i: 0x6F + i for i in range(1, 13)})


def keysym_to_vk(keysym):
    """tk keysym → Windows 虚拟键码；不支持返回 None。"""
    k = keysym.lower()
    if k in _KEYSYM_VK:
        return _KEYSYM_VK[k]
    if len(k) == 1 and k.isprintable():
        return ord(k.upper())
    return None


def load_hotkey():
    """读取自定义听写快捷键；缺失/损坏返回默认（右Alt）。"""
    hk = load_config().get('hotkey')
    if (isinstance(hk, dict) and isinstance(hk.get('vk'), int)
            and isinstance(hk.get('mods'), list) and 'key' in hk):
        return hk
    return dict(DEFAULT_HOTKEY)


def save_hotkey(hotkey):
    save_config_value('hotkey', hotkey)


def describe_hotkey(hotkey):
    """组合键的显示文本，如 'ctrl+alt+d'、'右Alt'。"""
    alias = {'ralt': '右Alt', 'lalt': '左Alt', 'lctrl': '左Ctrl', 'rctrl': '右Ctrl',
             'space': '空格', 'return': '回车'}
    key = str(hotkey.get('key', '')).lower()
    parts = [m + '+' for m in hotkey.get('mods', [])]
    return ''.join(parts) + alias.get(key, key)


def send_dictation_tap(hotkey):
    """按配置合成一次听写快捷键点按（修饰键 down → 主键 down/up → 修饰键 up）。"""
    send_dictation_key(hotkey, 'down')
    send_dictation_key(hotkey, 'up')


def send_dictation_key(hotkey, action):
    """按配置按下('down')/抬起('up')听写键——真按住/真松开，配合长按式听写。"""
    try:
        user32 = ctypes.windll.user32
        mod_vks = hotkey.get('mod_vks') or []
        vk = int(hotkey['vk'])
        if action == 'down':
            for m in mod_vks:
                user32.keybd_event(int(m), 0, 0, 0)
            user32.keybd_event(vk, 0, 0, 0)
        else:
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            for m in reversed(mod_vks):
                user32.keybd_event(int(m), 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        logging.warning('合成听写键 %s 失败', action, exc_info=True)


def send_right_alt_tap():
    """兼容保留：默认右Alt 点按。"""
    send_dictation_tap(DEFAULT_HOTKEY)


async def scan_remote(status, timeout=8.0):
    """扫描 BLE 设备，返回 (address, name) 或 (None, None)。"""
    status('正在扫描遥控器……请按遥控器方向键唤醒它。')
    found = await BleakScanner.discover(timeout=timeout)
    for dev in found:
        name = dev.name or ''
        if any(hint in name for hint in NAME_HINTS):
            return dev.address, name
    return None, None


class AudioPipe:
    """Decode mSBC, resample and continuously feed the virtual cable."""
    def __init__(self):
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.closed = False
        self.packets = 0
        self.last_packet = 0
        self.speech_event = threading.Event()  # 每收到一个音频帧置位（联动用）
        self.decoded_bytes = 0
        self.dropped_bytes = 0
        self.error = None
        self.fragment = bytearray(120)
        self.fragment_next = 1
        self.proc = None
        self.stream = None
        self.reader = None

    def start(self):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise RuntimeError('找不到 FFmpeg，请将 FFmpeg 加入 PATH。')
        cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-probesize', '32',
               '-analyzeduration', '0', '-f', 'sbc', '-i', 'pipe:0',
               '-acodec', 'pcm_s16le', '-ar', str(RATE), '-ac', str(CHANNELS),
               '-f', 's16le', '-flush_packets', '1', 'pipe:1']
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.stream = sd.RawOutputStream(device=cable_device(), samplerate=RATE,
                channels=CHANNELS, dtype='int16', blocksize=480,
                latency='low', callback=self._output)
            self.stream.start()
        except Exception:
            self.close()
            raise
        return self

    def _read(self):
        try:
            while True:
                data = self.proc.stdout.read(1920)
                if not data:
                    break
                with self.lock:
                    self.buffer.extend(data)
                    self.decoded_bytes += len(data)
                    # Bound latency after Bluetooth stalls; preserve sample boundaries.
                    excess = len(self.buffer) - RATE * CHANNELS * 2 // 2
                    if excess > 0:
                        excess -= excess % 4
                        del self.buffer[:excess]
                        self.dropped_bytes += excess
        except Exception as exc:
            if not self.closed:
                self.error = str(exc)
                logging.exception('Decoder reader failed')

    def _output(self, outdata, frames, timing, status):
        size = len(outdata)
        with self.lock:
            count = min(size, len(self.buffer))
            outdata[:count] = self.buffer[:count]
            del self.buffer[:count]
        if count < size:
            outdata[count:] = bytes(size - count)

    def feed(self, data):
        if self.closed:
            return
        data = bytes(data)
        if len(data) == 20 and data[0] & 31 == 20:
            idx = data[1]
            if idx == 1:
                self.fragment_next = 1
            if idx != self.fragment_next or not 1 <= idx <= 7:
                self.fragment_next = 1
                return
            count = 12 if idx == 7 else 18
            self.fragment[(idx-1)*18:(idx-1)*18+count] = data[2:2+count]
            self.fragment_next += 1
            if idx < 7:
                return
            data = bytes(self.fragment)
            self.fragment_next = 1
        if len(data) != 120:
            return
        frames = []
        for offset in (0, 60):
            block = data[offset:offset+60]
            if block[0] == 1 and block[2] == 0xad:
                frames.append(block[2:59])
        if frames:
            self.packets += 1
            self.last_packet = time.monotonic()
            self.speech_event.set()
            try:
                self.proc.stdin.write(b''.join(frames))
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self.error = '音频解码器已停止：' + str(exc)

    def close(self):
        self.closed = True
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait(timeout=2)
            if self.reader:
                self.reader.join(timeout=2)
            errors = self.proc.stderr.read().decode('utf-8', errors='replace')
            if errors:
                logging.warning('FFmpeg: %s', errors)
        if self.stream:
            self.stream.stop()
            self.stream.close()


async def find_paired_address():
    """从 Windows 系统设备清单找已配对的遥控器地址。

    遥控器休眠时不广播，扫描必然失败；但已配对设备永远在系统清单里。
    返回 (address, name) 或 None。"""
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice  # noqa: F401
        from winrt.windows.devices.enumeration import DeviceInformation
        devs = await DeviceInformation.find_all_async(0)
    except Exception:
        logging.info('系统设备清单不可用', exc_info=True)
        return None
    import re
    pat = re.compile(r'BTHLE#DEV_([0-9A-Fa-f]{12})#', re.I)
    for d in devs:
        name = d.name or ''
        if any(h in name for h in NAME_HINTS):
            m = pat.search(d.id or '')
            if m:
                h = m.group(1)
                address = ':'.join(h[i:i + 2] for i in range(0, 12, 2)).upper()
                return address, name
    return None


class RemoteMic:
    def __init__(self, status=lambda text: None, addr=None, on_record=None):
        self.status = status
        self.cli_addr = addr          # --addr 参数，优先级最高
        self.on_record = on_record    # 测试录音保存后的刷新回调
        self.stop_event = threading.Event()
        self.audio = None
        self.thread = None
        self.connected = False
        self.test_thread = None
        self.test_running = False
        self.test_stop = threading.Event()
        self.hotkey_enabled = load_dictation_enabled()  # 语音键联动开关
        self.hotkey = load_hotkey()                     # 听写快捷键（默认右Alt）
        self._linkage_stop = threading.Event()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self._linkage_stop.set()

    def start_test(self):
        """测试输入：连接成功后可用。倒计时录音 → 保存 wav → 自动回放。"""
        if not self.connected or self.test_running:
            return
        self.test_running = True
        self.test_stop.clear()
        self.test_thread = threading.Thread(target=self._test_run, daemon=True)
        self.test_thread.start()

    def _run(self):
        # WASAPI and WinRT both require COM on the worker thread.
        com_result = ctypes.windll.ole32.CoInitializeEx(None, 0)
        try:
            asyncio.run(self._connect())
        except Exception as exc:
            logging.exception('Remote connection failed')
            self.status('连接失败：' + str(exc) + '\n请按方向键唤醒遥控器，再点“连接”。')
        finally:
            self._linkage_stop.set()
            self.connected = False
            if self.audio:
                self.audio.close()
            self.audio = None
            if com_result in (0, 1):
                ctypes.windll.ole32.CoUninitialize()

    async def _connect(self):
        address = self.cli_addr or load_saved_address()
        if address:
            self.status('正在连接遥控器（%s）……请按方向键唤醒它。' % address)
        else:
            # 休眠的遥控器不广播：先查系统已配对设备清单（不依赖广播）
            found = await find_paired_address()
            if found:
                address, name = found
                self.status('找到已配对的遥控器 %s（%s），正在连接……' % (address, name))
            else:
                # 清单里没有 = 从未配对的新设备：扫描（需先进入配对模式）
                address, name = await scan_remote(self.status)
                if not address:
                    raise RuntimeError('未找到遥控器。旧遥控器请按方向键唤醒后重试；'
                                       '新设备请长按 主页+音量- 进入配对模式（红灯闪烁）后重试。')
        self.audio = AudioPipe().start()
        # 与原版一致的连接写法（真机验证可用）；地址来自 config/系统清单（见上）
        async with BleakClient(BLEDevice(address, 'Phicomm', None), timeout=20,
                               winrt={'use_cached_services': False}) as client:
            save_address(address)  # 首次连接成功，持久化地址
            self.connected = True
            def incoming(char, data):
                self.audio.feed(data)
            await client.start_notify(RX, incoming)
            async def send(data):
                await client.write_gatt_char(TX, data, response=True)
            try:
                await send(bytes([8, 1]) + struct.pack('<I', secrets.randbits(31))
                           + bytes([1, 3]) + bytes(12))
                await asyncio.sleep(.3)
                await send(bytes([8, 12]) + bytes(4) + bytes([1]) + bytes(13))
                await send(bytes([8, 6, 1]) + bytes(17))
                self.status('已连接。请按住遥控器语音键说话。')
                logging.info('READY')
                self._linkage_stop.clear()
                if self.hotkey_enabled:
                    threading.Thread(target=self._linkage_watchdog, daemon=True).start()
                last_packets = 0
                active = False
                while not self.stop_event.is_set() and client.is_connected:
                    if self.audio.error:
                        raise RuntimeError(self.audio.error)
                    if self.audio.proc.poll() is not None:
                        raise RuntimeError('FFmpeg 解码器意外退出。')
                    packets = self.audio.packets
                    if packets > last_packets:
                        self.status('正在传输遥控器语音 · %.1f 秒'
                                    % (packets * .015))
                        last_packets = packets
                        active = True
                    elif active and time.monotonic() - self.audio.last_packet > 1:
                        self.status('已连接，等待语音键。累计传输 %.1f 秒。'
                                    % (packets * .015))
                        active = False
                    await asyncio.sleep(.5)
                if not self.stop_event.is_set():
                    self.status('蓝牙连接已断开，请唤醒遥控器后重新连接。')
                else:
                    self.status('已停止，遥控器语音通道已关闭。')
            finally:
                if client.is_connected:
                    try:
                        await asyncio.wait_for(send(bytes([8, 6, 0]) + bytes(17)), 3)
                        await asyncio.sleep(.1)
                    except Exception:
                        logging.warning('Unable to send stop command', exc_info=True)

    def _linkage_watchdog(self):
        """语音键联动（真按住/真松开）：音频流开始 = 按下听写键；
        静音 0.5 秒 = 松开；断连/停止时兜底松开，避免按键卡住。"""
        holding = False
        try:
            while not self._linkage_stop.is_set():
                audio = self.audio
                if audio is None:
                    break
                got = audio.speech_event.wait(timeout=0.25)
                if got:
                    audio.speech_event.clear()
                    if not holding and self.hotkey_enabled:
                        send_dictation_key(self.hotkey, 'down')
                        holding = True
                elif holding and time.monotonic() - audio.last_packet >= 0.5:
                    send_dictation_key(self.hotkey, 'up')
                    holding = False
        finally:
            if holding:
                send_dictation_key(self.hotkey, 'up')

    def _test_run(self):
        """从 CABLE Output 回环录制 5 秒（不中断主推流），存 wav 后自动回放。"""
        try:
            device = cable_device(capture=True)
            RECORDINGS_DIR.mkdir(exist_ok=True)
            path = RECORDINGS_DIR / ('test-' + time.strftime('%Y%m%d-%H%M%S') + '.wav')
            chunks = []
            # RawInputStream 直接拿字节，不经 numpy；CABLE Output 是回环采集端。
            with sd.RawInputStream(device=device, samplerate=RATE, channels=CHANNELS,
                                   dtype='int16', blocksize=480, latency='low') as stream:
                for remaining in range(TEST_SECONDS, 0, -1):
                    self.status('测试输入：请按住遥控器语音键说话……剩余 %d 秒' % remaining)
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline and not self.test_stop.is_set():
                        data, _overflow = stream.read(480)
                        chunks.append(bytes(data))
            payload = b''.join(chunks)
            seconds = len(payload) / (RATE * CHANNELS * 2)
            with wave.open(str(path), 'wb') as wav:
                wav.setnchannels(CHANNELS)
                wav.setsampwidth(2)
                wav.setframerate(RATE)
                wav.writeframes(payload)
            self.status('测试录音已保存：%s（%.1f 秒），正在回放……' % (path.name, seconds))
            self._playback(path)
            self.status('测试完成：%s\n共 %.1f 秒。' % (path, seconds))
            if self.on_record:
                self.on_record()
        except Exception as exc:
            logging.exception('Test capture failed')
            self.status('测试输入失败：' + str(exc))
        finally:
            self.test_running = False

    @staticmethod
    def _playback(path):
        """用默认扬声器回放（RawOutputStream 写字节等价于 sd.play，且不依赖 numpy）。"""
        with wave.open(str(path), 'rb') as wav:
            rate = wav.getframerate() or RATE
            channels = wav.getnchannels() or CHANNELS
            with sd.RawOutputStream(samplerate=rate, channels=channels, dtype='int16',
                                    blocksize=480, latency='low') as out:
                while True:
                    data = wav.readframes(480)
                    if not data:
                        break
                    out.write(data)


def gui(addr=None):
    import tkinter as tk
    from tkinter import ttk, messagebox
    from ctypes import wintypes
    create_mutex = ctypes.windll.kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    mutex = create_mutex(None, False, 'Local\\PhicommRemoteMic')
    if ctypes.windll.kernel32.GetLastError() == 183:
        messagebox.showinfo('斐讯遥控器麦克风', '工具已经打开，请使用现有窗口。')
        if mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
        return
    handler = RotatingFileHandler(ROOT / 'remote-mic.log', maxBytes=256000,
                                  backupCount=1, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    window = tk.Tk()
    window.title('斐讯遥控器麦克风')
    try:
        window.iconbitmap(str(ROOT / 'icon.ico'))
    except Exception:
        pass
    window.geometry('580x580')
    window.resizable(False, False)
    # ---- 深色工作台主题：纯 tkinter/ttk，零额外依赖 ----
    BG, CARD, FG, DIM, ACCENT, GREEN, ERR = ('#17171a', '#212126', '#e8e8ea',
                                             '#8b8b93', '#f5a623', '#4ec26a', '#e05b5b')
    window.configure(bg=BG)
    style = ttk.Style(window)
    style.theme_use('clam')
    style.configure('.', background=BG, foreground=FG, font=('Microsoft YaHei UI', 10))
    style.configure('TFrame', background=BG)
    style.configure('TLabel', background=BG, foreground=FG)
    style.configure('Dim.TLabel', background=BG, foreground=DIM,
                    font=('Microsoft YaHei UI', 8))
    style.configure('Status.TLabel', background=BG, foreground=DIM,
                    font=('Consolas', 9))
    style.configure('Title.TLabel', background=BG, foreground=FG,
                    font=('Microsoft YaHei UI', 14, 'bold'))
    style.configure('Hint.TLabel', background=BG, foreground=ACCENT,
                    font=('Microsoft YaHei UI', 10, 'bold'))
    style.configure('TButton', background=CARD, foreground=FG, padding=(14, 6))
    style.map('TButton', background=[('active', '#2e2e35'), ('disabled', BG)],
              foreground=[('disabled', '#55555c')])
    style.configure('Accent.TButton', background=ACCENT, foreground=BG,
                    padding=(18, 6), font=('Microsoft YaHei UI', 10, 'bold'))
    style.map('Accent.TButton', background=[('active', '#ffb84d'), ('disabled', '#4a4436')],
              foreground=[('disabled', '#8a8478')])
    style.configure('TCheckbutton', background=BG, foreground=FG)
    style.map('TCheckbutton', background=[('active', BG)], foreground=[('active', ACCENT)])
    style.configure('Horizontal.TSeparator', background='#33333a')
    style.configure('TCombobox', fieldbackground=CARD, background=CARD,
                    foreground=FG, arrowcolor=FG)
    style.configure('Vertical.TScrollbar', background=CARD, troughcolor=BG,
                    arrowcolor=DIM)
    messages = collections.deque()
    record_dirty = [True]
    app = RemoteMic(messages.append, addr=addr,
                    on_record=lambda: record_dirty.__setitem__(0, True))
    ttk.Label(window, text='●  斐讯遥控器 → 无线麦克风',
              style='Title.TLabel').pack(pady=(16, 6))
    label = ttk.Label(window, text='点击连接，按方向键唤醒遥控器。',
                      style='Status.TLabel', wraplength=540, justify='center')
    label.pack(pady=4)
    buttons = ttk.Frame(window)
    buttons.pack(pady=8)
    start = ttk.Button(buttons, text='连接', command=app.start, style='Accent.TButton')
    start.pack(side='left', padx=10)
    ttk.Button(buttons, text='停止', command=app.stop).pack(side='left', padx=10)
    test = ttk.Button(buttons, text='测试输入', command=app.start_test, state='disabled')
    test.pack(side='left', padx=10)
    hotkey_var = tk.BooleanVar(value=app.hotkey_enabled)
    def toggle_hotkey():
        app.hotkey_enabled = hotkey_var.get()
        save_dictation_enabled(app.hotkey_enabled)
    hotkey_bar = ttk.Frame(window)
    hotkey_bar.pack(fill='x', padx=20)
    ttk.Checkbutton(hotkey_bar, text='语音键联动（按住说话自动开关听写）',
                    variable=hotkey_var, command=toggle_hotkey).pack(anchor='w')
    ttk.Label(hotkey_bar,
              text='按住语音键=按住听写键、松手=松开（配合闪电说「按住说话」等长按式听写）；'
                   '若用「按一次开始、再按一次结束」型工具请关闭本开关。',
              style='Dim.TLabel', wraplength=520).pack(anchor='w', padx=(22, 0))
    hk_row = ttk.Frame(window)
    hk_row.pack(fill='x', padx=20, pady=(4, 0))
    ttk.Label(hk_row, text='听写快捷键：').pack(side='left')
    hk_text = tk.StringVar(value=describe_hotkey(app.hotkey))
    ttk.Label(hk_row, textvariable=hk_text, foreground=ACCENT).pack(side='left',
                                                                  padx=(2, 12))
    def open_hotkey_dialog():
        dlg = tk.Toplevel(window)
        dlg.title('设置听写快捷键')
        dlg.geometry('380x170')
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()
        ttk.Label(dlg,
                  text='按下组合键（如 Ctrl+Alt+D、Shift+空格、F9），\n'
                       '或只按一个修饰键（右Alt / 左Ctrl）后松开即可单独使用。\n'
                       '（Win 键暂不支持）',
                  style='Dim.TLabel', wraplength=350,
                  justify='center').pack(pady=(12, 6))
        msg = ttk.Label(dlg, text='等待按键……', style='Status.TLabel')
        msg.pack(pady=4)
        pending_mod = [None]

        def save(hk):
            app.hotkey = hk
            save_hotkey(hk)
            hk_text.set(describe_hotkey(hk))
            dlg.destroy()

        MOD_BY_KEYSYM = {
            'control_l': ('lctrl', 0xA2, 'ctrl'), 'control_r': ('rctrl', 0xA3, 'ctrl'),
            'alt_l': ('lalt', 0xA4, 'alt'), 'alt_r': ('ralt', 0xA5, 'alt'),
            'iso_level3_shift': ('ralt', 0xA5, 'alt'),   # Windows 上右Alt 常报为 AltGr
            'shift_l': ('shift', 0x10, 'shift'), 'shift_r': ('shift', 0x10, 'shift'),
        }

        def on_key(e):
            key = e.keysym.lower()
            if key in MOD_BY_KEYSYM:
                pending_mod[0] = key
                msg.configure(text='松开即确认该键；或继续按一个主键组成组合键',
                              foreground=DIM)
                return
            if key in ('win_l', 'win_r', 'meta_l', 'meta_r', 'caps_lock'):
                msg.configure(text='这个键暂不支持，换一个试试', foreground=ERR)
                return
            mods, mod_vks = [], []
            if e.state & 0x4:
                mods.append('ctrl'); mod_vks.append(0x11)
            if e.state & 0x8:
                mods.append('alt'); mod_vks.append(0x12)
            if e.state & 0x1:
                mods.append('shift'); mod_vks.append(0x10)
            vk = keysym_to_vk(key)
            if vk is None:
                msg.configure(text='这个键暂不支持，换一个试试', foreground=ERR)
                return
            save({'mods': mods, 'mod_vks': mod_vks, 'key': key, 'vk': vk})

        def on_release(e):
            key = e.keysym.lower()
            if pending_mod[0] and key in MOD_BY_KEYSYM:
                name, vk, _kind = MOD_BY_KEYSYM[key]
                pending_mod[0] = None
                save({'mods': [], 'mod_vks': [], 'key': name, 'vk': vk})

        dlg.bind('<KeyPress>', on_key)
        dlg.bind('<KeyRelease>', on_release)
        dlg.focus_force()
    ttk.Button(hk_row, text='自定义…', command=open_hotkey_dialog).pack(side='left')
    ttk.Separator(window, orient='horizontal').pack(fill='x', padx=16, pady=8)
    record_bar = ttk.Frame(window)
    record_bar.pack(fill='x', padx=16)
    ttk.Label(record_bar, text='测试录音（点“测试输入”后生成，可回放核对）',
              font=('Microsoft YaHei UI', 9)).pack(side='left')
    def refresh_recordings():
        listing.delete(0, 'end')
        files = sorted(RECORDINGS_DIR.glob('test-*.wav'), reverse=True) if RECORDINGS_DIR.is_dir() else []
        for f in files:
            seconds = 0.0
            try:
                with wave.open(str(f), 'rb') as wav:
                    seconds = wav.getnframes() / (wav.getframerate() or 1)
            except Exception:
                pass
            listing.insert('end', '%s    %.1f 秒' % (f.name, seconds))
    def open_folder():
        RECORDINGS_DIR.mkdir(exist_ok=True)
        os.startfile(str(RECORDINGS_DIR))
    def open_sound_settings():
        try:
            subprocess.Popen(['control', 'mmsys.cpl,,1'])
        except OSError as exc:
            messagebox.showerror('斐讯遥控器麦克风', '无法打开声音设置：' + str(exc))
    record_buttons = ttk.Frame(record_bar)
    record_buttons.pack(side='right')
    ttk.Button(record_buttons, text='打开录音文件夹', command=open_folder).pack(side='left', padx=4)
    ttk.Button(record_buttons, text='打开声音设置', command=open_sound_settings).pack(side='left', padx=4)
    list_frame = ttk.Frame(window)
    list_frame.pack(fill='both', expand=True, padx=16, pady=6)
    listing = tk.Listbox(list_frame, height=7, font=('Consolas', 9), bg=CARD, fg=FG,
                         selectbackground=ACCENT, selectforeground=BG,
                         highlightthickness=0, relief='flat', borderwidth=0,
                         activestyle='none')
    scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=listing.yview)
    listing.configure(yscrollcommand=scrollbar.set)
    listing.pack(side='left', fill='both', expand=True)
    scrollbar.pack(side='right', fill='y')
    hint = ttk.Label(window, text='在 AI 应用的麦克风设置里选择：CABLE Output',
                     style='Hint.TLabel')
    hint.pack(pady=(4, 2))
    ttk.Label(window, text='音频只在本机转发。关闭窗口会停止传输。',
              style='Dim.TLabel').pack(pady=(0, 10))
    closing = False
    def close():
        nonlocal closing
        closing = True
        app.test_stop.set()
        app.stop()
        label.configure(text='正在关闭语音通道……')
    def poll():
        while messages:
            text = messages.popleft()
            if any(w in text for w in ('已连接', '正在传输')):
                color = GREEN
            elif any(w in text for w in ('失败', '错误', '断开')):
                color = ERR
            else:
                color = DIM
            label.configure(text=text, foreground=color)
        running = bool(app.thread and app.thread.is_alive())
        start.configure(state='disabled' if running or closing else 'normal')
        test.configure(state='normal' if (app.connected and not app.test_running) else 'disabled')
        if record_dirty[0]:
            record_dirty[0] = False
            refresh_recordings()
        if closing and not running:
            window.destroy()
        else:
            window.after(150, poll)
    window.protocol('WM_DELETE_WINDOW', close)
    poll()
    window.mainloop()
    if mutex:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='斐讯遥控器麦克风')
    parser.add_argument('--addr', default=None,
                        help='遥控器蓝牙地址，如 00:E0:4C:2C:04:B6；'
                             '不指定则用已保存地址（config.json），仍没有则自动扫描')
    args = parser.parse_args()
    gui(addr=args.addr)
