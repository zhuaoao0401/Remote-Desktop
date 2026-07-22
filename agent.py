"""桌面代理：在被控电脑上运行，连接到中继服务器，采集屏幕并执行输入。

被控端启动后会开启一个本地 Web 配置页（默认 http://localhost:8799），
可在页面上设置主机名、中继地址，并一键启动/停止连接。

也支持命令行直接启动（跳过配置页）:
    python agent.py --relay ws://1.2.3.4:9090 --hostname 我的电脑

选项:
    --relay URL        中继服务器地址, 例如 ws://1.2.3.4:9090
    --token TOKEN      代理认证令牌 (默认使用 config.py 中的 AGENT_TOKEN)
    --hostname NAME    主机名称 (显示在控制端的主机列表中)
    --no-gui           跳过配置页，直接用命令行参数启动
"""
import asyncio
import json
import argparse
import os
import sys
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import websockets

from core import DeltaScreenCapture, InputController, pack_delta_frame, AudioCapture
import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class AgentState:
    """Agent 运行状态管理。"""

    CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".remote_desktop_config.json")
    DEFAULT_RELAY_URL = "ws://43.163.239.11:9090"

    def __init__(self):
        self.hostname = ""
        self.relay_url = self.DEFAULT_RELAY_URL
        self.token = config.AGENT_TOKEN
        self.desktop_id = ""
        self.status = "idle"
        self.message = "未启动"
        self._thread = None
        self._stop_flag = False
        self.last_connected = None
        self._ws = None
        # 启动时加载持久化配置（覆盖默认值）
        self.load_config()

    def load_config(self):
        """从本地文件加载配置，覆盖默认值。"""
        try:
            import json
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                if cfg.get('hostname'):
                    self.hostname = cfg['hostname']
                if cfg.get('relay_url'):
                    self.relay_url = cfg['relay_url']
                if cfg.get('token'):
                    self.token = cfg['token']
                # 加载持久化密码
                pass_file = os.path.join(os.path.expanduser("~"), ".remote_desktop_password.json")
                if os.path.exists(pass_file):
                    import json as _json
                    with open(pass_file, 'r', encoding='utf-8') as pf:
                        pass_cfg = _json.load(pf)
                    if pass_cfg.get("admin"):
                        config.USERS["admin"] = pass_cfg["admin"]
        except Exception:
            pass

    def save_config(self):
        """保存配置到本地文件。"""
        try:
            import json
            cfg = {
                'hostname': self.hostname,
                'relay_url': self.relay_url,
                'token': self.token if self.token != config.AGENT_TOKEN else '',
            }
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def to_dict(self):
        return {
            "hostname": self.hostname,
            "relay_url": self.relay_url,
            "status": self.status,
            "message": self.message,
            "last_connected": self.last_connected,
        }


state = AgentState()


# ===========================================================================
# 本地配置 Web 服务
# ===========================================================================

app = FastAPI(title="远程桌面 - 被控端配置")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


