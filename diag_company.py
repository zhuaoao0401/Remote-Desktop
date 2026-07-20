"""公司电脑网络诊断脚本：排查 WebSocket 403 原因。

在公司电脑上运行: python diag_company.py
"""
import sys
import asyncio
import os

print("=" * 56)
print("公司电脑 WebSocket 连接诊断")
print("=" * 56)

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
    r = urllib.request.urlopen("http://43.163.239.11:9090/api/status", timeout=10)
    print(f"  ✅ HTTP 正常: {r.read().decode()}")
except Exception as e:
    print(f"  ❌ HTTP 失败: {e}")

# 3. 测试 WebSocket（带详细错误信息）
print("\n[3] WebSocket 连接测试")
import websockets

token = "change-this-agent-secret-token"
test_urls = [
    ("默认 URL", f"ws://43.163.239.11:9090/ws/agent?token={token}&desktop_id=test&hostname=test"),
    ("简化 URL（无 hostname）", f"ws://43.163.239.11:9090/ws/agent?token={token}&desktop_id=test"),
    ("IP 直连 80 端口模拟", f"ws://43.163.239.11:9090/ws/agent?token={token}"),
]

async def test_ws(name, url):
    try:
        async with websockets.connect(url, open_timeout=15) as ws:
            print(f"  ✅ {name}: 连接成功!")
            return True
    except websockets.exceptions.InvalidStatus as e:
        print(f"  ❌ {name}: HTTP {e.response.status_code}")
        try:
            body = e.response.body.decode('utf-8', errors='replace')
            if body:
                print(f"     响应体: {body[:200]}")
        except:
            pass
        # 检查是否是代理返回的
        server = e.response.headers.get('server', '')
        if server:
            print(f"     Server头: {server}")
        return False
    except Exception as e:
        print(f"  ❌ {name}: {type(e).__name__}: {e}")
        return False

async def run_tests():
    for name, url in test_urls:
        await test_ws(name, url)
        await asyncio.sleep(1)

asyncio.run(run_tests())

# 4. 测试用 HTTP 模拟 WebSocket 升级请求
print("\n[4] 手动 HTTP 升级请求测试")
import http.client
try:
    conn = http.client.HTTPConnection("43.163.239.11", 9090, timeout=10)
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

# 5. 结论
print("\n" + "=" * 56)
print("诊断结论")
print("=" * 56)
if http_proxy or https_proxy:
    print("""
⚠️ 检测到公司网络有 HTTP 代理！

代理可能篡改了 WebSocket 请求，导致 token 丢失。
解决方案：
  1. 设置 NO_PROXY 绕过代理:
     set NO_PROXY=43.163.239.11
     python agent.py

  2. 或在 Python 中禁用代理:
     set HTTP_PROXY=
     set HTTPS_PROXY=
     python agent.py
""")
else:
    print("""
如果 HTTP 正常但 WebSocket 403，可能原因：
  1. 公司防火墙深度检查，修改了 WebSocket 请求头
  2. token 参数被 URL 编码问题截断

尝试方案：
  set NO_PROXY=43.163.239.11
  python agent.py
""")
