"""核心功能模块：屏幕采集、输入控制、会话管理。"""
import io
import json
import time
import struct
from collections import OrderedDict
import secrets

from config import verify_password, USERS

# 以下三个库仅在桌面端使用（截屏、输入控制）。
# 公网中继服务器不需要它们，导入失败时静默跳过。
try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False
    Image = None

try:
    import mss
    HAS_MSS = True
except Exception:
    HAS_MSS = False

try:
    import pyautogui
    pyautogui.FAILSAFE = False   # 禁用鼠标移到角落触发的保护中断
    pyautogui.PAUSE = 0          # 禁用每次操作的自动延时
    HAS_PYAUTOGUI = True
except Exception:
    HAS_PYAUTOGUI = False

try:
    import pyperclip
    HAS_PYPERCLIP = True
except Exception:
    HAS_PYPERCLIP = False

try:
    import soundcard as sc
    import numpy as np
    HAS_SOUNDCARD = True
except Exception:
    HAS_SOUNDCARD = False
    np = None


class ScreenCapture:
    """采集屏幕画面并以 JPEG 字节流返回。"""

    def __init__(self, monitor=1, quality=55, scale=0.75, fps=15):
        if not HAS_MSS:
            raise RuntimeError("未安装 mss，请运行: pip install mss")
        if not HAS_PIL:
            raise RuntimeError("未安装 Pillow，请运行: pip install Pillow")
        self.sct = mss.mss()
        self.monitor_idx = max(1, monitor)
        self.quality = max(10, min(95, quality))
        self.scale = max(0.1, min(1.0, scale))
        self.fps = max(1, min(60, fps))
        self.min_interval = 1.0 / self.fps
        self.last_time = 0.0
        self._last_bytes = None  # 缓存上一帧用于变化检测

    def get_size(self):
        """返回实际屏幕分辨率 (宽, 高)。"""
        m = self.sct.monitors[self.monitor_idx]
        return m['width'], m['height']

    def capture_frame(self):
        """采集一帧。返回 JPEG 字节；若被帧率限制则返回 None。"""
        now = time.monotonic()
        if now - self.last_time < self.min_interval:
            return None
        self.last_time = now
        monitor = self.sct.monitors[self.monitor_idx]
        raw = self.sct.grab(monitor)
        img = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')
        if self.scale != 1.0:
            new_w = max(1, int(img.width * self.scale))
            new_h = max(1, int(img.height * self.scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=self.quality)
        return buf.getvalue()


class DeltaScreenCapture:
    """增量屏幕采集：分块差异检测 + 动态帧率。

    工作原理:
      1. 把屏幕切成 TILE×TILE 的格子
      2. 每帧对比每个块和上一帧对应块，只对变化的块编码为 JPEG
      3. 屏幕无变化时自动降低帧率（idle_fps），有变化时恢复满帧率（fps）
      4. 每隔 keyframe_interval 秒强制发送一次完整关键帧，防止画面残缺

    输出格式:
      capture_delta() 返回 dict:
        - type: "keyframe" 或 "delta"
        - width, height: 缩放后的画面尺寸
        - tiles: [ {x, y, w, h, data: bytes}, ... ]  变化的块
        - bytes_total: 本帧总字节数（用于统计）
      若被帧率限制返回 None。
    """

    # 块大小（缩放后的像素）。64 是性能与效率的平衡点。
    TILE = 64
    # 空闲帧率：屏幕无变化时降到这个帧率
    IDLE_FPS = 2
    # 关键帧间隔（秒）：定期发完整帧
    KEYFRAME_INTERVAL = 5.0
    # 差异阈值：块内像素差异超过此比例才认为变化（0-1）
    DIFF_THRESHOLD = 0.02

    def __init__(self, monitor=1, quality=55, scale=0.75, fps=15):
        if not HAS_MSS:
            raise RuntimeError("未安装 mss，请运行: pip install mss")
        if not HAS_PIL:
            raise RuntimeError("未安装 Pillow，请运行: pip install Pillow")
        self.sct = mss.mss()
        self.monitor_idx = max(1, monitor)
        self.quality = max(10, min(95, quality))
        self.scale = max(0.1, min(1.0, scale))
        self.fps = max(1, min(60, fps))
        self.idle_fps = min(self.IDLE_FPS, self.fps)

        self.min_interval = 1.0 / self.fps
        self.idle_interval = 1.0 / self.idle_fps
        self.last_time = 0.0
        self.last_keyframe_time = 0.0

        # 缩放后的尺寸
        m = self.sct.monitors[self.monitor_idx]
        self.src_w = m['width']
        self.src_h = m['height']
        self.dst_w = max(1, int(self.src_w * self.scale))
        self.dst_h = max(1, int(self.src_h * self.scale))

        # 块网格
        self.cols = (self.dst_w + self.TILE - 1) // self.TILE
        self.rows = (self.dst_h + self.TILE - 1) // self.TILE

        # 上一帧的灰度缩略图（用于快速差异检测）
        # 存储每个块的平均亮度，用 list[int] 节省内存
        self._prev_luma = None  # 长度 = cols * rows
        # 上一帧完整图像（用于切片对比，可选）
        self._prev_img = None

    def reset(self):
        """重置增量状态，下一次采集将发送关键帧。"""
        self._prev_luma = None
        self._prev_img = None
        self.last_keyframe_time = 0.0

    def get_monitors(self):
        """返回所有显示器列表 [{index, width, height}]。"""
        monitors = []
        for i, m in enumerate(self.sct.monitors):
            if i == 0:
                continue  # monitors[0] 是虚拟全屏
            monitors.append({
                "index": i,
                "width": m["width"],
                "height": m["height"],
            })
        return monitors

    def switch_monitor(self, monitor_idx):
        """切换到指定显示器。"""
        if monitor_idx < 1 or monitor_idx >= len(self.sct.monitors):
            return False
        self.monitor_idx = monitor_idx
        m = self.sct.monitors[self.monitor_idx]
        self.src_w = m['width']
        self.src_h = m['height']
        self.dst_w = max(1, int(self.src_w * self.scale))
        self.dst_h = max(1, int(self.src_h * self.scale))
        self.cols = (self.dst_w + self.TILE - 1) // self.TILE
        self.rows = (self.dst_h + self.TILE - 1) // self.TILE
        self.reset()
        return True

    def set_quality(self, quality):
        """动态调整 JPEG 质量。"""
        self.quality = max(10, min(95, quality))

    def set_fps(self, fps):
        """动态调整帧率。"""
        self.fps = max(1, min(60, fps))
        self.min_interval = 1.0 / self.fps
        self.idle_fps = min(self.IDLE_FPS, self.fps)
        self.idle_interval = 1.0 / self.idle_fps

    def get_size(self):
        """返回缩放后的画面尺寸 (宽, 高)。"""
        return self.dst_w, self.dst_h

    def _grab_scaled(self):
        """采集并缩放到目标尺寸，返回 PIL Image (RGB)。"""
        monitor = self.sct.monitors[self.monitor_idx]
        raw = self.sct.grab(monitor)
        img = Image.frombytes('RGB', raw.size, raw.bgra, 'raw', 'BGRX')
        if self.scale != 1.0:
            img = img.resize((self.dst_w, self.dst_h), Image.LANCZOS)
        return img

    def _compute_block_luma(self, img):
        """计算每个块的平均亮度，返回 list[int] (长度 cols*rows)。"""
        # 缩小到 cols×rows 的灰度图，每个像素对应一个块的平均亮度
        thumb = img.resize((self.cols, self.rows), Image.BILINEAR).convert('L')
        return list(thumb.getdata())

    def _encode_tile(self, img, x, y, w, h):
        """裁剪并编码单个块为 JPEG bytes。无效块返回 None。"""
        if w <= 0 or h <= 0 or x < 0 or y < 0:
            return None
        if x + w > img.width or y + h > img.height:
            w = min(w, img.width - x)
            h = min(h, img.height - y)
            if w <= 0 or h <= 0:
                return None
        tile = img.crop((x, y, x + w, y + h))
        if tile.mode != 'RGB':
            tile = tile.convert('RGB')
        buf = io.BytesIO()
        tile.save(buf, format='JPEG', quality=self.quality)
        return buf.getvalue()

    def capture_delta(self):
        """采集一帧增量。返回 dict 或 None（被帧率限制）。"""
        now = time.monotonic()

        # 判断是否需要强制关键帧
        need_keyframe = (self._prev_luma is None or
                         now - self.last_keyframe_time >= self.KEYFRAME_INTERVAL)

        # 动态帧率：有上一帧数据时，根据是否空闲选择间隔
        if self._prev_luma is not None and not need_keyframe:
            interval = self.idle_interval  # 空闲时用低速
        else:
            interval = self.min_interval   # 关键帧用满速

        if now - self.last_time < interval:
            return None
        self.last_time = now

        # 采集
        img = self._grab_scaled()
        cur_luma = self._compute_block_luma(img)

        if need_keyframe:
            # 关键帧：发送所有块
            changed_indices = list(range(self.cols * self.rows))
            frame_type = "keyframe"
            self.last_keyframe_time = now
        else:
            # 增量帧：找出变化的块
            changed_indices = []
            threshold_val = 255 * self.DIFF_THRESHOLD
            for i, (a, b) in enumerate(zip(self._prev_luma, cur_luma)):
                if abs(a - b) > threshold_val:
                    changed_indices.append(i)
            frame_type = "delta"
            # 如果没有任何变化，返回空增量帧（让客户端知道还活着）
            if not changed_indices:
                self._prev_luma = cur_luma
                return {
                    "type": "delta",
                    "width": self.dst_w,
                    "height": self.dst_h,
                    "tiles": [],
                    "bytes_total": 0,
                    "changed": 0,
                }

        # 编码变化的块
        tiles = []
        bytes_total = 0
        for idx in changed_indices:
            col = idx % self.cols
            row = idx // self.cols
            x = col * self.TILE
            y = row * self.TILE
            w = min(self.TILE, self.dst_w - x)
            h = min(self.TILE, self.dst_h - y)
            data = self._encode_tile(img, x, y, w, h)
            if data is None:
                continue
            tiles.append({"x": x, "y": y, "w": w, "h": h, "data": data})
            bytes_total += len(data)

        self._prev_luma = cur_luma

        return {
            "type": frame_type,
            "width": self.dst_w,
            "height": self.dst_h,
            "tiles": tiles,
            "bytes_total": bytes_total,
            "changed": len(changed_indices),
        }


def pack_delta_frame(frame):
    """把 capture_delta() 返回的 dict 打包成单个二进制消息。

    二进制格式:
      [4字节: JSON 头长度 N] [N字节: JSON 头] [拼接的 JPEG 块数据]
    JSON 头: {type, width, height, tiles:[{x,y,w,h,offset,length}], ...}
      每个 tile 的 offset/length 指向后续二进制数据的位置。

    返回 bytes。
    """
    import struct
    header = {
        "type": frame["type"],
        "width": frame["width"],
        "height": frame["height"],
        "changed": frame.get("changed", 0),
    }
    # 构建块元信息和拼接数据
    parts = []
    offset = 0
    tiles_meta = []
    for t in frame["tiles"]:
        data = t["data"]
        tiles_meta.append({
            "x": t["x"], "y": t["y"], "w": t["w"], "h": t["h"],
            "offset": offset, "length": len(data),
        })
        parts.append(data)
        offset += len(data)
    header["tiles"] = tiles_meta

    header_json = json.dumps(header).encode('utf-8')
    # 4 字节大端无符号整数表示头长度
    prefix = struct.pack('>I', len(header_json))
    return prefix + header_json + b''.join(parts)


# 浏览器按键 -> pyautogui 按键名 映射
KEY_MAP = {
    'Enter': 'enter', 'Return': 'enter', 'NumpadEnter': 'enter',
    'Escape': 'escape', 'Esc': 'escape',
    'Backspace': 'backspace',
    'Tab': 'tab',
    ' ': 'space', 'Spacebar': 'space',
    'Delete': 'delete', 'Del': 'delete',
    'Insert': 'insert',
    'Home': 'home', 'End': 'end',
    'PageUp': 'pageup', 'PageDown': 'pagedown',
    'ArrowUp': 'up', 'ArrowDown': 'down',
    'ArrowLeft': 'left', 'ArrowRight': 'right',
    'Shift': 'shift',
    'Control': 'ctrl', 'Ctrl': 'ctrl',
    'Alt': 'alt', 'AltGraph': 'altgr',
    'Meta': 'win', 'OS': 'win',
    'CapsLock': 'capslock',
    'NumLock': 'numlock',
    'ScrollLock': 'scrolllock',
    'PrintScreen': 'printscreen',
    'Pause': 'pause',
    'ContextMenu': 'menu',
}

# Mac → Windows 快捷键映射
# Mac 的 Cmd 键在 Windows 上对应 Win 键，但用户通常期望 Cmd 做 Ctrl 的事
MAC_KEY_REMAP = {
    'Meta': 'ctrl',      # Mac Cmd → Windows Ctrl（复制粘贴等）
    'Control': 'win',    # Mac Ctrl → Windows Win
}

# Mac 快捷键组合映射（cmd+key → ctrl+key）
MAC_SHORTCUT_REMAP = {
    'c': 'ctrl+c', 'v': 'ctrl+v', 'x': 'ctrl+x', 'a': 'ctrl+a',
    'z': 'ctrl+z', 'y': 'ctrl+y', 's': 'ctrl+s', 'p': 'ctrl+p',
    'f': 'ctrl+f', 'w': 'ctrl+w', 't': 'ctrl+t', 'n': 'ctrl+n',
    'l': 'ctrl+l', 'r': 'ctrl+r', 'q': 'ctrl+q',
}


def browser_key_to_pyautogui(key):
    """将浏览器 KeyboardEvent.key 转换为 pyautogui 按键名。"""
    if not key:
        return None
    if key in KEY_MAP:
        return KEY_MAP[key]
    # 单字符按键
    if len(key) == 1:
        return key.lower()
    # F1-F12
    if key.startswith('F') and key[1:].isdigit():
        return key.lower()
    return None


class InputController:
    """通过 pyautogui 执行鼠标和键盘操作。"""

    def __init__(self):
        if not HAS_PYAUTOGUI:
            raise RuntimeError("未安装 pyautogui，请运行: pip install pyautogui")

    def execute(self, command):
        """执行单个命令字典。出错时打印但不抛出。"""
        if not isinstance(command, dict):
            return
        t = command.get('type')
        try:
            if t == 'mouse_move':
                pyautogui.moveTo(int(command['x']), int(command['y']),
                                 _pause=False)
            elif t == 'mouse_down':
                pyautogui.mouseDown(int(command.get('x', 0)),
                                    int(command.get('y', 0)),
                                    button=command.get('button', 'left'))
            elif t == 'mouse_up':
                pyautogui.mouseUp(int(command.get('x', 0)),
                                  int(command.get('y', 0)),
                                  button=command.get('button', 'left'))
            elif t == 'mouse_click':
                pyautogui.click(int(command['x']), int(command['y']),
                                button=command.get('button', 'left'),
                                clicks=int(command.get('clicks', 1)))
            elif t == 'mouse_double':
                pyautogui.doubleClick(int(command['x']), int(command['y']))
            elif t == 'mouse_scroll':
                pyautogui.scroll(int(command.get('delta', 0)),
                                 int(command.get('x', 0)),
                                 int(command.get('y', 0)))
            elif t == 'key_down':
                raw_key = command.get('key')
                k = browser_key_to_pyautogui(raw_key)
                if k:
                    pyautogui.keyDown(k)
            elif t == 'key_up':
                k = browser_key_to_pyautogui(command.get('key'))
                if k:
                    pyautogui.keyUp(k)
            elif t == 'type_text':
                text = command.get('text', '')
                if text:
                    pyautogui.typewrite(text, interval=0)
            elif t == 'set_clipboard':
                # 设置被控端剪贴板内容
                text = command.get('text', '')
                if HAS_PYPERCLIP and text:
                    pyperclip.copy(text)
            elif t == 'get_clipboard':
                # 读取被控端剪贴板内容（返回给控制端）
                if HAS_PYPERCLIP:
                    try:
                        text = pyperclip.paste()
                        return {"type": "clipboard_data", "text": text or ""}
                    except Exception:
                        return {"type": "clipboard_data", "text": ""}
                return {"type": "clipboard_data", "text": ""}
        except Exception as e:
            print(f"[输入执行错误] {e}  命令: {command}")


class AudioCapture:
    """采集系统音频（WASAPI loopback），μ-law 压缩后返回。

    采样率降为 22050Hz 单声道，μ-law 编码（8-bit/样本），每帧约 0.1 秒。
    带宽：~22 KB/s（压缩前 88 KB/s）。
    """

    TARGET_RATE = 22050  # 目标采样率（降采样省带宽）
    FRAME_MS = 100       # 每帧时长（毫秒）

    # μ-law 编码查找表（256 项，0~255）
    _MULAW_TABLE = None

    def __init__(self):
        if not HAS_SOUNDCARD:
            raise RuntimeError("未安装 soundcard，请运行: pip install soundcard")
        self.mic = None
        self.original_rate = 44100
        self._running = False
        self._init_mulaw_table()

    @classmethod
    def _init_mulaw_table(cls):
        """初始化 μ-law 编码查找表。"""
        if cls._MULAW_TABLE is not None:
            return
        import numpy as np
        table = np.zeros(65536, dtype=np.uint8)
        for i in range(-32768, 32768):
            # μ-law 编码算法
            sample = max(-32768, min(32767, i))
            sign = 0x80 if sample < 0 else 0x00
            sample = abs(sample)
            if sample > 32635:
                sample = 32635
            sample = int(32768 * np.log1p(sample / 32768.0) / np.log(256))
            sample = min(127, sample)
            table[i + 32768] = ~(sign | sample) & 0xFF
        cls._MULAW_TABLE = table

    def start(self):
        """打开音频设备，开始采集。"""
        try:
            # 尝试 loopback 模式（新版 soundcard），失败则回退普通模式
            try:
                all_mics = sc.all_microphones(loopback=True)
            except TypeError:
                all_mics = sc.all_microphones()
            if all_mics:
                self.mic = all_mics[0]
            else:
                all_mics = sc.all_microphones()
                if all_mics:
                    self.mic = all_mics[0]
                else:
                    raise RuntimeError("无可用音频设备")
        except Exception as e:
            raise RuntimeError(f"无法打开音频设备: {e}")

        self.mic.__enter__()
        self._running = True

    def stop(self):
        """停止采集。"""
        self._running = False
        if self.mic:
            try:
                self.mic.__exit__(None, None, None)
            except Exception:
                pass
            self.mic = None

    def capture_chunk(self):
        """采集一个音频块，μ-law 压缩后返回 bytes（mono，22050Hz，8-bit）。

        返回 None 表示未运行或出错。
        """
        if not self._running or not self.mic:
            return None
        try:
            num_samples = int(self.original_rate * self.FRAME_MS / 1000)
            data = self.mic.record(numframes=num_samples)
            if data.ndim > 1:
                data = data.mean(axis=1)
            # 降采样
            if self.original_rate != self.TARGET_RATE:
                ratio = self.original_rate / self.TARGET_RATE
                indices = np.arange(0, len(data), ratio).astype(int)
                data = data[indices]
            # float32 → int16
            data = np.clip(data, -1.0, 1.0)
            data = (data * 32767).astype(np.int16)
            # μ-law 编码（查表）
            indices = data.astype(np.int32) + 32768
            encoded = AudioCapture._MULAW_TABLE[indices]
            return encoded.tobytes()
        except Exception as e:
            print(f"[音频采集错误] {e}")
            return None

    @staticmethod
    def is_available():
        """检查音频采集是否可用。"""
        return HAS_SOUNDCARD


class SessionManager:
    """内存中的会话令牌存储，带过期清理和登录失败限制。"""

    MAX_FAILED_ATTEMPTS = 5       # 最大失败次数
    LOCKOUT_DURATION = 300        # 锁定时长（秒）

    def __init__(self, expiry=3600 * 8):
        self.expiry = expiry
        self.sessions = OrderedDict()  # token -> {username, expires}
        self._failed_attempts = {}     # ip -> {count, lock_until}

    def create(self, username):
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {
            'username': username,
            'expires': time.time() + self.expiry,
        }
        self._cleanup()
        return token

    def valid(self, token):
        s = self.sessions.get(token)
        if not s:
            return False
        if time.time() > s['expires']:
            self.sessions.pop(token, None)
            return False
        return True

    def username(self, token):
        s = self.sessions.get(token)
        return s['username'] if s else None

    def destroy(self, token):
        self.sessions.pop(token, None)

    def _cleanup(self):
        now = time.time()
        for t in [t for t, s in self.sessions.items() if now > s['expires']]:
            self.sessions.pop(t, None)

    def check_rate_limit(self, client_ip):
        """检查是否被锁定。返回 True 表示可以尝试。"""
        info = self._failed_attempts.get(client_ip)
        if not info:
            return True
        if info.get('lock_until') and time.time() < info['lock_until']:
            return False
        return True

    def record_failed_attempt(self, client_ip):
        """记录一次失败尝试，达到上限则锁定。"""
        info = self._failed_attempts.get(client_ip, {'count': 0})
        info['count'] += 1
        if info['count'] >= self.MAX_FAILED_ATTEMPTS:
            info['lock_until'] = time.time() + self.LOCKOUT_DURATION
        self._failed_attempts[client_ip] = info

    def record_success(self, client_ip):
        """登录成功，清除失败记录。"""
        self._failed_attempts.pop(client_ip, None)

    def get_remaining_lock(self, client_ip):
        """获取剩余锁定时间（秒）。0 表示未锁定。"""
        info = self._failed_attempts.get(client_ip)
        if not info or not info.get('lock_until'):
            return 0
        remaining = info['lock_until'] - time.time()
        return max(0, int(remaining))


def authenticate(username, password):
    """校验用户名密码。成功返回 True。"""
    stored = USERS.get(username)
    if not stored:
        return False
    return verify_password(password, stored)
