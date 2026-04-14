#!/bin/bash

# Skill Factory 停止脚本
# 停止主服务和所有 OpenCode 子服务

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 配置
MAIN_PORT=8765
OPENCODE_BASE_PORT=54321
PID_FILE="$PROJECT_DIR/.skill_factory.pid"

echo -e "${RED}========================================${NC}"
echo -e "${RED}    Skill Factory 停止脚本${NC}"
echo -e "${RED}========================================${NC}"
echo ""

# 停止主服务
stop_main_service() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo -e "${YELLOW}停止主服务 (PID: $PID)...${NC}"
            kill "$PID"

            # 等待进程结束
            for i in {1..10}; do
                if ! ps -p "$PID" > /dev/null 2>&1; then
                    echo -e "${GREEN}✓ 主服务已停止${NC}"
                    rm -f "$PID_FILE"
                    return 0
                fi
                sleep 1
            done

            # 强制结束
            echo -e "${YELLOW}强制结束主服务...${NC}"
            kill -9 "$PID" 2>/dev/null || true
            rm -f "$PID_FILE"
        else
            echo -e "${YELLOW}主服务进程不存在，清理 PID 文件${NC}"
            rm -f "$PID_FILE"
        fi
    else
        echo -e "${YELLOW}未找到 PID 文件，尝试通过端口查找...${NC}"
    fi
}

# 通过端口停止监听进程（只找 LISTEN 状态的 PID，避免误杀连接方）
stop_by_port() {
    local port=$1
    local name=$2
    local pid=$(lsof -ti ":$port" -sTCP:LISTEN 2>/dev/null)

    if [ -n "$pid" ]; then
        echo -e "${YELLOW}停止 $name (端口 $port, PID: $pid)...${NC}"
        kill "$pid" 2>/dev/null || true
        sleep 1

        # 如果还在运行，强制结束
        local still_pid=$(lsof -ti ":$port" -sTCP:LISTEN 2>/dev/null)
        if [ -n "$still_pid" ]; then
            kill -9 "$still_pid" 2>/dev/null || true
        fi
        echo -e "${GREEN}✓ $name 已停止${NC}"
    fi
}

# 停止所有 OpenCode 服务
stop_opencode_services() {
    echo ""
    echo -e "${YELLOW}停止 OpenCode 服务池...${NC}"

    local count=0
    for port in $(seq $OPENCODE_BASE_PORT $((OPENCODE_BASE_PORT + 9))); do
        local pid=$(lsof -ti ":$port" -sTCP:LISTEN 2>/dev/null)
        if [ -n "$pid" ]; then
            echo -e "${YELLOW}  停止 OpenCode 实例 (端口 $port, PID: $pid)${NC}"
            kill "$pid" 2>/dev/null || true
            count=$((count + 1))
        fi
    done

    if [ $count -eq 0 ]; then
        echo -e "${YELLOW}  没有运行中的 OpenCode 实例${NC}"
    else
        # 等待进程结束
        sleep 2
        # 强制清理残留
        for port in $(seq $OPENCODE_BASE_PORT $((OPENCODE_BASE_PORT + 9))); do
            local pid=$(lsof -ti ":$port" -sTCP:LISTEN 2>/dev/null)
            if [ -n "$pid" ]; then
                echo -e "${YELLOW}  强制停止残留进程 (端口 $port, PID: $pid)${NC}"
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
        echo -e "${GREEN}✓ 已停止 $count 个 OpenCode 实例${NC}"
    fi

    # 额外清理：通过进程名查找所有残留的 opencode serve 进程
    local stale=$(pgrep -f "opencode serve --port" 2>/dev/null || true)
    if [ -n "$stale" ]; then
        echo -e "${YELLOW}  清理残留 opencode 进程: $stale${NC}"
        echo "$stale" | xargs kill 2>/dev/null || true
        sleep 1
        echo "$stale" | xargs kill -9 2>/dev/null || true
        echo -e "${GREEN}✓ 残留进程已清理${NC}"
    fi
}

# 执行停止
echo -e "${YELLOW}1. 停止主服务...${NC}"
stop_main_service

echo ""
echo -e "${YELLOW}2. 清理端口 $MAIN_PORT...${NC}"
stop_by_port "$MAIN_PORT" "主服务"

echo ""
echo -e "${YELLOW}3. 停止所有 OpenCode 服务...${NC}"
stop_opencode_services

# 清理临时文件
echo ""
echo -e "${YELLOW}4. 清理临时文件...${NC}"
rm -f "$PROJECT_DIR/.skill_factory.pid"
echo -e "${GREEN}✓ 临时文件已清理${NC}"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}    所有服务已停止${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${YELLOW}重新启动: ./start.sh${NC}"
