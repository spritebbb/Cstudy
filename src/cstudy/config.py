"""统一配置入口：读 config.yaml（非敏感）+ .env（密钥）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# src/cstudy/config.py -> src/cstudy -> src -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIG_FILE = PROJECT_ROOT / "config.yaml"


class ConfigError(RuntimeError):
    """配置缺失或格式错误。消息会直接显示给用户，所以要说人话。"""


class Config:
    def __init__(self, raw: dict[str, Any], root: Path) -> None:
        self._raw = raw
        self.root = root

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """按 'ai.model' 这种点号路径取值，取不到返回 default。"""
        node: Any = self._raw
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, key: str) -> Path:
        """取 paths.<key> 并转成绝对路径。"""
        raw = self.get(f"paths.{key}")
        if not raw:
            raise ConfigError(f"config.yaml 里缺少 paths.{key}")
        p = Path(raw)
        return p if p.is_absolute() else self.root / p

    def ensure_dirs(self) -> None:
        """把需要写入的目录都建好。

        paths 里有的配的是目录（notes、logs、audio），有的是文件名
        （database、cookies）。带后缀的按文件处理，建它的父目录。

        `temp_bank`（「考前突击」那一栏自己的库）是**可选**的：
        老配置文件里没有这一项、或者只带 `{"paths": {}}` 的最小配置，
        都不该让软件起不来——所以它缺了只是跳过，别的键缺了照旧报错
        （那些是硬依赖，缺了会在别处出更难看的问题）。
        """
        for key in ("notes", "logs", "cookies", "audio", "database", "temp_bank"):
            if not self.get(f"paths.{key}"):
                continue
            target = self.path(key)
            directory = target.parent if target.suffix else target
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def api_key(self) -> str:
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise ConfigError(
                "没有找到 DeepSeek 密钥。请在项目根目录的 .env 文件里填写：\n"
                "    DEEPSEEK_API_KEY=sk-你的密钥\n"
                "密钥在 https://platform.deepseek.com 申请。"
            )
        return key

    @property
    def ai_base_url(self) -> str:
        return self.get("ai.base_url", "https://api.deepseek.com")

    @property
    def ai_model(self) -> str:
        return self.get("ai.model", "deepseek-flash")


def load_config(config_file: Path | None = None) -> Config:
    """加载配置。找不到 config.yaml 会抛出说人话的错误。"""
    cfg_file = config_file or CONFIG_FILE
    if not cfg_file.exists():
        raise ConfigError(f"找不到配置文件：{cfg_file}")

    with cfg_file.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件格式不对，根节点应该是一个字典：{cfg_file}")

    # .env 里的值只在环境变量里缺失时才生效，方便临时用环境变量覆盖
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    cfg = Config(raw, cfg_file.parent)
    cfg.ensure_dirs()
    return cfg


_config: Config | None = None


def get_config() -> Config:
    """全局单例，避免到处重复读文件。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config
