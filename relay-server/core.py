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
except ImportError:
    HAS_PIL = False
    Image = None

try:
    import mss
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

try:
    import pyautogui
    pyautogui.FAILSAFE = False   # 禁用鼠标移到角落触发的保护中断
    pyautogui.PAUSE = 0          # 禁用每次操作的自动延时
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False


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
                k = browser_key_to_pyautogui(command.get('key'))
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
        except Exception as e:
            print(f"[输入执行错误] {e}  命令: {command}")


class SessionManager:
    """内存中的会话令牌存储，带过期清理。"""

    def __init__(self, expiry=3600 * 8):
        self.expiry = expiry
        self.sessions = OrderedDict()  # token -> {username, expires}

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


def authenticate(username, password):
    """校验用户名密码。成功返回 True。"""
    stored = USERS.get(username)
    if not stored:
        return False
    return verify_password(password, stored)