CONFIG_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>远程桌面 - 统一管理</title>
<link rel="stylesheet" href="/static/style.css">
<style>
.tab-bar { display: flex; gap: 0; margin-bottom: 20px; border-bottom: 2px solid var(--border); }
.tab-btn { padding: 12px 24px; font-size: 14px; font-weight: 600; cursor: pointer;
  border: none; background: none; color: var(--muted); border-bottom: 2px solid transparent;
  margin-bottom: -2px; transition: all .2s; }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-btn:hover { color: var(--text); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.control-section { margin-top: 16px; }
.relay-input-group { display: flex; gap: 8px; margin-bottom: 12px; }
.relay-input-group input { flex: 1; }
.host-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
.host-card { padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px;
  cursor: pointer; transition: all .2s; background: var(--surface); }
.host-card:hover { border-color: var(--accent); transform: translateY(-1px); }
.host-card .h-name { font-weight: 600; font-size: 14px; }
.host-card .h-status { font-size: 12px; margin-top: 4px; }
.host-card .h-status.online { color: var(--green); }
.host-card .h-status.offline { color: var(--red); }
.ctrl-iframe { width: 100%; height: 500px; border: 1px solid var(--border);
  border-radius: 10px; margin-top: 12px; }
</style>
</head>
<body class="login-body">
<div class="login-card" style="width:560px;">
  <div class="logo">🖥️</div>
  <h1>远程桌面</h1>

  <div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('agent')">🖥️ 被控端</button>
    <button class="tab-btn" onclick="switchTab('control')">🎮 控制端</button>
  </div>

  <!-- 被控端 -->
  <div id="tab-agent" class="tab-panel active">
    <p class="subtitle">设置主机名并连接到中继服务器，让其他设备可以远程控制本机</p>
    <form id="cfgForm">
      <div class="form-group">
        <label for="hostname">主机名称（控制端会看到此名称）</label>
        <input type="text" id="hostname" required placeholder="例如：我的办公电脑"
               value="" maxlength="32">
      </div>
      <div class="form-group">
      <label for="relay">中继服务器地址</label>
      <input type="text" id="relay" placeholder="ws://43.163.239.11:9090"
             value="ws://43.163.239.11:9090">
    </div>
      <div class="form-group">
        <label for="token">代理令牌</label>
        <input type="text" id="token" placeholder="留空使用默认令牌"
               value="">
      </div>
      <div id="status-box" class="status-box idle">状态：未启动</div>
      <button type="submit" id="startBtn" class="btn-primary">启动被控</button>
      <button type="button" id="stopBtn" class="btn-small"
              style="width:100%;margin-top:10px;display:none;">停止被控</button>
      <label style="display:flex;align-items:center;gap:8px;margin-top:12px;font-size:13px;color:var(--muted);">
        <input type="checkbox" id="autostartChk" onchange="toggleAutostart()">
        开机自动启动（Windows 注册表）
      </label>
      <hr style="border:none;border-top:1px solid var(--border);margin:12px 0;">
      <details style="font-size:13px;">
        <summary style="cursor:pointer;color:var(--muted);">修改登录密码</summary>
        <div style="margin-top:8px;">
          <input type="password" id="oldPass" placeholder="旧密码" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);margin-bottom:6px;">
          <input type="password" id="newPass" placeholder="新密码（至少4位）" style="width:100%;padding:8px 10px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);margin-bottom:6px;">
          <button type="button" class="btn-small" style="width:100%;" onclick="changePassword()">修改密码</button>
        </div>
      </details>
    </form>
  </div>

  <!-- 控制端 -->
  <div id="tab-control" class="tab-panel">
    <p class="subtitle">选择一台在线主机进行远程控制</p>

    <!-- 中继服务器地址 + 刷新 -->
    <div class="form-group">
      <label for="ctrlRelay">中继服务器地址</label>
      <div class="relay-input-group">
        <input type="text" id="ctrlRelay" placeholder="http://43.163.239.11:9090"
               value="http://43.163.239.11:9090">
        <button class="btn-primary" style="white-space:nowrap;" onclick="loadHostList()">刷新主机</button>
      </div>
    </div>

    <!-- 主机列表 -->
    <div id="ctrlHosts">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <span style="font-weight:600;font-size:14px;">📡 在线主机 (<span id="hostCount">0</span>)</span>
        <button class="btn-small" onclick="loadHostList()">🔄 刷新</button>
      </div>
      <div id="hostGrid" class="host-grid">
        <p style="color:var(--muted);text-align:center;padding:20px;">点击上方"刷新主机"查看在线设备</p>
      </div>
    </div>
  </div>

  <p class="hint">配置页地址：http://localhost:8799</p>
</div>
<script>
const $ = id => document.getElementById(id);
let polling = null;
let ctrlToken = '';
let ctrlRelayUrl = '';

