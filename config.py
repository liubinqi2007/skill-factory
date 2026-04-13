from dataclasses import dataclass, field


@dataclass
class Settings:
    # FastAPI 服务配置
    host: str = "127.0.0.1"
    port: int = 8765

    # OpenCode 服务器池配置
    opencode_base_port: int = 54321
    max_servers: int = 10  # 最大并发 OpenCode 实例数

    # OpenCode SDK 配置
    opencode_model_id: str = "claude-sonnet-4-20250514"
    opencode_provider_id: str = "anthropic"

    # Skill 存储路径（相对于项目根目录）
    skills_dir: str = "skills"

    # Mock 模式：无 OpenCode 服务器时使用模拟响应
    mock_mode: bool = False

    # skill-creator 技能路径
    skill_creator_prompt: str = (
        "使用 skill-creator 技能帮我创建一个新的 skill。"
        "请按照 skill-creator 的标准流程：先理解需求，"
        "然后编写 SKILL.md，包含 name、description 和完整指令。"
    )

    @property
    def opencode_ports(self) -> list[int]:
        """可用的 OpenCode 端口列表"""
        return [
            self.opencode_base_port + i for i in range(self.max_servers)
        ]


settings = Settings()
