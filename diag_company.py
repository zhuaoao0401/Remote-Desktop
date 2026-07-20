"""公司电脑网络诊断脚本：排查 WebSocket 403 原因。

在公司电脑上运行: python diag_company.py

使用前请修改下方 CONFIG 中的参数。
"""
import sys
import asyncio
import os

# ============================================================
#  配置区：按需修改
# ============================================================
CONFIG = {
    # 中继服务器 IP 或域名
    "SERVER_HOST": "43.163.239.11",
    # 中继服务器端口
    "SERVER_PORT": 9090,
    # Agent 令牌（与服务器的 AGENT_TOKEN 一致）
    "AGENT_TOKEN": "change-this-agent-secret-token",
    # 测试用的 desktop_id
    "DESKTOP_ID": "test",
    # 测试用的 hostname
    "HOSTNAME": "test",
    # 是否测试多个备选端口（用于排查端口封锁）
    "TEST_ALT_PORTS": True,
    # 备选端口列表
    "ALT_PORTS": [443, 8080, 8443, 80],
}
# ============================================================


def build_ws_url(path="/ws/agent", **params):
    """构建 WebSocket URL。"""
    host = CONFIG["SERVER_HOST"]
    port = CONFIG["SERVER_PORT"]
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    return f"ws://{host}:{port}{path}?{query}"


def build_http_url(path="/api/status"):
    """构建 HTTP URL。"""
    return f"http://{CONFIG['SERVER_HOST']}:{CONFIG['SERVER_PORT']}{path}"


print("=" * 56)
print("公司电脑 WebSocket 连接诊断")
print("=" * 56)
print(f"  目标服务器: {CONFIG['SERVER_HOST']}:{CONFIG['SERVER_PORT']}")
print(f"  令牌: {CONFIG['AGENT_TOKEN']}")

# 1. 检查代理设置
print("\n[1] 代理设置检查")
http_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
https_proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
no_proxy = os.environ.get('NO_PROXY') or os.environ.get('no_proxy')
print(f"  HTTP_PROXY:  {http_proxy or '(无)'}")
print(f"  HTTPS_PROXY: {https_proxy or '(无)'}")
print(f"  NO_PROXY:    {no_proxy or '(无)'}")
if http_proxy or https_proxy:
    print("  ⚠️ 检测到代理！这可能是 403 的原因——代理可能篡改了 WebSocket 请求")

# 2. 测试 HTTP 连通性
print("\n[2] HTTP 连通性测试")
import urllib.request
try:
    r = urllib.request.urlopen(build_http_url(), timeout=10)
    body = r.read().decode()
    # 检查是否被安全设备拦截
    if "netentsec" in body.lower() or "proxy notification" in body.lower() or "安全风险" in body:
        print(f"  ⚠️ HTTP 被安全设备拦截！返回的不是服务器数据，是安全警告页面")
        print(f"     响应片段: {body[:150]}")
    else:
        print(f"  ✅ HTTP 正常: {body[:100]}")
except Exception as e:
    print(f"  ❌ HTTP 失败: {e}")

# 3. 测试 WebSocket
print("\n[3] WebSocket 连接测试")
import websockets

token = CONFIG["AGENT_TOKEN"]
test_urls = [
    ("默认 URL", build_ws_url(token=token, desktop_id=CONFIG["DESKTOP_ID"], hostname=CONFIG["HOSTNAME"])),
    ("简化 URL（无 hostname）", build_ws_url(token=token, desktop_id=CONFIG["DESKTOP_ID"])),
    ("仅 token", build_ws_url(token=token)),
]

blocked_by_security = False

async def test_ws(name, url):
    global blocked_by_security
    try:
        async with websockets.connect(url, open_timeout=15) as ws:
            print(f"  ✅ {name}: 连接成功!")
            return True
    except websockets.exceptions.InvalidStatus as e:
        print(f"  ❌ {name}: HTTP {e.response.status_code}")
        try:
            body = e.response.body.decode('utf-8', errors='replace')
            if body:
                # 检测安全设备拦截
                if "netentsec" in body.lower() or "proxy notification" in body.lower() or "安全风险" in body:
                    print(f"     ⚠️ 被网络安全设备拦截（非服务器返回的403）")
                    blocked_by_security = True
                else:
                    print(f"     响应体: {body[:200]}")
        except:
            pass
        server = e.response.headers.get('server', '')
        if server:
            print(f"     Server头: {server}")
            if "netentsec" in server.lower():
                blocked_by_security = True
        return False
    except Exception as e:
        print(f"  ❌ {name}: {type(e).__name__}: {e}")
        return False