// ---- 标签切换 ----
function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  event.target.classList.add('active');
  $('tab-' + name).classList.add('active');
}

// ---- 被控端状态 ----
function setStatus(s, msg) {
  const box = $('status-box');
  box.className = 'status-box ' + s;
  box.textContent = '状态：' + msg;
}

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    if (d.hostname) $('hostname').value = d.hostname;
    if (d.relay_url) $('relay').value = d.relay_url;
    setStatus(d.status, d.message);
    const running = d.status === 'connected' || d.status === 'connecting';
    $('startBtn').style.display = running ? 'none' : 'block';
    $('stopBtn').style.display = running ? 'block' : 'none';
  } catch(e) {}
}

$('cfgForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const hostname = $('hostname').value.trim();
  const relay = $('relay').value.trim() || 'ws://43.163.239.11:9090';
  const token = $('token').value.trim();
  if (!hostname) { alert('请输入主机名'); return; }
  $('startBtn').disabled = true;
  $('startBtn').textContent = '启动中...';
  try {
    const r = await fetch('/api/start', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({hostname, relay, token})
    });
    const d = await r.json();
    if (!d.ok) { setStatus('error', d.error || '启动失败'); }
  } catch(e) { setStatus('error', e.message); }
  $('startBtn').disabled = false;
  $('startBtn').textContent = '启动连接';
  refresh();
});

$('stopBtn').addEventListener('click', async () => {
  await fetch('/api/stop', {method:'POST'});
  refresh();
});

refresh();
polling = setInterval(refresh, 2000);

// ---- 开机启动 ----
async function checkAutostart() {
  try {
    const r = await fetch('/api/autostart');
    const d = await r.json();
    $('autostartChk').checked = d.enabled;
  } catch(e) {}
}
async function toggleAutostart() {
  const enable = $('autostartChk').checked;
  try {
    await fetch('/api/autostart', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({enable})
    });
  } catch(e) {}
}
checkAutostart();

// ---- 修改密码 ----
async function changePassword() {
  const oldP = $('oldPass').value;
  const newP = $('newPass').value;
  if (!newP || newP.length < 4) { alert('新密码至少4位'); return; }
  try {
    const r = await fetch('/api/change_password', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({old_password: oldP, new_password: newP})
    });
    const d = await r.json();
    if (d.ok) { alert('密码修改成功'); $('oldPass').value=''; $('newPass').value=''; }
    else { alert(d.error || '修改失败'); }
  } catch(e) { alert('网络错误'); }
}

// ---- 控制端：中继主机列表 ----
let relayToken = '';
let relayBaseUrl = '';

