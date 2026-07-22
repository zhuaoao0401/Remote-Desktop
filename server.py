"""直连模式服务器：在被控桌面上运行，直接提供 Web 界面和 WebSocket 控制。

适用场景：被控电脑可被浏览器直接访问（同一局域网，或已做端口映射）。

用法:
    python server.py [--host 0.0.0.0] [--port 8765]

启动后用浏览器访问 http://<被控电脑IP>:8765
默认账号: admin / admin123
"""
import asyncio
import json
import argparse
import os

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import (ScreenCapture, DeltaScreenCapture, InputController,
                  SessionManager, authenticate, pack_delta_frame, AudioCapture)
import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="远程桌面 - 直连模式")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
          name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

sessions = SessionManager(expiry=config.SESSION_EXPIRY)

# 延迟初始化（首次 WebSocket 连接时创建）
_screen: DeltaScreenCapture = None
_input: InputController = None


def get_components():
    global _screen, _input
    if _screen is None:
        _screen = DeltaScreenCapture(monitor=config.SCREEN_MONITOR,
                                     quality=config.SCREEN_QUALITY,
                                     scale=config.SCREEN_SCALE,
                                     fps=config.SCREEN_FPS)
    if _input is None:
        _input = InputController()
    return _screen, _input


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    token = request.cookies.get("rd_token") or request.query_params.get("token")
    if token and sessions.valid(token):
        return RedirectResponse(url=f"/desktop?token={token}", status_code=302)
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
async def login(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    # 检查是否被锁定
    if not sessions.check_rate_limit(client_ip):
        remaining = sessions.get_remaining_lock(client_ip)
        return JSONResponse({"ok": False,
                             "error": f"登录失败次数过多，请 {remaining} 秒后再试"},
                            status_code=429)
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if authenticate(username, password):
        sessions.record_success(client_ip)
        token = sessions.create(username)
        resp = JSONResponse({"ok": True, "token": token,
                             "redirect": f"/desktop?token={token}"})
        resp.set_cookie("rd_token", token, httponly=True,
                        max_age=config.SESSION_EXPIRY)
        return resp
    sessions.record_failed_attempt(client_ip)
    return JSONResponse({"ok": False, "error": "用户名或密码错误"}, status_code=401)


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("rd_token") or request.query_params.get("token")
    if token:
        sessions.destroy(token)
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie("rd_token")
    return resp


@app.get("/desktop", response_class=HTMLResponse)
async def desktop_page(request: Request):
    token = request.query_params.get("token") or request.cookies.get("rd_token")
    if not token or not sessions.valid(token):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "desktop.html", {
        "username": sessions.username(token),
        "token": token,
        "mode": "direct",
    })


