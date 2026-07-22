"""远程桌面 - 统一启动入口

一个文件搞定一切：
  1. 自动检测并安装依赖
  2. 同时启动本地服务端（直连模式）+ 被控端配置页
  3. 浏览器自动打开管理界面

用法:
    python run.py              # 启动所有服务
    python run.py --port 8799  # 指定端口
    python run.py --no-browser # 不自动打开浏览器

打包成 exe:
    pip install pyinstaller
    pyinstaller --onefile --name 远程桌面 --add-data "templates;templates" --add-data "static;static" run.py
"""

import os
import sys
import subprocess
import importlib
import threading
import time
import webbrowser
import argparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ===========================================================================
# 1. 依赖自动安装
# ===========================================================================

REQUIRED_PACKAGES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("websockets", "websockets"),
    ("mss", "mss"),
    ("PIL", "Pillow"),
    ("pyautogui", "pyautogui"),
    ("pyperclip", "pyperclip"),
    ("soundcard", "soundcard"),
    ("numpy", "numpy"),
    ("multipart", "python-multipart"),
    ("jinja2", "jinja2"),
]


def check_and_install_dependencies():
    """检查并自动安装缺失的依赖。"""
    missing = []
    for import_name, pip_name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)

    if not missing:
        print("[依赖] 所有依赖已安装")
        return True

    print(f"[依赖] 缺少 {len(missing)} 个包: {', '.join(missing)}")
    print("[依赖] 正在自动安装...")

    # 获取当前 Python 的 pip
    pip_cmd = [sys.executable, "-m", "pip", "install"] + missing
    try:
        result = subprocess.run(
            pip_cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            print("[依赖] 安装成功!")
            return True
        else:
            print(f"[依赖] 安装失败: {result.stderr[:500]}")
            print(f"\n请手动运行: pip install {' '.join(missing)}")
            return False
    except subprocess.TimeoutExpired:
        print("[依赖] 安装超时（120秒），请手动安装")
        return False
    except Exception as e:
        print(f"[依赖] 安装异常: {e}")
        print(f"\n请手动运行: pip install {' '.join(missing)}")
        return False


# ===========================================================================
# 2. 统一启动
# ===========================================================================

def start_services(port=8799, open_browser=True):
    """启动所有服务：本地直连服务端 + 被控端配置页。"""
    # 延迟导入（确保依赖已安装）
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles

    # 创建统一应用
    from server import app as server_app, sessions
    from core import ScreenCapture, DeltaScreenCapture, InputController, AudioCapture, pack_delta_frame
    from agent import app as agent_app, state, run_agent_thread
    import config

    # 把 agent 的 API 路由挂载到 server_app 上
    # 这样一个端口同时提供：登录/控制端 + 被控端配置
    # server_app 已经有 / /login /desktop /ws 等路由
    # agent_app 有 /api/state /api/start /api/stop

    # 直接把 agent 的 API 路由复制到 server_app
    @server_app.get("/api/state")
    async def get_agent_state():
        return state.to_dict()

    @server_app.post("/api/start")
    async def api_start(request: Request):
        body = await request.json()
        hostname = (body.get("hostname") or "").strip()
        relay = (body.get("relay") or "").strip() or _ag.AgentState.DEFAULT_RELAY_URL
        token = (body.get("token") or "").strip()
        if not hostname:
            return JSONResponse({"ok": False, "error": "请输入主机名"}, status_code=400)
        if state._thread is not None:
            return JSONResponse({"ok": False, "error": "已在运行，请先停止"}, status_code=400)

        state.hostname = hostname
        state.relay_url = relay
        if token:
            state.token = token
        state.desktop_id = hostname
        state.status = "connecting"
        state.message = "正在连接中继..."

        import threading as _t
        state._thread = _t.Thread(target=run_agent_thread, daemon=True)
        state._thread.start()
        return JSONResponse({"ok": True})

    @server_app.post("/api/stop")
    async def api_stop():
        state._stop_flag = True
        if state._ws:
            try:
                await state._ws.close()
            except Exception:
                pass
        if state._thread and state._thread.is_alive():
            state._thread.join(timeout=3)
        state._thread = None
        state._stop_flag = False
        state.status = "idle"
        state.message = "已停止"
        state._ws = None
        return JSONResponse({"ok": True})

    # 注册 agent 的其他 API 路由到 server_app
    import agent as _ag

    @server_app.get("/api/autostart")
    async def _ag_autostart_get():
        return _ag.is_autostart_enabled() and JSONResponse({"enabled": _ag.is_autostart_enabled()}) or JSONResponse({"enabled": False})

    @server_app.post("/api/autostart")
    async def _ag_autostart_post(request: Request):
        body = await request.json()
        enable = body.get("enable", False)
        if enable:
            ok, msg = _ag.enable_autostart()
        else:
            ok, msg = _ag.disable_autostart()
        return JSONResponse({"ok": ok, "message": msg, "enabled": _ag.is_autostart_enabled()})

    @server_app.post("/api/change_password")
    async def _ag_change_pwd(request: Request):
        body = await request.json()
        old_pass = body.get("old_password", "")
        new_pass = body.get("new_password", "")
        if not new_pass or len(new_pass) < 4:
            return JSONResponse({"ok": False, "error": "新密码至少4个字符"}, status_code=400)
        from config import USERS, hash_password
        import config as cfg
        if not cfg.verify_password(old_pass, USERS.get("admin", "")):
            return JSONResponse({"ok": False, "error": "旧密码错误"}, status_code=401)
        USERS["admin"] = hash_password(new_pass)
        import json as _json, os as _os
        pass_file = _os.path.join(_os.path.expanduser("~"), ".remote_desktop_password.json")
        try:
            with open(pass_file, 'w', encoding='utf-8') as f:
                _json.dump({"admin": USERS["admin"]}, f)
        except Exception:
            pass
        return JSONResponse({"ok": True, "message": "密码修改成功"})

    @server_app.post("/api/relay_login")
    async def _ag_relay_login(request: Request):
        import urllib.request, urllib.parse, json as _json
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
                result = _json.loads(resp.read().decode())
                if result.get("ok") and result.get("token"):
                    _ag._relay_tokens[relay_url] = result["token"]
                    return JSONResponse({"ok": True, "token": result["token"]})
                return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"连接失败: {e}"}, status_code=500)

    @server_app.get("/api/relay_hosts")
    async def _ag_relay_hosts(relay_url: str = ""):
        import urllib.request, json as _json
        relay_url = relay_url.rstrip("/")
        if not relay_url:
            relay_url = state.relay_url.replace("ws://", "http://").replace("wss://", "https://")
        if not relay_url:
            return JSONResponse({"ok": False, "error": "未配置中继服务器地址"}, status_code=400)
        token = _ag._relay_tokens.get(relay_url, "")
        try:
            url = relay_url + "/api/hosts"
            if token:
                url += f"?token={token}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read().decode())
                return JSONResponse(result)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return JSONResponse({"ok": False, "error": "需要登录", "need_login": True})
            return JSONResponse({"ok": False, "error": f"服务器错误: {e.code}"}, status_code=500)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"连接失败: {e}"}, status_code=500)

    # 把 agent 的配置页也加到 server_app
    @server_app.get("/config", response_class=HTMLResponse)
    async def config_page():
        from agent import CONFIG_PAGE_HTML
        return HTMLResponse(CONFIG_PAGE_HTML)

    # 主页重定向：直接去配置页
    @server_app.get("/", response_class=HTMLResponse)
    async def root_page(request: Request):
        return RedirectResponse(url="/config", status_code=302)

    # 打印启动信息
    print()
    print("=" * 56)
    print("  远程桌面 - 统一启动")
    print("=" * 56)
    print(f"  管理界面 : http://localhost:{port}")
    print(f"  控制端   : http://localhost:{port}/desktop")
    print(f"  被控设置 : http://localhost:{port}/config")
    print(f"  直连模式 : 本机可同时作为被控端和控制端")
    print(f"  中继模式 : 被控设置中填写中继地址即可")
    print("=" * 56)
    print()

    # 自动打开浏览器
    if open_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{port}/")
        threading.Thread(target=_open, daemon=True).start()

    # 启动
    uvicorn.run(server_app, host="0.0.0.0", port=port, log_level="warning")


# ===========================================================================
# 3. 主入口
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="远程桌面 - 统一启动")
    parser.add_argument("--port", type=int, default=8799,
                        help="服务端口 (默认 8799)")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器")
    parser.add_argument("--install-only", action="store_true",
                        help="仅安装依赖，不启动服务")
    args = parser.parse_args()

    # 第一步：检查并安装依赖
    if not check_and_install_dependencies():
        print("\n依赖安装失败，请手动安装后重试。")
        input("按回车键退出...")
        sys.exit(1)

    if args.install_only:
        print("\n依赖安装完成!")
        return

    # 第二步：启动服务
    try:
        from core import setup_logging
        setup_logging()
        start_services(port=args.port, open_browser=not args.no_browser)
    except KeyboardInterrupt:
        print("\n已停止")
    except Exception as e:
        print(f"\n启动失败: {e}")
        import traceback
        traceback.print_exc()
        input("按回车键退出...")


if __name__ == "__main__":
    main()
