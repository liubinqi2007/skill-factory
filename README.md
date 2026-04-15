# Skill Factory

> 基于 OpenCode 的 AI 技能生成与管理平台

Skill Factory 是一个智能化的 AI 技能（Skill）生成系统，通过自然语言交互，自动创建、管理和迭代 Claude Code Skills。用户只需描述需求，系统即可自动生成符合规范的 Skill 文档和配套脚本。

---

## 核心特性

### 🚀 智能生成
- **自然语言驱动**：用简单的描述即可创建完整的 Skill
- **自动创建脚本**：根据需求自动生成 Python 辅助脚本
- **规范保障**：严格遵循 Skill 创建规范，确保输出质量

### 💬 实时交互
- **流式响应**：实时展示 AI 思考过程和生成进度
- **多轮对话**：支持对已创建的 Skill 进行迭代优化
- **断线恢复**：WebSocket 断开后可恢复已生成内容

### ⚡ 高性能架构
- **服务池管理**：按需启停 OpenCode 实例，最多支持 10 个并发
- **智能复用**：同一 Skill 的对话复用同一 OpenCode 实例
- **自动清理**：空闲 5 分钟后自动释放资源

---

## 快速开始

### 环境要求

- Python 3.11+
- OpenCode CLI（需先安装并配置）
- Node.js 18+（仅开发前端时需要）

### 安装

```bash
# 克隆项目
git clone <repository-url>
cd skill-factory

# 安装 Python 依赖
pip install -r requirements.txt
```

### 启动服务

```bash
# 启动服务
./start.sh

# 停止服务
./stop.sh
```

启动后访问：http://127.0.0.1:8765

---

## 使用指南

### 创建 Skill

1. 打开 Web 界面
2. 输入你的需求，例如：
   ```
   帮我创建一个查询手机话费的技能，支持按手机号和账期查询
   ```
3. 系统会自动：
   - 分析需求并创建 Skill 工作区
   - 生成 SKILL.md 文档
   - 创建配套的 Python 脚本
   - 提供使用说明

### 迭代优化

对于已创建的 Skill，可以继续输入修改意见：

```
这个查询脚本需要添加支持按月份查询所有号码的账单汇总
```

系统会基于现有内容进行增量修改。

---

## 项目结构

```
skill-factory/
├── api.py                 # FastAPI 路由和 WebSocket 处理
├── main.py                # 应用入口
├── config.py              # 配置管理
├── models.py              # 数据模型定义
├── skill_manager.py       # Skill 生命周期管理
├── server_pool.py         # OpenCode 服务池管理
├── frontend/              # Web 前端
│   ├── index.html
│   ├── app.js
│   └── style.css
├── skills/                # 用户创建的 Skills
│   ├── 001/
│   ├── 002/
│   └── ...
├── logs/                  # 日志文件
├── start.sh               # 启动脚本
├── stop.sh                # 停止脚本
├── requirements.txt       # Python 依赖
└── AGENT_SYSTEM_PROMPT.md # AI 智能体系统提示词
```

---

## 技术架构

### 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| 主服务 | 8765 | FastAPI Web 服务 |
| OpenCode 池 | 54321-54330 | OpenCode 实例端口范围 |

### 核心组件

#### 1. API 层 (`api.py`)
- RESTful API 端点
- WebSocket 实时通信
- 流式响应处理

#### 2. Skill 管理器 (`skill_manager.py`)
- Skill 创建与持久化
- 对话历史管理
- OpenCode 流式调用

#### 3. 服务池 (`server_pool.py`)
- OpenCode 实例按需启动
- 空闲超时自动清理
- 进程健康检查

### 数据流

```
用户输入
    ↓
WebSocket 连接
    ↓
按需启动 OpenCode 实例
    ↓
SSE 流式接收 AI 响应
    ↓
实时推送至前端
    ↓
增量持久化到磁盘
```

---

## 配置说明

### 修改配置

编辑 `config.py`：

```python
@dataclass
class Settings:
    host: str = "127.0.0.1"              # 服务地址
    port: int = 8765                      # 服务端口
    opencode_base_port: int = 54321       # OpenCode 起始端口
    max_servers: int = 10                 # 最大并发实例数
    skills_dir: str = "skills"            # Skill 存储目录
```

### 环境变量

可通过环境变量覆盖配置：

```bash
export SKILL_FACTORY_HOST=0.0.0.0
export SKILL_FACTORY_PORT=9000
./start.sh
```

---

## API 文档

### RESTful 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web 界面 |
| GET | `/api/skills` | 列出所有 Skills |
| POST | `/api/skills` | 创建新 Skill |
| GET | `/api/skills/{id}` | 获取 Skill 详情 |
| DELETE | `/api/skills/{id}` | 删除 Skill |
| GET | `/api/skills/{id}/messages` | 获取对话历史 |
| GET | `/api/status` | 系统状态 |

### WebSocket

**端点**: `ws://127.0.0.1:8765/ws/chat/{skill_id}`

**消息类型**:
- `text` - 文本内容
- `thinking` - AI 思考过程
- `tool_status` - 工具调用状态
- `tool_detail` - 工具详情（write/bash/read/edit）
- `question` - AI 提问
- `done` - 完成

---

## 开发指南

### 添加新的前端功能

1. 编辑 `frontend/app.js`
2. 更新 `frontend/style.css`
3. 刷新浏览器即可（无需重启）

### 扩展 Skill 模板

编辑 `AGENT_SYSTEM_PROMPT.md` 修改 AI 行为规范。

### 调试

```bash
# 查看日志
tail -f logs/skill_factory.log

# 查看运行中的进程
ps aux | grep opencode

# 查看端口占用
lsof -i :8765
lsof -i :54321-54330
```

---

## 故障排查

### OpenCode 启动失败

**症状**: 创建 Skill 后长时间无响应

**解决**:
```bash
# 检查 OpenCode 是否安装
which opencode

# 检查端口占用
lsof -i :54321-54330

# 重启服务
./stop.sh && ./start.sh
```

### WebSocket 连接断开

**症状**: 界面显示已断开

**说明**: 正常行为，系统会：
1. 保留已生成内容到磁盘
2. 5 分钟内重连可恢复进度
3. 5 分钟后自动释放 OpenCode 实例

---

## 许可证

MIT License

---

## 贡献

欢迎提交 Issue 和 Pull Request！
