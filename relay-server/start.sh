#!/bin/bash
# ============================================================
#  远程桌面 - 中继服务器一键启动脚本
#  用法:
#    ./start.sh              使用默认端口 8766
#    ./start.sh 9000         指定端口
#  停止: ./stop.sh
# ============================================================
set -e

# 切换到脚本所在目录
cd "$(dirname "$0")"

PORT="${1:-8766}"
VENV_DIR="venv"
LOG_FILE="relay.log"
PID_FILE="relay.pid"

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo "================================================"
echo "  远程桌面 - 中继服务器"
echo "================================================"

# ---- 1. 检查是否已在运行 ----
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo -e "${YELLOW}中继服务器已在运行 (PID: $(cat "$PID_FILE"))${NC}"
    echo "如需重启，请先运行: ./stop.sh"
    exit 0
fi

# ---- 2. 检查 Python ----
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo -e "${RED}错误: 未找到 Python，请先安装 Python 3.8+${NC}"
    exit 1
fi
echo "Python: $($PYTHON --version 2>&1)"

# ---- 3. 创建/激活虚拟环境 ----
if [ ! -d "$VENV_DIR" ]; then
    echo "正在创建虚拟环境..."
    $PYTHON -m venv "$VENV_DIR"
fi
PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

# ---- 4. 安装依赖 ----
echo "正在安装/更新依赖..."
$PIP install --upgrade pip -q 2>/dev/null || true
$PIP install -r requirements.txt -q
echo -e "${GREEN}依赖安装完成${NC}"

# ---- 5. 后台启动 ----
echo "正在启动中继服务器 (端口 $PORT)..."
nohup "$PYTHON" relay.py --port "$PORT" > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# 等待启动
sleep 2

# ---- 6. 检查启动结果 ----
if kill -0 "$SERVER_PID" 2>/dev/null; then
    # 尝试获取公网 IP
    PUBLIC_IP=$(curl -s --connect-timeout 3 ifconfig.me 2>/dev/null || \
                curl -s --connect-timeout 3 ipinfo.io/ip 2>/dev/null || \
                echo "<服务器公网IP>")

    echo -e "${GREEN}================================================${NC}"
    echo -e "${GREEN}  启动成功！${NC}"
    echo -e "${GREEN}================================================${NC}"
    echo "  PID        : $SERVER_PID"
    echo "  端口       : $PORT"
    echo "  日志文件   : $LOG_FILE"
    echo "  虚拟环境   : $VENV_DIR"
    echo ""
    echo "  -------- 访问信息 --------"
    echo "  Web 访问   : http://$PUBLIC_IP:$PORT"
    echo "  登录账号   : admin / admin123"
    echo "  代理令牌   : (见 config.py 的 AGENT_TOKEN)"
    echo ""
    echo "  -------- 被控电脑 --------"
    echo "  在被控电脑运行:"
    echo "    python agent.py --relay ws://$PUBLIC_IP:$PORT"
    echo ""
    echo "  停止服务   : ./stop.sh"
    echo "  查看日志   : tail -f $LOG_FILE"
    echo -e "${GREEN}================================================${NC}"
else
    echo -e "${RED}启动失败！请查看日志: $LOG_FILE${NC}"
    rm -f "$PID_FILE"
    echo "--- 最后 20 行日志 ---"
    tail -20 "$LOG_FILE" 2>/dev/null || true
    exit 1
fi