async def run_tests():
    for name, url in test_urls:
        await test_ws(name, url)
        await asyncio.sleep(1)

asyncio.run(run_tests())

# 4. 手动 HTTP 升级请求
print("\n[4] 手动 HTTP 升级请求测试")
import http.client
try:
    conn = http.client.HTTPConnection(CONFIG["SERVER_HOST"], CONFIG["SERVER_PORT"], timeout=10)
    conn.request("GET", f"/ws/agent?token={token}&desktop_id=test&hostname=test", headers={
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version": "13",
    })
    resp = conn.getresponse()
    print(f"  状态码: {resp.status}")
    print(f"  响应头: {dict(resp.getheaders())}")
    body = resp.read().decode('utf-8', errors='replace')
    if body:
        print(f"  响应体: {body[:200]}")
    if resp.status == 403:
        print("  ⚠️ 服务器拒绝——token 可能没正确传到服务器")
    elif resp.status == 101:
        print("  ✅ WebSocket 升级成功!")
    conn.close()
except Exception as e:
    print(f"  ❌ 错误: {e}")

# 5. 测试备选端口连通性
if CONFIG["TEST_ALT_PORTS"]:
    print(f"\n[5] 备选端口连通性测试")
    import socket
    for port in CONFIG["ALT_PORTS"]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            result = sock.connect_ex((CONFIG["SERVER_HOST"], port))
            if result == 0:
                print(f"  端口 {port}: ✅ 可连通（可尝试用此端口）")
            else:
                print(f"  端口 {port}: ❌ 不可达")
            sock.close()
        except Exception as e:
            print(f"  端口 {port}: ❌ {e}")

# 6. 结论
print("\n" + "=" * 56)
print("诊断结论")
print("=" * 56)

if blocked_by_security:
    print("""
🔴 确认：公司网络安全设备（绿盟/华为 HIS）拦截了 WebSocket 连接！

所有 WebSocket 请求都被安全代理拦截，返回的是安全警告页面，
根本没到达你的服务器。这不是令牌或代码问题。

解决方案（按推荐顺序）：

  1. 联系公司 IT 加白名单
     把 %s:%s 加入网络安全设备的白名单

  2. 换端口（可能绕过端口级拦截）
     在公网服务器上换端口启动:
       ./stop.sh && ./start.sh 443
     然后 agent 中继地址改为: ws://%s:443

  3. 用域名 + WSS（HTTPS 加密，安全设备无法检测内容）
     - 域名解析到服务器 IP
     - nginx 反代 + SSL 证书
     - agent 中继地址改为: wss://你的域名

  4. SSH 隧道（最可靠）
     在公司电脑执行:
       ssh -L 9090:127.0.0.1:9090 root@%s
     保持窗口开着，agent 中继地址填: ws://127.0.0.1:9090

  5. 手机热点临时验证
     公司电脑连手机热点测试，确认是公司网络问题
""" % (CONFIG["SERVER_HOST"], CONFIG["SERVER_PORT"], CONFIG["SERVER_HOST"], CONFIG["SERVER_HOST"]))
elif http_proxy or https_proxy:
    print("""
⚠️ 检测到 HTTP 代理！

代理可能篡改了 WebSocket 请求。
解决方案：
  set HTTP_PROXY=
  set HTTPS_PROXY=
  set NO_PROXY=%s
  python agent.py
""" % CONFIG["SERVER_HOST"])
else:
    print("""
如果 HTTP 正常但 WebSocket 403，可能原因：
  1. 公司防火墙深度检查，修改了 WebSocket 请求头
  2. token 参数被 URL 编码问题截断

尝试方案：
  set NO_PROXY=%s
  python agent.py
""" % CONFIG["SERVER_HOST"])
