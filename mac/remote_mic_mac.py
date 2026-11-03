#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""斐讯遥控器 -> macOS 虚拟麦克风（BlackHole 2ch）

协议层与 Windows 版完全一致：BLE 服务 0x1920，写 2B01 / 通知 2B00，
握手三连 -> mSBC 音频帧 -> ffmpeg 解码 -> 推入 BlackHole。

用法：
  ./venv/bin/python remote_mic_mac.py                 # 图形界面
  ./venv/bin/python remote_mic_mac.py --selftest 15   # 无界面自检（录 15 秒，验证整条链路）
"""
import argparse
import json
import os
import secrets
import struct
import subprocess
import sys
import threading
import time
import uuid as uuid_mod
import wave
from pathlib import Path

import objc
import CoreBluetooth as CB
import sounddevice as sd
from Foundation import NSDate, NSObject, NSRunLoop

try:
    import libdispatch
    BLE_QUEUE = libdispatch.dispatch_queue_create(b"phicomm.remote.mic.ble", None)
except Exception:                                     # 没有 libdispatch 就退回主线程模式
    BLE_QUEUE = None

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:                                    # 退回到系统 ffmpeg
    FFMPEG = "ffmpeg"

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
REC_DIR = ROOT / "recordings"
LOG_PATH = ROOT / "remote-mic.log"

VOICE_SVC = "00001920-0000-1000-8000-00805f9b34fb"
RX_UUID = "00002b00-0000-1000-8000-00805f9b34fb"
TX_UUID = "00002b01-0000-1000-8000-00805f9b34fb"
RATE = 48000
CHANNELS = 2
VOICE_RMS = 300          # 解码后 PCM 的 RMS 阈值：高于它算「有声音」，用来判断说完

# macOS 虚拟键码（供听写键选择）
HOTKEY_CHOICES = [
    ("右 Shift", 60), ("左 Shift", 56), ("右 Option", 61), ("左 Option", 58),
    ("右 Control", 62), ("左 Control", 59), ("F13", 105), ("F14", 107),
    ("F15", 113), ("F16", 106), ("F17", 64), ("F18", 79),
]
DEFAULT_HOTKEY = 60


def log(*args):
    line = " ".join(str(a) for a in args)
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(time.strftime("[%H:%M:%S] ") + line + "\n")
    except OSError:
        pass


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(**kw):
    cfg = load_config()
    cfg.update(kw)
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def blackhole_index():
    for i, d in enumerate(sd.query_devices()):
        if "BlackHole 2ch" in d["name"]:
            return i
    raise RuntimeError("未找到 BlackHole 2ch，请先安装 BlackHole")


def norm(u):
    return str(u).lower().split("-")[0]


# ---------------------------------------------------------------- 音频管线
class AudioPipe:
    """mSBC -> ffmpeg 解码 -> BlackHole 输出，并记录最近活动时间供联动使用。"""

    def __init__(self):
        self.proc = None
        self.stream = None
        self.reader = None
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.frames = 0
        self.last_packet = 0.0
        self.last_voice = 0.0        # 最近一次「确实有声音」的时间（RMS 超阈值）
        self.speech_event = threading.Event()
        self.error = None
        self.fragment = bytearray(120)
        self.fragment_next = 1
        self.rec_buffer = None        # 非 None 时把解码后的 PCM 存起来

    def start(self):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-probesize", "32",
               "-analyzeduration", "0", "-f", "sbc", "-i", "pipe:0",
               "-acodec", "pcm_s16le", "-ar", str(RATE), "-ac", str(CHANNELS),
               "-f", "s16le", "-flush_packets", "1", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        idx = blackhole_index()
        self.stream = sd.RawOutputStream(device=idx, samplerate=RATE, channels=CHANNELS,
                                         dtype="int16", blocksize=480, latency="low",
                                         callback=self._output)
        self.stream.start()
        log("[audio] 已推流到 BlackHole 2ch")
        return self

    def _read(self):
        try:
            while True:
                data = self.proc.stdout.read(1920)
                if not data:
                    break
                try:                        # 有声音才更新，静音包不算
                    import audioop
                    if audioop.rms(data, 2) > VOICE_RMS:
                        self.last_voice = time.monotonic()
                except Exception:
                    pass
                with self.lock:
                    self.buffer.extend(data)
                    if self.rec_buffer is not None:
                        self.rec_buffer.append(data)
                    excess = len(self.buffer) - RATE * CHANNELS * 2 // 2
                    if excess > 0:
                        excess -= excess % 4
                        del self.buffer[:excess]
        except Exception as exc:
            self.error = str(exc)

    def _output(self, outdata, frames, timing, status):
        with self.lock:
            count = min(len(outdata), len(self.buffer))
            outdata[:count] = self.buffer[:count]
            del self.buffer[:count]
        if count < len(outdata):
            outdata[count:] = bytes(len(outdata) - count)

    def feed(self, data):
        data = bytes(data)
        if len(data) == 20 and (data[0] & 31) == 20:        # 分片重组
            idx = data[1]
            if idx == 1:
                self.fragment_next = 1
            if idx != self.fragment_next or not 1 <= idx <= 7:
                self.fragment_next = 1
                return
            count = 12 if idx == 7 else 18
            self.fragment[(idx - 1) * 18:(idx - 1) * 18 + count] = data[2:2 + count]
            self.fragment_next += 1
            if idx < 7:
                return
            data = bytes(self.fragment)
            self.fragment_next = 1
        if len(data) != 120:
            return
        sbc = bytearray()
        for off in (0, 60):
            blk = data[off:off + 60]
            if blk[0] == 1 and blk[2] == 0xAD:
                sbc += blk[2:59]
        if not sbc:
            return
        self.frames += 1
        self.last_packet = time.monotonic()
        self.speech_event.set()
        try:
            self.proc.stdin.write(bytes(sbc))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.error = "解码器停止：%s" % exc

    def close(self):
        try:
            if self.proc:
                self.proc.stdin.close()
                self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass


# ---------------------------------------------------------------- BLE
class BleDelegate(NSObject):
    """所有回调都包了护栏：Python 异常一旦穿过 ObjC 会直接杀掉进程。"""

    def initWithOwner_(self, owner):
        self = objc.super(BleDelegate, self).init()
        self.owner = owner
        return self

    @objc.python_method
    def _safe(self, name, *args):
        try:
            return getattr(self.owner, name)(*args)
        except Exception as exc:
            log("[delegate] %s 出错: %r" % (name, exc))

    def centralManagerDidUpdateState_(self, cm):
        self._safe("on_central_state", cm, cm.state())

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(self, cm, p, adv, rssi):
        self._safe("on_discovered", p, adv, rssi)

    def centralManager_didConnectPeripheral_(self, cm, p):
        self._safe("on_connected", p)

    def centralManager_didFailToConnectPeripheral_error_(self, cm, p, err):
        self._safe("on_fail", err)

    def centralManager_didDisconnectPeripheral_error_(self, cm, p, err):
        self._safe("on_disconnected", err)

    def peripheral_didDiscoverServices_(self, p, err):
        self._safe("on_services", p, err)

    def peripheral_didDiscoverCharacteristicsForService_error_(self, p, svc, err):
        self._safe("on_characteristics", p, svc, err)

    def peripheral_didUpdateNotificationStateForCharacteristic_error_(self, p, ch, err):
        self._safe("on_notify_state", p, ch, err)

    def peripheral_didUpdateValueForCharacteristic_error_(self, p, ch, err):
        self._safe("on_value", ch, err)

    def peripheral_didWriteValueForCharacteristic_error_(self, p, ch, err):
        if err is not None:
            log("[ble] write error:", err)


class RemoteMic:
    """BLE + 音频 + 联动 的总控（不含界面）。"""

    def __init__(self, status=lambda t: None, want_audio=True):
        self.status = status
        self.want_audio = want_audio
        self.cfg = load_config()
        self.audio = None
        self.manager = None
        self.peripheral = None
        self.rx = None
        self.tx = None
        self.notify_ready = False
        self.stage = 0
        self.t_stage = 0.0
        self.thread = None
        self.stop_event = threading.Event()
        self.connected = False
        self.hotkey = int(self.cfg.get("hotkey_keycode", DEFAULT_HOTKEY))
        self.dictation_enabled = bool(self.cfg.get("dictation_enabled", True))
        self.linkage_thread = None
        self.linkage_stop = threading.Event()
        self.linkage_gen = 0
        self.last_uuid = self.cfg.get("peripheral_uuid")

    # ---- 生命周期 ----
    def start(self):
        """创建 CBCentralManager。优先用 GCD 队列（回调不占主线程，音频更稳）。"""
        if self.manager is not None:
            return
        self.stop_event.clear()
        self.stage = 0
        self.notify_ready = False
        self.delegate = BleDelegate.alloc().initWithOwner_(self)
        self.manager = CB.CBCentralManager.alloc().initWithDelegate_queue_(
            self.delegate, BLE_QUEUE)
        if BLE_QUEUE is not None:
            self.ticker = threading.Thread(target=self._ticker_loop, daemon=True)
            self.ticker.start()
        log("[ble] central manager 已创建（队列=%s）" % ("GCD" if BLE_QUEUE else "主线程"))

    def _ticker_loop(self):
        """GCD 模式下的节奏驱动：只管握手时序，不再依赖界面泵 RunLoop。"""
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                log("[tick] 出错:", repr(exc))
            time.sleep(0.02)

    def pump(self, slot=0.02):
        """退回主线程模式时才需要外部反复调用。"""
        if BLE_QUEUE is not None:
            return
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(slot))
        self._tick()

    def stop(self):
        self.stop_event.set()
        self.linkage_stop.set()
        th = self.linkage_thread
        if th is not None and th.is_alive():
            th.join(timeout=1.5)
        self.linkage_thread = None
        release_hotkey(self.hotkey, owner="link")     # 停就保证松手，别把 Shift 卡住
        try:
            if self.manager is not None:
                self.manager.stopScan()
                if self.peripheral is not None:
                    self.manager.cancelPeripheralConnection_(self.peripheral)
        except Exception:
            pass
        self.peripheral = None
        audio, self.audio = self.audio, None
        if audio is not None:
            audio.close()
        self.manager = None
        if self.peripheral is not None:
            try:
                self.peripheral.setNotifyValue_forCharacteristic_(False, self.rx)
            except Exception:
                pass

    # ---- CoreBluetooth 回调 ----
    def on_central_state(self, cm, state):
        if state != CB.CBManagerStatePoweredOn:
            self.status("蓝牙未就绪（状态 %d），请确认系统蓝牙已打开" % state)
            return
        cands = list(cm.retrieveConnectedPeripheralsWithServices_(
            [CB.CBUUID.UUIDWithString_(VOICE_SVC)]))
        if cands:
            log("[ble] 找到已连接的遥控器:", cands[0].name())
            self._use(cands[0])
            return
        if self.last_uuid:
            try:
                known = list(cm.retrievePeripheralsWithIdentifiers_([uuid_mod.UUID(self.last_uuid)]))
            except Exception:
                known = []
            if known:
                log("[ble] 用上次的标识连接:", known[0].name())
                self._use(known[0])
                return
        self.status("正在搜索遥控器……请按一下遥控器任意键唤醒")
        log("[ble] 开始扫描（需遥控器广播）")
        cm.scanForPeripheralsWithServices_([CB.CBUUID.UUIDWithString_(VOICE_SVC)], None)

    def on_discovered(self, p, adv, rssi):
        name = (p.name() or "").replace("\u200b", "")
        log("[ble] 发现:", name, str(p.identifier()), rssi)
        if "斐讯" in name or "Phicomm" in name or not name:
            self.manager.stopScan()
            self._use(p)

    def _use(self, p):
        self.peripheral = p
        p.setDelegate_(self.delegate)
        self.manager.connectPeripheral_options_(p, None)

    def on_connected(self, p):
        self.connected = True
        self.last_uuid = str(p.identifier())
        save_config(peripheral_uuid=self.last_uuid)
        log("[ble] 已连接:", p.name())
        if self.want_audio and self.audio is None:
            try:
                self.audio = AudioPipe().start()
            except Exception as exc:
                self.status("音频输出启动失败：%s" % exc)
                log("[audio] 启动失败:", exc)
        p.discoverServices_(None)

    def on_fail(self, err):
        self.status("连接失败：%s" % err)
        log("[ble] 连接失败:", err)

    def on_disconnected(self, err):
        self.connected = False
        self.status("蓝牙已断开，请唤醒遥控器后重新连接")
        log("[ble] 断开:", err)

    def on_services(self, p, err):
        for s in (p.services() or []):
            if norm(s.UUID()) == "1920":
                p.discoverCharacteristics_forService_(None, s)
                return
        self.status("遥控器没有语音服务，可能型号不符")

    def on_characteristics(self, p, svc, err):
        for ch in (svc.characteristics() or []):
            if norm(ch.UUID()) == "2b00":
                self.rx = ch
            elif norm(ch.UUID()) == "2b01":
                self.tx = ch
        if self.rx is not None and self.tx is not None and not self.notify_ready:
            p.setNotifyValue_forCharacteristic_(True, self.rx)

    def on_notify_state(self, p, ch, err):
        if ch.isNotifying() and not self.notify_ready:
            self.notify_ready = True
            self.stage = 1
            self.t_stage = time.time()
            log("[ble] 通知已开启，准备握手")

    def on_value(self, ch, err):
        v = ch.value()
        if v is None or self.audio is None:
            return
        try:
            self.audio.feed(bytes(v))
        except Exception as exc:
            log("[audio] feed 出错:", repr(exc))

    # ---- 节奏控制 ----
    def _tick(self):
        now = time.time()
        if self.stage == 1 and now - self.t_stage > 0.5:
            if self.want_audio:
                if self.audio is None:
                    self.status("音频管线不可用，无法开流")
                    self.stage = -1
                    return
                self._send(bytes([8, 1]) + struct.pack("<I", secrets.randbits(31))
                           + bytes([1, 3]) + bytes(12), "hs1")
                self.stage = 2
                self.t_stage = now
                self.status("已连接。请按住遥控器语音键说话")
                self._start_linkage()
        elif self.stage == 2 and now - self.t_stage > 0.35:
            self._send(bytes([8, 12]) + bytes(4) + bytes([1]) + bytes(13), "hs2")
            self._send(bytes([8, 6, 1]) + bytes(17), "voice-open")
            self.stage = 3
            log("[ble] 已开流")

    def _send(self, payload, tag):
        if self.tx is None or self.peripheral is None:
            return
        try:
            self.peripheral.writeValue_forCharacteristic_type_(
                payload, self.tx, CB.CBCharacteristicWriteWithResponse)
            log("[ble] TX", tag)
        except Exception as exc:
            log("[ble] TX", tag, "失败:", exc)

    # ---- 听写键联动 ----
    def _start_linkage(self):
        if not self.dictation_enabled:
            log("[link] 联动未勾选，不启动")
            return
        th = self.linkage_thread
        if th is not None and th.is_alive():
            log("[link] 联动线程已在运行，不重复启动")
            return
        self.linkage_stop.clear()
        self.linkage_gen += 1
        gen = self.linkage_gen
        self.linkage_thread = threading.Thread(target=self._linkage_loop, args=(gen,), daemon=True)
        self.linkage_thread.start()
        log("[link] 联动线程启动 gen=%d" % gen)

    def _linkage_loop(self, gen):
        speaking = False
        log("[link] 循环开始 gen=%d" % gen)
        while not self.linkage_stop.is_set() and gen == self.linkage_gen:
            audio = self.audio
            if audio is None:
                if speaking:                                    # 音频管线没了：立刻松手
                    release_hotkey(self.hotkey, owner="link")
                    speaking = False
                time.sleep(0.2)
                continue
            got = audio.speech_event.wait(timeout=0.15)
            if got:
                audio.speech_event.clear()
                if not speaking:
                    if press_hotkey(self.hotkey, owner="link"):   # 开口就按住听写键
                        speaking = True
            if speaking:
                now = time.monotonic()
                # 三层兜底，任一层满足就松手：绝不能把 Shift 卡在按下状态
                idle_for = now - audio.last_packet                  # 连音频包都不来了
                quiet_for = now - max(audio.last_voice, audio.last_packet)  # 有包但静音
                if idle_for >= 0.5 or quiet_for >= 0.7:
                    release_hotkey(self.hotkey, owner="link")     # 说完了
                    speaking = False
        if speaking:
            release_hotkey(self.hotkey, owner="link")
        log("[link] 循环结束 gen=%d" % gen)

    # ---- 测试录音：从 BlackHole 回环录 ----
    def record(self, seconds=5, path=None):
        """录音：直接从解码后的 PCM 抓（不经过 BlackHole 回环，最稳）。"""
        if self.audio is None:
            raise RuntimeError("还没连接，无法录音")
        self.audio.rec_buffer = []
        try:
            end = time.time() + seconds
            while time.time() < end:
                self.pump(0.01) if BLE_QUEUE is None else time.sleep(0.01)
            chunks = list(self.audio.rec_buffer)
        finally:
            self.audio.rec_buffer = None
        REC_DIR.mkdir(exist_ok=True)
        path = path or (REC_DIR / time.strftime("test-%Y%m%d-%H%M%S.wav"))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(chunks))
        return path


# 修饰键必须连 mask 一起发：闪电说这类工具监听的是 FlagsChanged，
# 只发 keyDown/keyUp 而不带 NX_DEVICExxxKEYMASK，会被当成"没按修饰键"。
# (kCGEventFlagMaskShift, NX_DEVICE*KEYMASK)
MOD_FLAGS = {
    56: (0x00020000, 0x00000002),   # 左 Shift
    60: (0x00020000, 0x00000004),   # 右 Shift
    58: (0x00080000, 0x00000020),   # 左 Option
    61: (0x00080000, 0x00000040),   # 右 Option
}

_hk_lock = threading.Lock()
_hk_down = False          # 全局唯一的「按键是否处于按下状态」
_hk_owner = None          # 谁按下的：link=联动，test=试一下


def _post_hotkey(keycode, down):
    """合成修饰键状态变化。显式发 flagsChanged，和真键盘一致：
    只发 keyDown 时，某些情况下系统不会转成 flagsChanged，监听 FlagsChanged 的工具就收不到。"""
    import Quartz
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    ev = Quartz.CGEventCreateKeyboardEvent(src, keycode, down)
    mask = 0
    if down and keycode in MOD_FLAGS:
        base, device = MOD_FLAGS[keycode]
        mask = base | device
    Quartz.CGEventSetFlags(ev, mask)
    try:
        Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
    except Exception:
        pass
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    return mask


def _shift_stuck():
    """系统层面是否还残留着 Shift 按下状态（上一个进程没松手就会这样）。"""
    try:
        import Quartz
        f = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState)
        return bool((f >> 17) % 2)
    except Exception:
        return False


def force_clear_modifier(keycode):
    """把可能卡住的修饰键复位：发一个 flags=0 的松开。"""
    try:
        _post_hotkey(keycode, False)
        log("[hotkey] 已清理残留修饰键状态")
    except Exception as exc:
        log("[hotkey] 清理失败:", exc)


def press_hotkey(keycode, owner="link"):
    """按下并保持。已经按住时不重复发——重复的 keyDown 会让听写工具状态错乱。"""
    global _hk_down, _hk_owner
    with _hk_lock:
        if _hk_down:
            log("[hotkey] 已按住中，跳过重复按下 owner=%s" % _hk_owner)
            return False
        if _shift_stuck():
            # 上一次（别的进程或崩溃）没把 Shift 松开 -> 现在再按不会有状态变化，工具收不到
            log("[hotkey] 检测到系统里 Shift 还按着，先复位")
            force_clear_modifier(keycode)
            time.sleep(0.05)
        try:
            mask = _post_hotkey(keycode, True)
        except Exception as exc:
            log("[hotkey] 按下失败:", exc)
            return False
        _hk_down, _hk_owner = True, owner
        log("[hotkey] press", keycode, "flags=0x%08x" % mask, "owner=" + owner)
        return True


def release_hotkey(keycode, owner="link"):
    """松开。只允许「按下它的那个人」松手，防止两处互相打断。"""
    global _hk_down, _hk_owner
    with _hk_lock:
        if not _hk_down:
            return False
        if _hk_owner != owner:
            log("[hotkey] 跳过松开：当前按住的是", _hk_owner)
            return False
        try:
            _post_hotkey(keycode, False)
        except Exception as exc:
            log("[hotkey] 松开失败:", exc)
        _hk_down, _hk_owner = False, None
        log("[hotkey] release", keycode)
        return True


def hotkey_is_down():
    return _hk_down


def tap_hotkey(keycode, hold=0.35):
    """自检用：按下-保持-松开。联动正按住时直接跳过，绝不打断正在进行的录音。"""
    if not press_hotkey(keycode, owner="test"):
        log("[hotkey] 联动正在按住按键，测试跳过")
        return
    time.sleep(hold)
    release_hotkey(keycode, owner="test")


# ---------------------------------------------------------------- 按键守卫
# 遥控器的语音键在系统里同时是一只 HID 键盘键：按住时会以 ~12 次/秒自动重复 F5。
# 这些 keyDown 会被听写工具当成"你在打字"，于是它把合成出来的 RShift 判为卡键并清掉，
# 长按录音于是只维持几百毫秒。这里在会话层把这些事件吞掉（只在遥控器连着的时候有意义）。
SWALLOW_KEYCODES = {96}          # 96 = F5，斐讯遥控器语音键的键码
_guard_stat = {"dropped": 0}


def start_key_guard(enabled=True):
    if not enabled:
        log("[guard] 未启用按键守卫")
        return None
    try:
        import Quartz
    except Exception as exc:
        log("[guard] 没有 Quartz，跳过:", exc)
        return None

    def cb(proxy, etype, event, refcon):
        try:
            if etype == Quartz.kCGEventTapDisabledByTimeout:
                Quartz.CGEventTapEnable(refcon, True)
                return event
            if etype in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                kc = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                pid = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID)
                if kc in SWALLOW_KEYCODES and pid == 0:      # pid=0 才是真硬件
                    _guard_stat["dropped"] += 1
                    n = _guard_stat["dropped"]
                    if n in (1, 30) or n % 100 == 0:
                        log("[guard] 已拦截遥控器语音键 F5 连发 ×%d" % n)
                    return None
        except Exception as exc:
            log("[guard] 回调异常:", exc)
        return event

    mask = (1 << Quartz.kCGEventKeyDown) | (1 << Quartz.kCGEventKeyUp)
    tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                  Quartz.kCGEventTapOptionDefault, mask, cb, None)
    if not tap:
        log("[guard] 创建事件过滤器失败（可能缺辅助功能权限），F5 连发不会被拦截")
        return None

    def run():
        src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        Quartz.CFRunLoopRun()

    threading.Thread(target=run, daemon=True).start()
    log("[guard] 已启用：拦截遥控器语音键的 F5 连发")
    return tap


# ---------------------------------------------------------------- 自检
def selftest(seconds, prompt=False):
    if prompt:
        subprocess.run(["osascript", "-e",
                        'display dialog "自检：点「开始」后立刻按住遥控器语音键说话 %d 秒" '
                        'buttons {"开始"} default button 1 with icon note' % seconds],
                       capture_output=True)
    mic = RemoteMic(status=lambda t: print("[status]", t, flush=True))
    mic.dictation_enabled = False      # 自检期间不合成任何按键
    mic.start()
    deadline = time.time() + 25
    while time.time() < deadline and not (mic.audio and mic.stage >= 2):
        mic.pump(0.05) if BLE_QUEUE is None else time.sleep(0.05)
    if not mic.audio:
        print("FAILED: 20 秒内没有连上/开流", flush=True)
        mic.stop()
        return 1
    out = REC_DIR / "selftest.wav"
    REC_DIR.mkdir(exist_ok=True)
    path = mic.record(seconds, path=out)
    frames = mic.audio.frames if mic.audio else 0
    mic.stop()
    with wave.open(str(path), "rb") as w:
        n, sr = w.getnframes(), w.getframerate()
        raw = w.readframes(n)
    import struct as st
    samples = st.unpack("<%dh" % (len(raw) // 2), raw) or [0]
    peak = max(abs(s) for s in samples)
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
    print(f"RESULT 录音={path.name} 时长={n / sr:.1f}s frames={frames} peak={peak} rms={rms:.0f}", flush=True)
    subprocess.Popen(["afplay", str(path)])
    return 0 if peak > 100 else 2


# ---------------------------------------------------------------- GUI
def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    BG, CARD, FG, DIM, ACCENT, GREEN, ERR = ("#17171a", "#212126", "#e8e8ea",
                                             "#8b8b93", "#f5a623", "#4ec26a", "#e05b5b")
    win = tk.Tk()
    win.title("斐讯遥控器麦克风 (Mac)")
    win.geometry("600x600")
    win.configure(bg=BG)
    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=FG, font=("PingFang SC", 12))
    for name, kw in {
        "TFrame": dict(background=BG), "TLabel": dict(background=BG, foreground=FG),
        "Title.TLabel": dict(background=BG, foreground=FG, font=("PingFang SC", 17, "bold")),
        "Status.TLabel": dict(background=BG, foreground=DIM, font=("Menlo", 11)),
        "Dim.TLabel": dict(background=BG, foreground=DIM, font=("PingFang SC", 10)),
        "Hint.TLabel": dict(background=BG, foreground=ACCENT, font=("PingFang SC", 12, "bold")),
        "TButton": dict(background=CARD, foreground=FG, padding=(14, 7)),
        "Accent.TButton": dict(background=ACCENT, foreground=BG, padding=(18, 7),
                               font=("PingFang SC", 12, "bold")),
        "TCheckbutton": dict(background=BG, foreground=FG),
        "Horizontal.TSeparator": dict(background="#33333a"),
        "TCombobox": dict(fieldbackground=CARD, background=CARD, foreground=FG),
        "Vertical.TScrollbar": dict(background=CARD, troughcolor=BG, arrowcolor=DIM),
    }.items():
        style.configure(name, **kw)
    style.map("TButton", background=[("active", "#2e2e35")])
    style.map("Accent.TButton", background=[("active", "#ffb84d")])

    messages = []

    def status(text):
        messages.append(text)

    mic = RemoteMic(status=status)
    start_key_guard(bool(mic.cfg.get("guard_remote_f5", True)))

    ttk.Label(win, text="斐讯遥控器 → 无线麦克风", style="Title.TLabel").pack(pady=(18, 8))
    label = ttk.Label(win, text="点「连接」，然后按遥控器任意键唤醒它。",
                      style="Status.TLabel", wraplength=560, justify="center")
    label.pack(pady=4)
    meter = ttk.Label(win, text="音频帧 0  ·  0.0 秒", style="Status.TLabel")
    meter.pack(pady=(0, 2))
    link_lbl = ttk.Label(win, text="联动：待机", style="Status.TLabel")
    link_lbl.pack(pady=(0, 2))

    btns = ttk.Frame(win)
    btns.pack(pady=10)
    start_btn = ttk.Button(btns, text="连接", style="Accent.TButton", command=mic.start)
    start_btn.pack(side="left", padx=8)
    ttk.Button(btns, text="停止", command=mic.stop).pack(side="left", padx=8)
    test_btn = ttk.Button(btns, text="测试输入", state="disabled")
    test_btn.pack(side="left", padx=8)

    hk_var = tk.BooleanVar(value=mic.dictation_enabled)

    def toggle_dictation():
        mic.dictation_enabled = hk_var.get()
        save_config(dictation_enabled=mic.dictation_enabled)

    ttk.Checkbutton(win, text="语音键联动（按下语音键=开始听写，松开=结束）",
                    variable=hk_var, command=toggle_dictation).pack(anchor="w", padx=24)
    ttk.Label(win, text="听写工具里把语音键设成下面这个键即可（闪电说：长按说话模式）；联动会在你开口时按住它、说完松开。",
              style="Dim.TLabel", wraplength=560).pack(anchor="w", padx=44)

    row = ttk.Frame(win)
    row.pack(fill="x", padx=24, pady=(6, 0))
    ttk.Label(row, text="听写快捷键：").pack(side="left")
    labels = [c[0] for c in HOTKEY_CHOICES]
    cur = next((lab for lab, code in HOTKEY_CHOICES if code == mic.hotkey), labels[0])
    hk_combo = ttk.Combobox(row, values=labels, state="readonly", width=14)
    hk_combo.set(cur)
    hk_combo.pack(side="left", padx=(4, 10))

    def on_hotkey_change(_evt=None):
        lab = hk_combo.get()
        for name, code in HOTKEY_CHOICES:
            if name == lab:
                mic.hotkey = code
                save_config(hotkey_keycode=code)
                return

    hk_combo.bind("<<ComboboxSelected>>", on_hotkey_change)

    def test_tap():
        tap_hotkey(mic.hotkey, hold=1.2)       # 长按 1.2 秒，能验出「长按触发」型工具是否响应
        status("已发送一次听写键（若听写没反应：系统设置→隐私与安全→辅助功能，允许「终端」）")

    ttk.Button(row, text="试一下", command=test_tap).pack(side="left")

    ttk.Separator(win, orient="horizontal").pack(fill="x", padx=20, pady=10)

    ttk.Label(win, text="测试录音（点「测试输入」录 5 秒，可从 BlackHole 回环听到）",
              style="Dim.TLabel").pack(anchor="w", padx=24)
    listing = tk.Listbox(win, height=7, bg=CARD, fg=FG, selectbackground=ACCENT,
                         selectforeground=BG, highlightthickness=0, relief="flat",
                         font=("Menlo", 11))
    listing.pack(fill="both", expand=True, padx=24, pady=6)

    def refresh_list():
        listing.delete(0, "end")
        if REC_DIR.is_dir():
            for f in sorted(REC_DIR.glob("*.wav"), reverse=True):
                try:
                    with wave.open(str(f), "rb") as w:
                        secs = w.getnframes() / (w.getframerate() or 1)
                except Exception:
                    secs = 0
                listing.insert("end", "%s   %.1fs" % (f.name, secs))

    def open_folder():
        REC_DIR.mkdir(exist_ok=True)
        subprocess.Popen(["open", str(REC_DIR)])

    def open_sound():
        subprocess.Popen(["open", "x-apple.systempreferences:com.apple.Sound-Settings.extension"])

    def play_last():
        files = sorted(REC_DIR.glob("*.wav"), reverse=True)
        if files:
            subprocess.Popen(["afplay", str(files[0])])

    row2 = ttk.Frame(win)
    row2.pack(fill="x", padx=24)
    ttk.Button(row2, text="打开录音文件夹", command=open_folder).pack(side="left", padx=4)
    ttk.Button(row2, text="播放最新一条", command=play_last).pack(side="left", padx=4)
    ttk.Button(row2, text="打开声音设置", command=open_sound).pack(side="left", padx=4)

    ttk.Label(win, text="在听写/输入法里把麦克风选为 BlackHole 2ch", style="Hint.TLabel").pack(pady=(10, 2))
    ttk.Label(win, text="音频只在本机流转；关闭窗口即停止推流。", style="Dim.TLabel").pack(pady=(0, 10))

    recording = {"busy": False}

    def do_test():
        if recording["busy"] or not mic.connected:
            return
        recording["busy"] = True
        status("测试录音：5 秒倒计时，请按住遥控器语音键说话……")

        def run():
            try:
                path = mic.record(5)
                status("已保存 %s" % path.name)
                subprocess.Popen(["afplay", str(path)])
            except Exception as exc:
                status("测试录音失败：%s" % exc)
            finally:
                recording["busy"] = False
                refresh_list()

        threading.Thread(target=run, daemon=True).start()

    test_btn.configure(command=do_test)

    debug_hook = os.environ.get("N1_DEBUG_HOOK") == "1"
    debug_path = os.environ.get("N1_DEBUG_CMD", "/tmp/n1_linkage_cmd")
    debug_seen = [""]

    def _debug_poll():
        if not debug_hook:
            return
        try:
            cmd = open(debug_path, encoding="utf-8").read().strip()
        except Exception:
            return
        if not cmd or cmd == debug_seen[0]:
            return
        debug_seen[0] = cmd
        log("[debug] 收到指令", cmd)
        if cmd == "press":
            press_hotkey(mic.hotkey, owner="debug")
        elif cmd == "release":
            release_hotkey(mic.hotkey, owner="debug")
        elif cmd == "clear":
            force_clear_modifier(mic.hotkey)

    def poll():
        _debug_poll()
        down = hotkey_is_down()
        link_lbl.configure(text=("联动：已按住听写键" if down else "联动：待机"),
                           foreground=(ACCENT if down else DIM))
        while messages:
            text = messages.pop(0)
            color = GREEN if ("已连接" in text or "已开流" in text) else (
                ERR if ("失败" in text or "断开" in text or "错误" in text) else DIM)
            label.configure(text=text, foreground=color)
        running = mic.manager is not None
        live = mic.audio.frames if mic.audio else 0
        meter.configure(text="音频帧 %d  ·  %.1f 秒" % (live, live * 0.015))
        start_btn.configure(state="disabled" if running else "normal")
        test_btn.configure(state="normal" if mic.connected else "disabled")
        win.after(150, poll)

    refresh_list()
    poll()
    win.mainloop()
    mic.stop()


def main():
    ap = argparse.ArgumentParser(description="斐讯遥控器 → Mac 虚拟麦克风")
    ap.add_argument("--selftest", type=int, metavar="SECONDS", help="无界面自检：录 N 秒验证链路")
    ap.add_argument("--prompt", action="store_true", help="自检前弹出系统对话框提示")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest(args.selftest, prompt=args.prompt))
    gui()


if __name__ == "__main__":
    main()
