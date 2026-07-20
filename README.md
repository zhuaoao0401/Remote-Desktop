# 远程桌面控制系统

基于 Python 的 Web 远程桌面控制工具。通过浏览器即可远程操控电脑的鼠标和键盘，支持账号密码登录认证，支持**直连模式**和**中继模式**（用于 NAT 穿透）。

## ✨ 功能特性

- 🖥️ **远程画面**：实时传输桌面画面（JPEG 压缩，可调帧率/画质）
- 🖱️ **鼠标控制**：移动、左/右/中键点击、双击、滚轮
- ⌨️ **键盘控制**：全键盘按键、组合键、文字粘贴输入
- 🔐 **账号认证**：用户名/密码登录，会话令牌管理
- 🌐 **两种模式**：
  - **直连模式**：被控电脑可直接访问（同局域网或端口映射）
  - **中继模式**：被控电脑在 NAT 后，通过公网中继服务器中转
- 📱 **触屏支持**：基础触摸操作
- 🎛️ **虚拟键盘**：提供功能键、方向键快捷面板

## 📦 项目结构

```
remote-desktop/
├── config.py          # 配置（账号密码、屏幕参数、端口）
├── core.py            # 核心：截屏、输入控制、会话管理
├── server.py          # 直连模式服务器（运行在被控电脑）
├── relay.py           # 中继服务器（运行在公网服务器）
├── agent.py           # 桌面代理（运行在被控电脑，连接中继）
├── requirements.txt   # 依赖
├── run_direct.bat     # Windows 一键启动直连模式
├── templates/
│   ├── login.html     # 登录页
│   └── desktop.html   # 控制台页
└── static/
    ├── style.css      # 样式
    └── app.js         # 前端交互逻辑
```

## 🚀 快速开始

### 1. 安装依赖

```bash
cd remote-desktop
pip install -r requirements.txt
```

> Windows 上 `pyautogui` 需要 `pygetwindow`，会自动安装。

### 2. 选择运行模式

---

#### 模式一：直连模式（最简单，推荐局域网使用）

被控电脑和浏览器在同一网络，或已配置端口映射。

**在被控电脑上运行：**
```bash
python server.py
```
或双击 `run_direct.bat`。

**访问：** 浏览器打开 `http://<被控电脑IP>:8765`，输入账号密码登录。

默认账号：`admin` / `admin123`

---

#### 模式二：中继模式（跨网络 / NAT 穿透）

被控电脑在内网，通过一台公网服务器中转。

**① 在公网服务器上运行中继服务器：**
```bash
python relay.py --port 8766
```

**② 在被控电脑上运行桌面代理：**
```bash
python agent.py --relay ws://<公网服务器IP>:8766
```

**③ 浏览器访问：** `http://<公网服务器IP>:8766`，登录后即可控制。

> 中继模式下，代理需用令牌认证。默认令牌在 `config.py` 的 `AGENT_TOKEN`，可用环境变量 `RD_AGENT_TOKEN` 修改。

---

## ⚙️ 配置说明

所有配置可通过**环境变量**覆盖，无需改代码：

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `RD_USERS` | 用户账号，格式 `user1:pass1,user2:pass2` | `admin:admin123` |
| `RD_PORT` | 直连模式端口 | `8765` |
| `RD_RELAY_PORT` | 中继服务器端口 | `8766` |
| `RD_HOST` | 直连监听地址 | `0.0.0.0` |
| `RD_FPS` | 屏幕帧率 | `15` |
| `RD_QUALITY` | JPEG 画质 (10-95) | `55` |
| `RD_SCALE` | 画面缩放 (0.1-1.0) | `0.75` |
| `RD_MONITOR` | 显示器编号 (1=主) | `1` |
| `RD_AGENT_TOKEN` | 代理认证令牌 | `change-this-...` |
| `RD_SESSION_EXPIRY` | 会话有效期(秒) | `28800` (8小时) |

### 修改账号密码

**方法一（环境变量，推荐）：**
```bash
set RD_USERS=admin:MyStrongPass,viewer:viewer123
python server.py
```

**方法二（生成哈希写入代码）：**
```bash
python config.py hash MyPassword
```
将输出的哈希字符串填入 `config.py` 的 `USERS` 字典。

### 调整画质与性能

- 局域网高画质：`set RD_FPS=25 && set RD_QUALITY=80 && set RD_SCALE=1.0`
- 跨网络省流量：`set RD_FPS=10 && set RD_QUALITY=40 && set RD_SCALE=0.6`

## 🖥️ 使用说明

1. 登录后进入控制台，画面自动开始传输
2. 在画面上**移动/点击鼠标**即可远程操控
3. **键盘按键**会自动转发到远程桌面
4. 底部输入框可**粘贴大段文字**到远程（回车发送）
5. 点击「键盘」按钮打开**虚拟键盘**（功能键、方向键等）
6. 点击「全屏」进入全屏模式
7. 顶部显示连接状态、FPS、分辨率

### 键盘注意事项

- 浏览器快捷键（Ctrl+R 刷新、Ctrl+L 地址栏、F12 开发者工具）仍归浏览器使用，不会转发
- 其他按键（含 F1-F11、方向键、Tab、功能键）会转发到远程桌面
- 组合键（Ctrl+C、Alt+Tab 等）正常工作

## 🔒 安全提示

- ⚠️ 默认使用 HTTP/WebSocket（明文）。**生产环境请务必使用 HTTPS/WSS**（可用 Nginx 反向代理 + Let's Encrypt）
- 默认密码 `admin123` **请立即修改**
- 中继模式的 `AGENT_TOKEN` **请修改为随机长字符串**
- 会话令牌存在内存中，服务器重启后需重新登录
- 建议配合防火墙限制访问来源 IP

### 用 Nginx 配置 HTTPS（示例）

```nginx
server {
    listen 443 ssl;
    server_name your.domain.com;
    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
    }
    location /ws {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
```

## 🐛 常见问题

**Q: 屏幕黑屏 / 没有画面？**
- 确认被控电脑没有锁屏（锁屏后 mss 可能抓不到画面）
- 检查 `RD_MONITOR` 是否选对了显示器

**Q: 鼠标点击位置偏移？**
- Windows 高 DPI 缩放可能导致坐标偏差，尝试在 Python 启动前调用 `ctypes.windll.shcore.SetProcessDpiAwareness(2)`，或调整 `RD_SCALE=1.0`

**Q: 连接频繁断开？**
- 跨网络时降低帧率和画质：`set RD_FPS=8 && set RD_QUALITY=35`
- 中继模式下检查代理令牌是否匹配

**Q: 键盘某些键无效？**
- 浏览器安全策略可能拦截部分组合键，可使用虚拟键盘面板

## 📋 工作原理

**直连模式：**
```
浏览器 ──HTTP/WebSocket──> server.py(被控电脑)
                            ├─ mss 采集屏幕 → JPEG 帧 → WebSocket → 浏览器
                            └─ WebSocket ← 鼠标/键盘命令 → pyautogui 执行
```

**中继模式：**
```
浏览器 ──> relay.py(公网) <── agent.py(被控电脑)
              │                    ├─ mss 采集屏幕 → 中继 → 浏览器
              │                    └─ 中继 ← 鼠标/键盘命令 → pyautogui 执行
              └─ 仅转发数据，不接触桌面
```

## 📄 依赖说明

- **FastAPI + Uvicorn**：Web 服务器与 WebSocket
- **mss**：高速屏幕截图
- **Pillow**：图像压缩
- **pyautogui**：鼠标键盘模拟
- **websockets**：代理端的 WebSocket 客户端
