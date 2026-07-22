"""远程桌面控制系统的配置模块。

可以通过环境变量覆盖默认配置，例如：
    set RD_USERS=admin:yourpass,viewer:viewerpass
    set RD_PORT=9000
    set RD_FPS=20
"""
import os
import hashlib
import secrets


# ---------------------------------------------------------------------------
# 认证配置
# ---------------------------------------------------------------------------

def hash_password(password: str, salt: str = None) -> str:
    """使用 PBKDF2-HMAC-SHA256 哈希密码。返回格式: pbkdf2_sha256$<salt>$<hash>"""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                             bytes.fromhex(salt), 100000)
    return f"pbkdf2_sha256${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码是否与存储的哈希匹配。"""
    try:
        algo, salt, hashval = stored.split('$', 2)
        if algo != 'pbkdf2_sha256':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                 bytes.fromhex(salt), 100000)
        return secrets.compare_digest(dk.hex(), hashval)
    except Exception:
        return False


def _load_users() -> dict:
    """加载用户列表。优先使用环境变量 RD_USERS (格式: user1:pass1,user2:pass2)。"""
    env_users = os.environ.get('RD_USERS', '')
    if env_users:
        users = {}
        for pair in env_users.split(','):
            if ':' in pair:
                u, p = pair.split(':', 1)
                users[u.strip()] = hash_password(p.strip())
        return users
    # 默认账号: admin / admin123
    users = {'admin': hash_password('admin123')}
    # 加载持久化密码（优先于默认密码）
    pass_file = os.path.join(os.path.expanduser("~"), ".remote_desktop_password.json")
    if os.path.exists(pass_file):
        try:
            import json
            with open(pass_file, 'r', encoding='utf-8') as f:
                pass_cfg = json.load(f)
            for user, hashed in pass_cfg.items():
                users[user] = hashed
        except Exception:
            pass
    return users


USERS = _load_users()

# 代理令牌：桌面 agent 连接中继服务器时需要出示此令牌
AGENT_TOKEN = os.environ.get('RD_AGENT_TOKEN', 'change-this-agent-secret-token')


# ---------------------------------------------------------------------------
# 屏幕采集配置
# ---------------------------------------------------------------------------

SCREEN_FPS = int(os.environ.get('RD_FPS', '15'))          # 每秒帧数
SCREEN_QUALITY = int(os.environ.get('RD_QUALITY', '55'))  # JPEG 质量 (10-95)
SCREEN_SCALE = float(os.environ.get('RD_SCALE', '0.75'))  # 画面缩放 (传输用，0.1-1.0)
SCREEN_MONITOR = int(os.environ.get('RD_MONITOR', '1'))   # 1=主显示器


# ---------------------------------------------------------------------------
# 服务器配置
# ---------------------------------------------------------------------------

DIRECT_HOST = os.environ.get('RD_HOST', '0.0.0.0')
DIRECT_PORT = int(os.environ.get('RD_PORT', '8765'))

RELAY_HOST = os.environ.get('RD_RELAY_HOST', '0.0.0.0')
RELAY_PORT = int(os.environ.get('RD_RELAY_PORT', '8766'))

SESSION_EXPIRY = int(os.environ.get('RD_SESSION_EXPIRY', str(3600 * 8)))  # 会话有效期(秒)


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == 'hash':
        print(hash_password(sys.argv[2]))
    else:
        print("用法:")
        print("  python config.py hash <密码>    生成密码哈希")
        print("  python config.py                显示此帮助")
        print("\n默认登录账号: admin / admin123")
        print("可用环境变量 RD_USERS=user:pass 修改账号密码")