async function loadHostList() {
  const relayUrl = $('ctrlRelay').value.trim().replace(/\/$/, '');
  if (!relayUrl) { alert('请输入中继服务器地址'); return; }
  relayBaseUrl = relayUrl;
  $('hostGrid').innerHTML = '<p style="color:var(--muted);text-align:center;padding:20px;">加载中...</p>';

  // 先自动登录获取 token
  try {
    const lr = await fetch('/api/relay_login', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({relay_url: relayUrl})
    });
    const ldata = await lr.json();
    if (ldata.ok) { relayToken = ldata.token; }
  } catch(e) {}

  // 通过本地服务器代理请求主机列表
  try {
    const r = await fetch('/api/relay_hosts?relay_url=' + encodeURIComponent(relayUrl));
    const data = await r.json();
    if (data.error) {
      $('hostGrid').innerHTML = '<p style="color:var(--red);text-align:center;padding:20px;">' + data.error + '</p>';
      return;
    }
    const hosts = data.hosts || [];
    $('hostCount').textContent = hosts.length;
    if (hosts.length === 0) {
      $('hostGrid').innerHTML = '<p style="color:var(--muted);text-align:center;padding:20px;">暂无在线主机，请先在"被控端"标签中启动被控</p>';
      return;
    }
    $('hostGrid').innerHTML = hosts.map(h => {
      const statusCls = h.online ? 'online' : 'offline';
      const statusText = h.online ? '● 在线' : '● 离线';
      const initial = (h.hostname || '?')[0].toUpperCase();
      return `<div class="host-card" onclick="connectHost('${h.desktop_id || h.hostname}', '${h.hostname}')">
        <div style="display:flex;align-items:center;gap:10px;">
          <div class="host-avatar" style="width:36px;height:36px;font-size:16px;border-radius:8px;">${initial}</div>
          <div style="flex:1;">
            <div class="h-name">${h.hostname || '未知'}</div>
            <div class="h-status ${statusCls}">${statusText} ${h.since || ''}</div>
          </div>
          <div style="font-size:20px;">${h.online ? '🎮' : '💤'}</div>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    $('hostGrid').innerHTML = '<p style="color:var(--red);text-align:center;padding:20px;">连接失败: ' + e.message + '</p>';
  }
}

function connectHost(desktopId, hostname) {
  // 在新窗口中打开控制页面
  const did = encodeURIComponent(desktopId);
  const url = relayBaseUrl + '/desktop?token=' + relayToken + '&desktop_id=' + did;
  window.open(url, '_blank');
}

// 自动填充被控端的中继地址到控制端
$('ctrlRelay').addEventListener('focus', function() {
  if (!this.value && $('relay').value) {
    this.value = $('relay').value.replace('ws://', 'http://').replace('wss://', 'https://');
  }
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def cfg_page():
    return HTMLResponse(CONFIG_PAGE_HTML)


@app.get("/api/state")
async def get_state():
    return state.to_dict()


@app.post("/api/start")
async def api_start(request: Request):
    body = await request.json()
    hostname = (body.get("hostname") or "").strip()
    relay = (body.get("relay") or "").strip() or AgentState.DEFAULT_RELAY_URL
    token = (body.get("token") or "").strip()
    if not hostname:
        return JSONResponse({"ok": False, "error": "请输入主机名"}, status_code=400)
    if state._thread is not None:
        return JSONResponse({"ok": False, "error": "已在运行，请先停止"}, status_code=400)

    state.hostname = hostname
    state.relay_url = relay
    if token:
        state.token = token
    # 用主机名生成 desktop_id（做 URL 安全编码）
    state.desktop_id = hostname
    state.status = "connecting"
    state.message = "正在连接中继..."
    # 保存配置到本地文件
    state.save_config()

    # 在独立线程中启动 agent，避免阻塞 uvicorn 事件循环
    import threading
    state._thread = threading.Thread(target=run_agent_thread, daemon=True)
    state._thread.start()
    return JSONResponse({"ok": True})


@app.post("/api/stop")
async def api_stop():
    state._stop_flag = True
    # 关闭 WebSocket 连接
    if state._ws:
        try:
            await state._ws.close()
        except Exception:
            pass
    # 等待线程结束
    if state._thread and state._thread.is_alive():
        state._thread.join(timeout=3)
    state._thread = None
    state._stop_flag = False
    state.status = "idle"
    state.message = "已停止"
    state._ws = None
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# 自动开机启动管理
# ---------------------------------------------------------------------------

if sys.platform == 'win32':
    import winreg

def get_startup_command():
    """获取开机启动的命令行。"""
    if sys.platform == 'win32':
        exe = sys.executable
        script = os.path.abspath(__file__)
        if exe.endswith('python.exe'):
            return f'"{exe}" "{script}" --no-gui'
        else:
            # 打包成 exe 的情况
            return f'"{exe}" --no-gui'
    return None

def is_autostart_enabled():
    """检查是否已设置开机启动。"""
    if sys.platform != 'win32':
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                             0, winreg.KEY_READ)
        winreg.QueryValueEx(key, "RemoteDesktop")
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False

def enable_autostart():
    """设置开机启动。"""
    if sys.platform != 'win32':
        return False, "仅支持 Windows"
    try:
        cmd = get_startup_command()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "RemoteDesktop", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        return True, "已设置开机启动"
    except Exception as e:
        return False, str(e)

def disable_autostart():
    """取消开机启动。"""
    if sys.platform != 'win32':
        return False, "仅支持 Windows"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                             0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, "RemoteDesktop")
        winreg.CloseKey(key)
        return True, "已取消开机启动"
    except FileNotFoundError:
        return True, "未设置开机启动"
    except Exception as e:
        return False, str(e)


@app.get("/api/autostart")
async def get_autostart():
    return JSONResponse({"enabled": is_autostart_enabled()})

@app.post("/api/autostart")
async def toggle_autostart(request: Request):
    body = await request.json()
    enable = body.get("enable", False)
    if enable:
        ok, msg = enable_autostart()
    else:
        ok, msg = disable_autostart()
    return JSONResponse({"ok": ok, "message": msg, "enabled": is_autostart_enabled()})


# ---------------------------------------------------------------------------
# 密码修改
# ---------------------------------------------------------------------------
@app.post("/api/change_password")
async def change_password(request: Request):
    body = await request.json()
    old_pass = body.get("old_password", "")
    new_pass = body.get("new_password", "")
    if not new_pass or len(new_pass) < 4:
        return JSONResponse({"ok": False, "error": "新密码至少4个字符"}, status_code=400)
    # 验证旧密码
    from config import USERS, hash_password
    import config as cfg
    if not cfg.verify_password(old_pass, USERS.get("admin", "")):
        return JSONResponse({"ok": False, "error": "旧密码错误"}, status_code=401)
    # 更新内存中的密码
    USERS["admin"] = hash_password(new_pass)
    # 保存到配置文件
    import json
    pass_file = os.path.join(os.path.expanduser("~"), ".remote_desktop_password.json")
    try:
        with open(pass_file, 'w', encoding='utf-8') as f:
            json.dump({"admin": USERS["admin"]}, f)
    except Exception:
        pass
    return JSONResponse({"ok": True, "message": "密码修改成功"})


# ---------------------------------------------------------------------------
# 中继服务器代理接口（避免浏览器跨域问题）
# ---------------------------------------------------------------------------

# 缓存中继服务器登录 token
_relay_tokens = {}  # relay_url -> token

@app.post("/api/relay_login")
async def relay_login(request: Request):
    """代理登录中继服务器，返回 token。"""
    import urllib.request
    body = await request.json()
    relay_url = body.get("relay_url", "").rstrip("/")
    username = body.get("username", "admin")
    password = body.get("password", "admin123")
    if not relay_url:
        return JSONResponse({"ok": False, "error": "请输入中继服务器地址"}, status_code=400)
    try:
        data = urllib.parse.urlencode({"username": username, "password": password}).encode()
        req = urllib.request.Request(relay_url + "/login", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json as _json
            result = _json.loads(resp.read().decode())
            if result.get("ok") and result.get("token"):
                _relay_tokens[relay_url] = result["token"]
                return JSONResponse({"ok": True, "token": result["token"]})
            return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"连接失败: {e}"}, status_code=500)

@app.get("/api/relay_hosts")
async def relay_hosts(relay_url: str = "", refresh_login: str = ""):
    """代理获取中继服务器的主机列表。"""
    import urllib.request
    relay_url = relay_url.rstrip("/")
    if not relay_url:
        # 用配置中的默认地址
        relay_url = state.relay_url.replace("ws://", "http://").replace("wss://", "https://")
    if not relay_url:
        return JSONResponse({"ok": False, "error": "未配置中继服务器地址"}, status_code=400)
    token = _relay_tokens.get(relay_url, "")
    try:
        url = relay_url + "/api/hosts"
        if token:
            url += f"?token={token}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json as _json
            result = _json.loads(resp.read().decode())
            return JSONResponse(result)
    except urllib.error.HTTPError as e:
        if e.code == 401 or e.code == 403:
            return JSONResponse({"ok": False, "error": "需要登录", "need_login": True})
        return JSONResponse({"ok": False, "error": f"服务器错误: {e.code}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"连接失败: {e}"}, status_code=500)


# ===========================================================================
# Agent 连接逻辑
# ===========================================================================

def run_agent_thread():
    """Agent 主循环（独立线程）：连接中继并传输屏幕。

    在独立线程中运行，使用自己的 asyncio 事件循环，
    避免阻塞 uvicorn 的 Web 服务。
    """
    # 在线程内创建独立的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_agent_async_main())
    except Exception as e:
        print(f"[Agent线程异常] {e}")
    finally:
        loop.close()


async def _agent_async_main():
    """Agent 异步主逻辑。"""
    try:
        screen = DeltaScreenCapture(monitor=config.SCREEN_MONITOR,
                                    quality=config.SCREEN_QUALITY,
                                    scale=config.SCREEN_SCALE,
                                    fps=config.SCREEN_FPS)
        inp = InputController()

        import urllib.parse
        did = urllib.parse.quote(state.desktop_id, safe='')
        url = f"{state.relay_url}/ws/agent?token={state.token}&desktop_id={did}&hostname={urllib.parse.quote(state.hostname, safe='')}"

        retry_delay = 2
        while True:
            if state._stop_flag:
                break
            try:
                state.status = "connecting"
                state.message = "正在连接中继..."
                async with websockets.connect(url, max_size=None,
                                              ping_interval=20,
                                              open_timeout=15) as ws:
                    state._ws = ws
                    state.status = "connected"
                    retry_delay = 2
                    state.message = "已连接，等待控制端接入"
                    state.last_connected = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[已连接] 主机={state.hostname} 中继={state.relay_url}")

                    w, h = screen.get_size()
                    monitors = screen.get_monitors()
                    audio_cap = AudioCapture.is_available()
                    await ws.send(json.dumps({
                        "type": "init", "width": w, "height": h,
                        "fps": config.SCREEN_FPS, "encoding": "delta",
                        "hostname": state.hostname,
                        "monitors": monitors,
                        "clipboard_supported": True,
                        "audio_supported": audio_cap,
                        "audio_rate": 22050,
                    }))

                    loop = asyncio.get_event_loop()

                    # 默认不传画面，等控制端接入后再传
                    streaming_active = False

                    # 文件传输状态
                    file_recv = {"fp": None, "name": "", "size": 0, "received": 0}

                    # 音频采集器
                    audio = None
                    audio_supported = AudioCapture.is_available()

                    async def capture_loop():
                        nonlocal streaming_active, audio
                        while True:
                            if not streaming_active:
                                # 没人看，不传画面，低频等待
                                await asyncio.sleep(0.5)
                                continue
                            frame = await loop.run_in_executor(
                                None, screen.capture_delta)
                            if frame:
                                try:
                                    await ws.send(pack_delta_frame(frame))
                                except Exception:
                                    break
                            await asyncio.sleep(0.005)

                    async def audio_loop():
                        """音频采集循环：采集系统声音并发送。"""
                        nonlocal streaming_active, audio
                        while True:
                            if not streaming_active or not audio:
                                await asyncio.sleep(0.5)
                                continue
                            chunk = await loop.run_in_executor(None, audio.capture_chunk)
                            if chunk:
                                try:
                                    # 音频消息：以 4 字节标记 AUDI 开头，区分画面数据
                                    await ws.send(b'AUDI' + chunk)
                                except Exception:
                                    pass
                            await asyncio.sleep(0.02)

                    async def command_loop():
                        nonlocal streaming_active
                        while True:
                            try:
                                msg = await ws.recv()
                            except Exception:
                                break
                            if isinstance(msg, bytes):
                                # 二进制消息：文件传输数据块
                                if file_recv["fp"]:
                                    file_recv["fp"].write(msg)
                                    file_recv["received"] += len(msg)
                                    # 发送进度
                                    pct = int(file_recv["received"] / max(1, file_recv["size"]) * 100)
                                    await ws.send(json.dumps({
                                        "type": "file_progress",
                                        "name": file_recv["name"],
                                        "received": file_recv["received"],
                                        "size": file_recv["size"],
                                        "percent": min(100, pct),
                                    }))
                                    if file_recv["received"] >= file_recv["size"]:
                                        file_recv["fp"].close()
                                        save_path = file_recv.get("path", "")
                                        print(f"[文件接收完成] {file_recv['name']} → {save_path}")
                                        await ws.send(json.dumps({
                                            "type": "file_done",
                                            "name": file_recv["name"],
                                            "path": save_path,
                                        }))
                                        file_recv["fp"] = None
                                        file_recv["name"] = ""
                                        file_recv["received"] = 0
                                continue
                            try:
                                cmd = json.loads(msg)
                                cmd_type = cmd.get("type")
                                if cmd_type == "init":
                                    continue
                                elif cmd_type == "ping":
                                    await ws.send(json.dumps({"type": "pong", "t": cmd.get("t", 0)}))
                                    continue
                                elif cmd_type == "start_streaming":
                                    if not streaming_active:
                                        streaming_active = True
                                        screen.reset()
                                        # 启动音频采集
                                        if audio_supported and not audio:
                                            try:
                                                audio = AudioCapture()
                                                audio.start()
                                                print(f"[音频] 已开始采集系统声音")
                                            except Exception as e:
                                                print(f"[音频] 采集失败: {e}")
                                                audio = None
                                        state.message = "已连接，正在传输画面"
                                        print(f"[开始传输] 控制端已接入")
                                    continue
                                elif cmd_type == "stop_streaming":
                                    if streaming_active:
                                        streaming_active = False
                                        # 停止音频采集
                                        if audio:
                                            audio.stop()
                                            audio = None
                                        state.message = "已连接，等待控制端接入"
                                        print(f"[停止传输] 控制端已断开")
                                    continue
                                elif cmd_type == "switch_monitor":
                                    idx = cmd.get("index", 1)
                                    if screen.switch_monitor(idx):
                                        w2, h2 = screen.get_size()
                                        await ws.send(json.dumps({
                                            "type": "init", "width": w2, "height": h2,
                                            "fps": config.SCREEN_FPS, "encoding": "delta",
                                            "hostname": state.hostname,
                                            "monitors": screen.get_monitors(),
                                        }))
                                    continue
                                elif cmd_type == "set_quality":
                                    screen.set_quality(int(cmd.get("quality", 55)))
                                    screen.reset()
                                    continue
                                elif cmd_type == "set_fps":
                                    screen.set_fps(int(cmd.get("fps", 15)))
                                    continue
                                elif cmd_type == "get_clipboard":
                                    result = await loop.run_in_executor(None, inp.execute, cmd)
                                    if result:
                                        await ws.send(json.dumps(result))
                                    continue
                                elif cmd_type == "file_start":
                                    import os
                                    fname = cmd.get("name", "upload")
                                    fsize = cmd.get("size", 0)
                                    offset = cmd.get("offset", 0)
                                    save_dir = os.path.join(os.path.expanduser("~"), "Desktop")
                                    os.makedirs(save_dir, exist_ok=True)
                                    save_path = os.path.join(save_dir, fname)
                                    # 如果有 offset，尝试续传
                                    if offset > 0 and os.path.exists(save_path) and os.path.getsize(save_path) >= offset:
                                        file_recv["fp"] = open(save_path, "ab")
                                        file_recv["received"] = offset
                                        print(f"[文件续传] {fname} 从 {offset} 字节继续")
                                    else:
                                        # 避免重名
                                        base, ext = os.path.splitext(fname)
                                        counter = 1
                                        while os.path.exists(save_path):
                                            save_path = os.path.join(save_dir, f"{base}_{counter}{ext}")
                                            counter += 1
                                        file_recv["fp"] = open(save_path, "wb")
                                        file_recv["received"] = 0
                                    file_recv["name"] = os.path.basename(save_path)
                                    file_recv["size"] = fsize
                                    file_recv["path"] = save_path
                                    print(f"[文件接收开始] {fname} ({fsize} bytes) → {save_path}")
                                    continue
                                elif cmd_type == "file_cancel":
                                    if file_recv["fp"]:
                                        file_recv["fp"].close()
                                        file_recv["fp"] = None
                                    file_recv["name"] = ""
                                    file_recv["received"] = 0
                                    continue
                                # 普通输入命令
                                await loop.run_in_executor(None, inp.execute, cmd)
                            except Exception as e:
                                print(f"[命令错误] {e}")

                    async def heartbeat_loop():
                        """定时发送心跳，防止连接超时断开。"""
                        while True:
                            await asyncio.sleep(15)
                            try:
                                await ws.send(json.dumps({"type": "heartbeat"}))
                            except Exception:
                                break

                    await asyncio.gather(capture_loop(), audio_loop(), command_loop(), heartbeat_loop())
            except asyncio.CancelledError:
                raise
            except (websockets.exceptions.ConnectionClosed,
                    ConnectionRefusedError, OSError) as e:
                state.status = "connecting"
                state.message = f"连接断开，{retry_delay}秒后重连: {e}"
                print(f"[连接断开] {e}  {retry_delay}秒后重连...")
            except Exception as e:
                state.status = "connecting"
                state.message = f"异常，{retry_delay}秒后重连: {e}"
                print(f"[异常] {e}  {retry_delay}秒后重连...")
            # 指数退避等待重连
            for _ in range(retry_delay):
                if state._stop_flag:
                    break
                await asyncio.sleep(1)
            retry_delay = min(retry_delay * 2, 30)
    finally:
        state.status = "idle"
        state.message = "已停止"
        state._ws = None
        print("[已停止]")


# ===========================================================================
# 启动
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="远程桌面 - 桌面代理(被控端)")
    parser.add_argument("--relay", default="",
                        help="中继服务器地址 (不填则用配置页设置)")
    parser.add_argument("--token", default=config.AGENT_TOKEN,
                        help="代理认证令牌")
    parser.add_argument("--hostname", default="",
                        help="主机名称 (不填则用配置页设置)")
    parser.add_argument("--port", type=int, default=8799,
                        help="本地配置页端口 (默认 8799)")
    parser.add_argument("--no-gui", action="store_true",
                        help="跳过配置页，直接用命令行参数启动")
    args = parser.parse_args()

    print("=" * 56)
    print("  远程桌面控制 - 被控端")
    print("=" * 56)
    print(f"  本地配置页: http://localhost:{args.port}")
    print(f"  帧率/质量 : {config.SCREEN_FPS}fps / JPEG {config.SCREEN_QUALITY}")
    print("=" * 56)

    # 如果命令行提供了完整参数且 --no-gui，在 startup 事件中启动 agent 线程
    if args.no_gui and args.relay and args.hostname:
        state.hostname = args.hostname
        state.relay_url = args.relay
        state.token = args.token
        state.desktop_id = args.hostname

        @app.on_event("startup")
        async def _auto_start():
            import threading
            state._thread = threading.Thread(target=run_agent_thread, daemon=True)
            state._thread.start()

    # 启动配置页 Web 服务
    config_app = app
    # 挂载静态文件（如果目录存在）
    static_dir = os.path.join(BASE_DIR, "static")
    if os.path.isdir(static_dir):
        config_app.mount("/static", StaticFiles(directory=static_dir), name="static")

    uvicorn.run(config_app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
