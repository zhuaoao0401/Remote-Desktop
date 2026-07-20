#!/bin/bash
# 查看中继服务器运行状态
cd "$(dirname "$0")"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

PID_FILE="relay.pid"
LOG_FILE="relay.log"

echo "================================================"
echo "  中继服务器状态"
echo "================================================"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    PID=$(cat "$PID_FILE")
    echo -e "运行状态: ${GREEN}运行中${NC} (PID: $PID)"
    echo "运行时间: $(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')"
    echo "内存占用: $(ps -o rss= -p "$PID" 2>/dev/null | awk '{printf "%.1f MB\n", $1/1024}')"
else
    echo -e "运行状态: ${RED}未运行${NC}"
fi

echo ""
echo "-------- 最近 15 行日志 --------"
tail -15 "$LOG_FILE" 2>/dev/null || echo "(无日志文件)"
