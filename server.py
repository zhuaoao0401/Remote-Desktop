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
                  SessionManager, authenticate, pack_delta_frame)
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
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if authenticate(username, password):
        token = sessions.create(username)
        resp = JSONResponse({"ok": True, "token": token,
                             "redirect": f"/desktop?token={token}"})
        resp.set_cookie("rd_token", token, httponly=True,
                        max_age=config.SESSION_EXPIRY)
        return resp
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
    await ws.send_text(json.dumps({
        "type": "init", "width": w, "height": h,
        "fps": config.SCREEN_FPS, "encoding": "delta",
    }))

    capture_task = None
    loop = asyncio.get_event_loop()
    try:
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

        async def command_loop():
            """接收并执行远程命令。"""
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                text = msg.get("text")
                if text:
                    try:
                        cmd = json.loads(text)
                        await loop.run_in_executor(None, inp.execute, cmd)
                    except Exception as e:
                        print(f"[命令错误] {e}")

        capture_task = asyncio.create_task(capture_loop())
        await command_loop()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WebSocket 错误] {e}")
    finally:
        if capture_task and not capture_task.done():
            capture_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="远程桌面 - 直连模式服务器")
    parser.add_argument("--host", default=config.DIRECT_HOST)
    parser.add_argument("--port", type=int, default=config.DIRECT_PORT)
    args = parser.parse_args()
    print("=" * 56)
    print("  远程桌面控制 - 直连模式")
    print("=" * 56)
    print(f"  访问地址 : http://127.0.0.1:{args.port}")
    print(f"  局域网   : http://<本机IP>:{args.port}")
    print(f"  默认账号 : admin / admin123")
    print(f"  帧率/质量: {config.SCREEN_FPS}fps / JPEG {config.SCREEN_QUALITY}")
    print("=" * 56)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
