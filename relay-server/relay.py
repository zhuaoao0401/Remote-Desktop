"""中继服务器：运行在公网服务器上，在浏览器和桌面代理之间转发数据。

适用场景：被控电脑在内网/NAT 后面，无法被直接访问。
架构:
    浏览器  <-->  中继服务器(公网)  <-->  桌面代理(内网)

用法:
    python relay.py [--host 0.0.0.0] [--port 8766]

启动后:
    1. 在被控电脑运行: python agent.py --relay ws://<公网IP>:8766
    2. 用浏览器访问:   http://<公网IP>:8766
"""
import asyncio
import json
import argparse
import os
from collections import defaultdict

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import SessionManager, authenticate
import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="远程桌面 - 中继服务器")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
          name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

sessions = SessionManager(expiry=config.SESSION_EXPIRY)

# 已连接的桌面代理: desktop_id -> WebSocket
agents: dict = {}
# 已连接的浏览器客户端: desktop_id -> set(WebSocket)
clients: dict = defaultdict(set)
# 各桌面的初始化信息(分辨率等)，供新客户端获取
agent_inits: dict = {}
# 各桌面的主机名: desktop_id -> hostname
agent_hostnames: dict = {}
# 各桌面上线时间: desktop_id -> timestamp
agent_online_since: dict = {}


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    token = request.cookies.get("rd_token") or request.query_params.get("token")
    if token and sessions.valid(token):
        return RedirectResponse(url=f"/hosts?token={token}", status_code=302)
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if authenticate(username, password):
        token = sessions.create(username)
        resp = JSONResponse({"ok": True, "token": token,
                             "redirect": f"/hosts?token={token}"})
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


@app.get("/hosts", response_class=HTMLResponse)
async def hosts_page(request: Request):
    """主机选择页：列出所有在线主机，选择后进入控制台。"""
    token = request.query_params.get("token") or request.cookies.get("rd_token")
    if not token or not sessions.valid(token):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "hosts.html", {
        "username": sessions.username(token),
        "token": token,
    })


@app.get("/desktop", response_class=HTMLResponse)
async def desktop_page(request: Request):
    token = request.query_params.get("token") or request.cookies.get("rd_token")
    if not token or not sessions.valid(token):
        return RedirectResponse(url="/", status_code=302)
    desktop_id = request.query_params.get("desktop_id", "")
    return templates.TemplateResponse(request, "desktop.html", {
        "username": sessions.username(token),
        "token": token,
        "mode": "relay",
        "desktop_id": desktop_id,
        "hostname": agent_hostnames.get(desktop_id, desktop_id),
    })


@app.get("/api/hosts")
async def api_hosts():
    """返回当前在线的主机列表（含主机名）。"""
    import time
    hosts = []
    for did, hostname in agent_hostnames.items():
        hosts.append({
            "desktop_id": did,
            "hostname": hostname,
            "online": True,
            "since": agent_online_since.get(did),
        })
    return {"hosts": hosts, "count": len(hosts)}


@app.get("/api/status")
async def status():
    """返回当前在线的桌面列表。"""
    return {"desktops": list(agents.keys()),
            "online": len(agents) > 0}


# ---------------------------------------------------------------------------
# WebSocket: 桌面代理接入端点
# ---------------------------------------------------------------------------

@app.websocket("/ws/agent")
async def agent_endpoint(ws: WebSocket):
    import time
    import urllib.parse
    token = ws.query_params.get("token")
    desktop_id = urllib.parse.unquote(ws.query_params.get("desktop_id", "default"))
    hostname = urllib.parse.unquote(ws.query_params.get("hostname", desktop_id))
    if token != config.AGENT_TOKEN:
        await ws.close(code=4001, reason="无效的代理令牌")
        return
    await ws.accept()
    agents[desktop_id] = ws
    agent_hostnames[desktop_id] = hostname
    agent_online_since[desktop_id] = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[代理上线] {hostname} (id={desktop_id})")

    # 通知所有等待的客户端
    for c in list(clients.get(desktop_id, set())):
        try:
            await c.send_text(json.dumps({"type": "agent_connected",
                                          "hostname": hostname}))
        except Exception:
            pass

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is None:
                data = msg.get("text")
            if data is None:
                continue

            # 文本消息：记录 init 信息，并转发给客户端
            if isinstance(data, str):
                try:
                    parsed = json.loads(data)
                    if parsed.get("type") == "init":
                        agent_inits[desktop_id] = data
                except Exception:
                    pass

            # 转发给该桌面的所有客户端
            dead = []
            for c in clients.get(desktop_id, set()):
                try:
                    if isinstance(data, bytes):
                        await c.send_bytes(data)
                    else:
                        await c.send_text(data)
                except Exception:
                    dead.append(c)
            for c in dead:
                clients[desktop_id].discard(c)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[代理错误] {e}")
    finally:
        agents.pop(desktop_id, None)
        agent_inits.pop(desktop_id, None)
        agent_hostnames.pop(desktop_id, None)
        agent_online_since.pop(desktop_id, None)
        for c in list(clients.get(desktop_id, set())):
            try:
                await c.send_text(json.dumps({"type": "agent_disconnected"}))
            except Exception:
                pass
        print(f"[代理下线] {desktop_id}")


# ---------------------------------------------------------------------------
# WebSocket: 浏览器客户端接入端点
# ---------------------------------------------------------------------------

@app.websocket("/ws/client")
async def client_endpoint(ws: WebSocket):
    token = ws.query_params.get("token")
    desktop_id = ws.query_params.get("desktop_id", "default")
    if not token or not sessions.valid(token):
        await ws.close(code=4001, reason="未授权")
        return
    await ws.accept()
    clients[desktop_id].add(ws)

    # 先发送缓存的 init 信息（分辨率）
    init = agent_inits.get(desktop_id)
    if init:
        await ws.send_text(init)

    # 告知当前代理连接状态
    if desktop_id in agents:
        await ws.send_text(json.dumps({"type": "agent_connected"}))
    else:
        await ws.send_text(json.dumps({
            "type": "agent_disconnected",
            "message": "桌面未连接，请等待代理上线"}))

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            agent = agents.get(desktop_id)
            if not agent:
                continue
            data = msg.get("text")
            if data is None:
                data = msg.get("bytes")
            if data is None:
                continue
            try:
                if isinstance(data, bytes):
                    await agent.send_bytes(data)
                else:
                    await agent.send_text(data)
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[客户端错误] {e}")
    finally:
        clients[desktop_id].discard(ws)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="远程桌面 - 中继服务器")
    parser.add_argument("--host", default=config.RELAY_HOST)
    parser.add_argument("--port", type=int, default=config.RELAY_PORT)
    args = parser.parse_args()
    print("=" * 56)
    print("  远程桌面控制 - 中继服务器")
    print("=" * 56)
    print(f"  Web 访问 : http://127.0.0.1:{args.port}")
    print(f"  公网访问 : http://<服务器公网IP>:{args.port}")
    print(f"  默认账号 : admin / admin123")
    print(f"  代理令牌 : {config.AGENT_TOKEN}")
    print("-" * 56)
    print("  在被控电脑运行:")
    print(f"  python agent.py --relay ws://<服务器IP>:{args.port}")
    print("=" * 56)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
