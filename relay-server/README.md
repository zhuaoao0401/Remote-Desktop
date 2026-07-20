# 远程桌面 - 中继服务器（公网部署版）

这个文件夹包含**部署到公网服务器**所需的全部文件。公网服务器只负责中转数据，不需要安装图形界面相关的库。

## 📁 文件清单

```
relay-server/
├── relay.py            # 中继服务器主程序
├── config.py           # 配置（账号密码、端口、令牌）
├── core.py             # 核心模块（会话管理、认证）
├── requirements.txt    # 依赖（精简版，无需图形库）
├── start.sh            # 一键启动（自动装依赖 + 后台运行）
├── stop.sh             # 停止服务
├── status.sh           # 查看运行状态
├── templates/          # Web 页面模板
│   ├── login.html      # 登录页
│   └── desktop.html    # 控制台页
└── static/             # 前端资源
    ├── style.css       # 样式
    └── app.js          # 交互逻辑
```

## 🚀 快速部署（3 步）

### 1. 上传到公网服务器

把整个 `relay-server` 文件夹上传到服务器（例如 `/opt/relay-server`）：

```bash
# 方法一：用 scp 上传压缩包
scp -r relay-server/ root@你的服务器IP:/opt/

# 方法二：用 rsync
rsync -avz relay-server/ root@你的服务器IP:/opt/relay-server/
```

### 2. 一键启动

SSH 登录服务器后：

```bash
cd /opt/relay-server
chmod +x start.sh stop.sh status.sh
./start.sh
```

脚本会自动：
- ✅ 创建 Python 虚拟环境
- ✅ 安装所有依赖
- ✅ 后台启动中继服务器
- ✅ 输出访问地址和代理连接命令

### 3. 在被控电脑上运行代理

在被控电脑（你想要远程控制的电脑）上，用 `remote-desktop` 主目录里的 `agent.py`：

```bash
python agent.py --relay ws://你的服务器IP:8766
```

然后用任意浏览器访问 `http://你的服务器IP:8766`，输入账号密码即可远程控制桌面。

## 🔧 常用命令

```bash
./start.sh          # 启动（默认端口 8766）
./start.sh 9000     # 启动并指定端口
./stop.sh           # 停止
./status.sh         # 查看状态 + 最近日志
tail -f relay.log   # 实时查看完整日志
```

## ⚙️ 配置修改

所有配置通过**环境变量**或修改 `config.py` 调整。推荐用环境变量，改完重启即可：

```bash
# 修改账号密码（格式: 用户1:密码1,用户2:密码2）
export RD_USERS="admin:MyStrongPass2024"

# 修改代理令牌（被控电脑 agent.py 连接时需要相同的令牌）
export RD_AGENT_TOKEN="my-random-secret-token-xyz"

# 修改端口
export RD_RELAY_PORT=9000

# 修改会话有效期（秒，默认 8 小时）
export RD_SESSION_EXPIRY=28800

# 然后启动
./start.sh
```

> **重要安全提示**：请务必修改默认密码和代理令牌！

## 🌐 设置开机自启（可选）

用 systemd 让服务器开机自动启动：

```bash
sudo cat > /etc/systemd/system/relay-server.service << 'EOF'
[Unit]
Description=Remote Desktop Relay Server
After=network.target

[Service]
Type=forking
WorkingDirectory=/opt/relay-server
ExecStart=/opt/relay-server/start.sh
ExecStop=/opt/relay-server/stop.sh
PIDFile=/opt/relay-server/relay.pid
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable relay-server
sudo systemctl start relay-server
sudo systemctl status relay-server
```

## 🔒 安全建议

1. **改默认密码**：默认 `admin/admin123`，必须修改
2. **改代理令牌**：默认令牌不安全，用 `RD_AGENT_TOKEN` 设为随机字符串
3. **用 HTTPS**：生产环境建议用 Nginx 反向代理 + SSL 证书
4. **防火墙**：只开放需要的端口（如 8766），限制来源 IP 更佳

### Nginx HTTPS 反向代理示例

```nginx
server {
    listen 443 ssl;
    server_name your.domain.com;
    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8766;
        proxy_set_header Host $host;
    }
    location /ws/ {
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
```

配置 Nginx 后，`start.sh` 可以只监听本地：
```bash
# 修改 config.py 或用环境变量让 relay 只监听 127.0.0.1
export RD_RELAY_HOST=127.0.0.1
./start.sh
```

## 🐛 故障排查

**启动失败？**
```bash
cat relay.log        # 查看完整日志
./stop.sh && ./start.sh  # 重启
```

**端口被占用？**
```bash
lsof -i :8766        # 查看占用进程
./start.sh 9000      # 换个端口
```

**被控电脑连不上中继？**
- 检查服务器防火墙是否开放端口（如 `sudo ufw allow 8766`）
- 检查云服务器安全组规则是否放行
- 确认 agent.py 的 `--relay` 地址和端口正确
- 确认 `AGENT_TOKEN` 与中继服务器一致

**浏览器登录后显示"桌面未连接"？**
- 这是正常的——说明中继服务器正常，但被控电脑的 agent 还没连上
- 在被控电脑运行 `agent.py` 后会自动连接

## 📊 架构说明

```
浏览器                中继服务器(公网)              被控电脑(内网)
  |                       |                            |
  |--- HTTP 登录 -------->|                            |
  |<- 登录成功 / Token ---|                            |
  |                       |                            |
  |--- WS 连接(带Token) ->|<--- WS 连接(带AgentToken)--|
  |                       |                            |
  |<-- 屏幕画面帧 --------|<--- 屏幕画面帧 ------------|
  |                       |                            |
  |--- 鼠标/键盘命令 ---->|----> 鼠标/键盘命令 -------->|
  |                       |                            |
  |     (中继只转发，不接触桌面)    (agent 执行截屏和输入)  |
```

中继服务器**只转发数据**，不安装任何图形库，不接触桌面操作，适合部署在轻量 VPS 上。
