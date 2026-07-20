#!/bin/bash
# ============================================================
#  远程桌面 - 中继服务器停止脚本
#  用法: ./stop.sh
# ============================================================
cd "$(dirname "$0")"

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

PID_FILE="relay.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "未找到 PID 文件 ($PID_FILE)，服务器可能未运行"
    # 尝试通过进程名查找
    PIDS=$(pgrep -f "relay.py" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "发现 relay.py 进程: $PIDS"
        for pid in $PIDS; do
            kill "$pid" 2>/dev/null && echo "已终止进程 $pid"
        done
    fi
    exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID" 2>/dev/null || true
    fi
    echo -e "${GREEN}已停止中继服务器 (PID: $PID)${NC}"
else
    echo -e "${YELLOW}进程 $PID 已不存在${NC}"
fi
rm -f "$PID_FILE"
