"""配置加载的测试。重点是路径解析和缺密钥时的报错信息。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.config import Config, ConfigError, load_config  # noqa: E402

RAW = {
    "app": {"name": "测试"},
    "paths": {
        "database": "data/test.db",
        "notes": "notes",
        "logs": "logs",
        "cookies": "data/cookies/bilibili.json",
        "audio": "data/audio",
    },
    "ai": {"model": "deepseek-flash", "base_url": "https://api.deepseek.com"},
}


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(RAW, tmp_path)


class TestGet:
    def test_dotted_lookup(self, config: Config):
        assert config.get("ai.model") == "deepseek-flash"

    def test_missing_returns_default(self, config: Config):
        assert config.get("ai.nonexistent", "兜底") == "兜底"
        assert config.get("nope.deep.key", 42) == 42

    def test_non_dict_in_path_returns_default(self, config: Config):
        # ai.model 是字符串，再往下取应该安全返回默认值而不是抛异常
        assert config.get("ai.model.deeper", "兜底") == "兜底"


class TestPaths:
    def test_relative_path_becomes_absolute(self, config: Config, tmp_path: Path):
        assert config.path("database") == tmp_path / "data" / "test.db"

    def test_directory_path(self, config: Config, tmp_path: Path):
        assert config.path("notes") == tmp_path / "notes"

    def test_missing_key_raises(self, config: Config):
        with pytest.raises(ConfigError, match="缺少 paths"):
            config.path("nonexistent")

    def test_ensure_dirs_creates_directories_not_files(self, config: Config, tmp_path: Path):
        config.ensure_dirs()
        assert (tmp_path / "notes").is_dir()
        assert (tmp_path / "logs").is_dir()
        assert (tmp_path / "data" / "audio").is_dir()
        # cookies 配的是文件名，应该建父目录而不是同名目录
        assert (tmp_path / "data" / "cookies").is_dir()
        assert not (tmp_path / "data" / "cookies" / "bilibili.json").exists()


class TestApiKey:
    def test_missing_key_raises_readable_error(self, config: Config, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(ConfigError, match="没有找到 DeepSeek 密钥"):
            _ = config.api_key

    def test_blank_key_treated_as_missing(self, config: Config, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "   ")
        with pytest.raises(ConfigError):
            _ = config.api_key

    def test_present_key_is_returned(self, config: Config, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        assert config.api_key == "sk-test"


class TestLoadConfig:
    def test_missing_file_raises_readable_error(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="找不到配置文件"):
            load_config(tmp_path / "nope.yaml")

    def test_non_dict_root_raises(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text("- 这是一个列表\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="根节点应该是一个字典"):
            load_config(path)

    def test_real_config_file_loads(self):
        """项目里真实的 config.yaml 必须是合法的。"""
        real = Path(__file__).resolve().parents[1] / "config.yaml"
        assert real.exists(), "项目根目录缺少 config.yaml"
        parsed = yaml.safe_load(real.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        # 这几项是代码硬依赖的，缺了会出问题
        for key in ("database", "notes", "logs", "cookies", "audio"):
            assert key in parsed["paths"], f"config.yaml 缺少 paths.{key}"
        assert parsed["ai"]["model"]