# ---------------------------------------------------------------------------
# WebSocket 路由（屏幕流 + 输入控制）
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    token = ws.query_params.get("token")
    if not token or not sessions.valid(token):
        await ws.close(code=4001, reason="未授权")
        return
    await ws.accept()

    screen, inp = get_components()

    # 发送初始化信息（缩放后的画面尺寸，用于坐标映射）
    w, h = screen.get_size()
    monitors = screen.get_monitors()
    audio_cap_supported = AudioCapture.is_available()
    audio_obj = None
    await ws.send_text(json.dumps({
        "type": "init", "width": w, "height": h,
        "fps": config.SCREEN_FPS, "encoding": "delta",
        "monitors": monitors, "clipboard_supported": True,
        "audio_supported": audio_cap_supported,
        "audio_rate": 22050,
    }))

    capture_task = None
    audio_task = None
    heartbeat_task = None
    file_fp = None
    file_name = ""
    file_size = 0
    file_received = 0
    file_path = ""
    loop = asyncio.get_event_loop()
    try:
        # 如果支持音频，启动音频采集
        if audio_cap_supported:
            try:
                audio_obj = AudioCapture()
                audio_obj.start()
            except Exception as e:
                print(f"[音频] 采集失败: {e}")
                audio_obj = None

        async def capture_loop():
            """持续采集屏幕增量并发送。"""
            while True:
                frame = await loop.run_in_executor(None, screen.capture_delta)
                if frame:
                    try:
                        packed = pack_delta_frame(frame)
                        await ws.send_bytes(packed)
                    except Exception:
                        break
                await asyncio.sleep(0.005)

        async def audio_loop():
            """采集系统音频并发送。"""
            while True:
                if not audio_obj:
                    await asyncio.sleep(1)
                    continue
                chunk = await loop.run_in_executor(None, audio_obj.capture_chunk)
                if chunk:
                    try:
                        await ws.send_bytes(b'AUDI' + chunk)
                    except Exception:
                        pass
                await asyncio.sleep(0.02)

        async def command_loop():
            """接收并执行远程命令。"""
            nonlocal file_fp, file_name, file_size, file_received, file_path
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                # 二进制消息：文件传输数据块
                bin_data = msg.get("bytes")
                if bin_data and file_fp:
                    file_fp.write(bin_data)
                    file_received += len(bin_data)
                    pct = int(file_received / max(1, file_size) * 100)
                    await ws.send_text(json.dumps({
                        "type": "file_progress", "name": file_name,
                        "received": file_received, "size": file_size,
                        "percent": min(100, pct),
                    }))
                    if file_received >= file_size:
                        file_fp.close()
                        print(f"[文件接收完成] {file_name} → {file_path}")
                        await ws.send_text(json.dumps({
                            "type": "file_done", "name": file_name, "path": file_path,
                        }))
                        file_fp = None
                        file_received = 0
                    continue
                text = msg.get("text")
                if text:
                    try:
                        cmd = json.loads(text)
                        t = cmd.get("type")
                        if t == "ping":
                            await ws.send_text(json.dumps({"type": "pong", "t": cmd.get("t", 0)}))
                            continue
                        elif t == "switch_monitor":
                            idx = cmd.get("index", 1)
                            if screen.switch_monitor(idx):
                                w2, h2 = screen.get_size()
                                await ws.send_text(json.dumps({
                                    "type": "init", "width": w2, "height": h2,
                                    "fps": config.SCREEN_FPS, "encoding": "delta",
                                    "monitors": screen.get_monitors(),
                                }))
                            continue
                        elif t == "set_quality":
                            screen.set_quality(int(cmd.get("quality", 55)))
                            screen.reset()
                            continue
                        elif t == "set_fps":
                            screen.set_fps(int(cmd.get("fps", 15)))
                            continue
                        elif t == "get_clipboard":
                            result = await loop.run_in_executor(None, inp.execute, cmd)
                            if result:
                                await ws.send_text(json.dumps(result))
                            continue
                        elif t == "file_start":
                            import os
                            fname = cmd.get("name", "upload")
                            fsize = cmd.get("size", 0)
                            offset = cmd.get("offset", 0)
                            save_dir = os.path.join(os.path.expanduser("~"), "Desktop")
                            os.makedirs(save_dir, exist_ok=True)
                            save_path = os.path.join(save_dir, fname)
                            # 如果有 offset，尝试续传
                            if offset > 0 and os.path.exists(save_path) and os.path.getsize(save_path) >= offset:
                                file_fp = open(save_path, "ab")
                                file_received = offset
                                print(f"[文件续传] {fname} 从 {offset} 字节继续")
                            else:
                                # 避免重名
                                base, ext = os.path.splitext(fname)
                                counter = 1
                                while os.path.exists(save_path):
                                    save_path = os.path.join(save_dir, f"{base}_{counter}{ext}")
                                    counter += 1
                                file_fp = open(save_path, "wb")
                                file_received = 0
                            file_name = os.path.basename(save_path)
                            file_size = fsize
                            file_path = save_path
                            print(f"[文件接收开始] {fname} → {save_path}")
                            continue
                        elif t == "file_cancel":
                            if file_fp:
                                file_fp.close()
                            file_fp = None
                            continue
                        await loop.run_in_executor(None, inp.execute, cmd)
                    except Exception as e:
                        print(f"[命令错误] {e}")

        async def heartbeat_loop():
            """定时发送心跳，防止连接超时断开。"""
            while True:
                await asyncio.sleep(15)
                try:
                    await ws.send_text(json.dumps({"type": "heartbeat"}))
                except Exception:
                    break

        capture_task = asyncio.create_task(capture_loop())
        audio_task = asyncio.create_task(audio_loop())
        heartbeat_task = asyncio.create_task(heartbeat_loop())
        await command_loop()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WebSocket 错误] {e}")
    finally:
        if capture_task and not capture_task.done():
            capture_task.cancel()
        if audio_task and not audio_task.done():
            audio_task.cancel()
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
        if audio_obj:
            audio_obj.stop()
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="远程桌面 - 直连模式服务器")
    parser.add_argument("--host", default=config.DIRECT_HOST)
    parser.add_argument("--port", type=int, default=config.DIRECT_PORT)
    parser.add_argument("--ssl-cert", default="", help="SSL 证书路径 (pem)")
    parser.add_argument("--ssl-key", default="", help="SSL 私钥路径 (pem)")
    args = parser.parse_args()
    ssl_kwargs = {}
    if args.ssl_cert and args.ssl_key:
        ssl_kwargs["ssl_certfile"] = args.ssl_cert
        ssl_kwargs["ssl_keyfile"] = args.ssl_key
        proto = "https"
    else:
        proto = "http"
    print("=" * 56)
    print("  远程桌面控制 - 直连模式")
    print("=" * 56)
    print(f"  访问地址 : {proto}://127.0.0.1:{args.port}")
    print(f"  局域网   : {proto}://<本机IP>:{args.port}")
    print(f"  默认账号 : admin / admin123")
    print(f"  帧率/质量: {config.SCREEN_FPS}fps / JPEG {config.SCREEN_QUALITY}")
    print("=" * 56)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", **ssl_kwargs)
