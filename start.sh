#!/bin/bash

# Skill Factory 启动脚本
# 端口: 8765 (主服务), 54321-54330 (OpenCode 服务池)

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
LOG_FILE="$PROJECT_DIR/logs/skill_factory.log"

# 创建日志目录
mkdir -p "$(dirname "$LOG_FILE")"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}    Skill Factory 启动脚本${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# 检查是否已运行
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo -e "${YELLOW}⚠ 服务已在运行中 (PID: $OLD_PID)${NC}"
        echo -e "${YELLOW}如需重启，请先运行 ./stop.sh${NC}"
        echo ""
        echo -e "${GREEN}访问地址: http://127.0.0.1:$MAIN_PORT${NC}"
        exit 0
    else
        echo -e "${YELLOW}清理旧的 PID 文件...${NC}"
        rm -f "$PID_FILE"
    fi
fi

# 检查端口占用（只检测 LISTEN 状态，避免误检测客户端连接）
check_port() {
    local port=$1
    if lsof -ti ":$port" -sTCP:LISTEN > /dev/null 2>&1; then
        echo -e "${RED}❌ 端口 $port 已被占用${NC}"
        echo -e "${YELLOW}占用进程:${NC}"
        lsof -i ":$port" -sTCP:LISTEN | grep -v COMMAND || lsof -ti ":$port" -sTCP:LISTEN | xargs ps -p
        return 1
    fi
    return 0
}

# 检查主端口
if ! check_port "$MAIN_PORT"; then
    echo -e "${RED}启动失败！请先释放端口 $MAIN_PORT${NC}"
    exit 1
fi

# 检查 OpenCode 端口范围（只检测 LISTEN 状态）
echo -e "${YELLOW}检查 OpenCode 服务端口...${NC}"
PORTS_OCCUPIED=0
for port in $(seq $OPENCODE_BASE_PORT $((OPENCODE_BASE_PORT + 9))); do
    if lsof -ti ":$port" -sTCP:LISTEN > /dev/null 2>&1; then
        echo -e "${YELLOW}  ⚠ 端口 $port 已被占用${NC}"
        PORTS_OCCUPIED=$((PORTS_OCCUPIED + 1))
    fi
done

if [ $PORTS_OCCUPIED -gt 0 ]; then
    echo -e "${YELLOW}警告: $PORTS_OCCUPIED 个 OpenCode 端口已被占用${NC}"
    read -p "是否继续启动? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# 检查 Python 环境
echo -e "${YELLOW}检查 Python 环境...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ 未找到 Python 3${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 --version)
echo -e "${GREEN}✓ $PYTHON_VERSION${NC}"

# 检查依赖
echo -e "${YELLOW}检查依赖...${NC}"
if [ ! -f "requirements.txt" ]; then
    echo -e "${RED}❌ 未找到 requirements.txt${NC}"
    exit 1
fi

# 启动服务
echo ""
echo -e "${GREEN}正在启动 Skill Factory...${NC}"
echo -e "${GREEN}主服务端口: $MAIN_PORT${NC}"
echo -e "${GREEN}OpenCode 端口范围: $OPENCODE_BASE_PORT-$((OPENCODE_BASE_PORT + 9))${NC}"
echo ""

# 使用 nohup 后台启动
nohup python3 main.py > "$LOG_FILE" 2>&1 &
PID=$!

# 等待服务启动
echo -e "${YELLOW}等待服务启动...${NC}"
sleep 3

# 检查进程是否存在
if ps -p $PID > /dev/null 2>&1; then
    echo $PID > "$PID_FILE"
    echo -e "${GREEN}✓ 服务启动成功！${NC}"
    echo -e "${GREEN}  PID: $PID${NC}"
    echo -e "${GREEN}  日志: $LOG_FILE${NC}"
    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}    服务已启动${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    echo -e "${GREEN}🌐 访问地址: http://127.0.0.1:$MAIN_PORT${NC}"
    echo -e "${GREEN}📊 API 状态: http://127.0.0.1:$MAIN_PORT/api/status${NC}"
    echo ""
    echo -e "${YELLOW}查看日志: tail -f $LOG_FILE${NC}"
    echo -e "${YELLOW}停止服务: ./stop.sh${NC}"
else
    echo -e "${RED}❌ 服务启动失败${NC}"
    echo -e "${YELLOW}查看日志: cat $LOG_FILE${NC}"
    exit 1
fi
