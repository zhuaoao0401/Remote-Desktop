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
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import websockets

from core import DeltaScreenCapture, InputController, pack_delta_frame
import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class AgentState:
    """Agent 运行状态管理。"""

    def __init__(self):
        self.hostname = ""
        self.relay_url = ""
        self.token = config.AGENT_TOKEN
        self.desktop_id = ""        # 基于主机名生成，用于中继路由
        self.status = "idle"        # idle / connecting / connected / error
        self.message = "未启动"
        self._thread = None         # threading.Thread
        self._stop_flag = False     # 停止标志
        self.last_connected = None
        self._ws = None

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
<title>被控端配置 - 远程桌面</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body class="login-body">
<div class="login-card" style="width:460px;">
  <div class="logo">🖥️</div>
  <h1>被控端配置</h1>
  <p class="subtitle">设置主机名并连接到中继服务器</p>
  <form id="cfgForm">
    <div class="form-group">
      <label for="hostname">主机名称（控制端会看到此名称）</label>
      <input type="text" id="hostname" required placeholder="例如：我的办公电脑"
             value="" maxlength="32">
    </div>
    <div class="form-group">
      <label for="relay">中继服务器地址</label>
      <input type="text" id="relay" required placeholder="ws://1.2.3.4:9090"
             value="">
    </div>
    <div class="form-group">
      <label for="token">代理令牌</label>
      <input type="text" id="token" placeholder="留空使用默认令牌"
             value="">
    </div>
    <div id="status-box" class="status-box idle">状态：未启动</div>
    <button type="submit" id="startBtn" class="btn-primary">启动连接</button>
    <button type="button" id="stopBtn" class="btn-small"
            style="width:100%;margin-top:10px;display:none;">停止连接</button>
  </form>
  <p class="hint">配置页地址：http://localhost:8799</p>
</div>
<script>
const $ = id => document.getElementById(id);
let polling = null;

function setStatus(s, msg) {
  const box = $('status-box');
  box.className = 'status-box ' + s;
  box.textContent = '状态：' + msg;
}

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    $('hostname').value = d.hostname || $('hostname').value;
    $('relay').value = d.relay_url || $('relay').value;
    setStatus(d.status, d.message);
    const running = d.status === 'connected' || d.status === 'connecting';
    $('startBtn').style.display = running ? 'none' : 'block';
    $('stopBtn').style.display = running ? 'block' : 'none';
  } catch(e) {}
}

$('cfgForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const hostname = $('hostname').value.trim();
  const relay = $('relay').value.trim();
  const token = $('token').value.trim();
  if (!hostname || !relay) return;
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
    relay = (body.get("relay") or "").strip()
    token = (body.get("token") or "").strip()
    if not hostname:
        return JSONResponse({"ok": False, "error": "请输入主机名"}, status_code=400)
    if not relay:
        return JSONResponse({"ok": False, "error": "请输入中继地址"}, status_code=400)
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
                    state.message = "已连接，等待控制端接入"
                    state.last_connected = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"[已连接] 主机={state.hostname} 中继={state.relay_url}")

                    w, h = screen.get_size()
                    monitors = screen.get_monitors()
                    await ws.send(json.dumps({
                        "type": "init", "width": w, "height": h,
                        "fps": config.SCREEN_FPS, "encoding": "delta",
                        "hostname": state.hostname,
                        "monitors": monitors,
                        "clipboard_supported": True,
                    }))

                    loop = asyncio.get_event_loop()

                    # 默认不传画面，等控制端接入后再传
                    streaming_active = False

                    # 文件传输状态
                    file_recv = {"fp": None, "name": "", "size": 0, "received": 0}

                    async def capture_loop():
                        nonlocal streaming_active
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
                                        state.message = "已连接，正在传输画面"
                                        print(f"[开始传输] 控制端已接入")
                                    continue
                                elif cmd_type == "stop_streaming":
                                    if streaming_active:
                                        streaming_active = False
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
                                    # 开始接收文件
                                    import os
                                    fname = cmd.get("name", "upload")
                                    fsize = cmd.get("size", 0)
                                    save_dir = os.path.join(os.path.expanduser("~"), "Desktop")
                                    os.makedirs(save_dir, exist_ok=True)
                                    save_path = os.path.join(save_dir, fname)
                                    # 避免重名
                                    base, ext = os.path.splitext(fname)
                                    counter = 1
                                    while os.path.exists(save_path):
                                        save_path = os.path.join(save_dir, f"{base}_{counter}{ext}")
                                        counter += 1
                                    file_recv["fp"] = open(save_path, "wb")
                                    file_recv["name"] = os.path.basename(save_path)
                                    file_recv["size"] = fsize
                                    file_recv["received"] = 0
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

                    await asyncio.gather(capture_loop(), command_loop())
            except asyncio.CancelledError:
                raise
            except (websockets.exceptions.ConnectionClosed,
                    ConnectionRefusedError, OSError) as e:
                state.status = "connecting"
                state.message = f"连接断开，5秒后重连: {e}"
                print(f"[连接断开] {e}  5秒后重连...")
            except Exception as e:
                state.status = "connecting"
                state.message = f"异常，5秒后重连: {e}"
                print(f"[异常] {e}  5秒后重连...")
            # 等待重连，但每秒检查停止标志
            for _ in range(5):
                if state._stop_flag:
                    break
                await asyncio.sleep(1)
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
