"""界面主题：深色可读、颜色和背景图都能改。

**测试盯的是"配置写坏了会不会把界面搞崩"**——用户手改 config.yaml 时
颜色少打一个 `#`、背景图路径写错，都是很常见的事，那时候该退回默认值，
而不是启动不了。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.config import Config  # noqa: E402
from cstudy.ui.theme import DARK, LIGHT, _rgba, load_appearance  # noqa: E402


def make_config(theme_cfg: dict | None = None) -> Config:
    return Config({"ui": {"theme": theme_cfg or {}}, "paths": {}}, Path("."))


class TestRgba:
    def test_converts_hex_to_rgba(self):
        assert _rgba("#252526", 0.9) == "rgba(37, 37, 38, 0.90)"

    def test_passes_through_bad_input(self):
        assert _rgba("天蓝色", 1.0) == "天蓝色"


class TestBareTheme:
    """不填任何配置时的默认值。"""

    def test_defaults_to_dark(self):
        appearance = load_appearance(make_config())

        assert appearance.theme.name == "dark"
        assert appearance.theme.bg == DARK.bg

    def test_no_background_image(self):
        appearance = load_appearance(make_config())

        assert not appearance.has_image
        assert appearance.theme.surface_alpha == 1.0, "没背景图时面板该是不透明的"


class TestMode:
    def test_light_mode(self):
        assert load_appearance(make_config({"mode": "light"})).theme.bg == LIGHT.bg

    def test_mode_is_case_insensitive(self):
        assert load_appearance(make_config({"mode": "DARK"})).theme.name == "dark"

    def test_unknown_mode_falls_back_to_dark(self):
        """填错了不报错、不崩溃，退回深色。"""
        assert load_appearance(make_config({"mode": "彩虹"})).theme.name == "dark"


class TestCustomColors:
    def test_overrides_a_single_color(self):
        appearance = load_appearance(make_config({"accent": "#ff0000"}))

        assert appearance.theme.accent == "#ff0000"
        assert appearance.theme.bg == DARK.bg, "没改的应该保持默认"

    def test_empty_values_keep_the_default(self):
        appearance = load_appearance(make_config({"accent": "   "}))

        assert appearance.theme.accent == DARK.accent

    def test_bad_color_falls_back(self):
        """少打一个 `#` 是最常见的笔误，不能因为这个起不来。"""
        appearance = load_appearance(make_config({"accent": "4a9eff"}))

        assert appearance.theme.accent == DARK.accent

    def test_three_digit_shorthand_is_expanded(self):
        """`#abc` 展开成 `#aabbcc`——QSS 对简写的支持看版本，展开了一劳永逸。"""
        assert load_appearance(make_config({"bg": "#abc"})).theme.bg == "#aabbcc"

    def test_non_hex_characters_are_rejected(self):
        assert load_appearance(make_config({"bg": "#zzzzzz"})).theme.bg == DARK.bg

    def test_wrong_length_is_rejected(self):
        assert load_appearance(make_config({"bg": "#12345"})).theme.bg == DARK.bg

    def test_light_mode_keeps_its_own_defaults(self):
        appearance = load_appearance(make_config({"mode": "light"}))

        assert appearance.theme.text == LIGHT.text


class TestBackgroundImage:
    def test_missing_file_is_ignored(self, tmp_path: Path):
        """路径写错不该崩，退回纯色背景就行。"""
        appearance = load_appearance(make_config({"background_image": str(tmp_path / "没有.png")}))

        assert not appearance.has_image

    def test_existing_file_is_used(self, tmp_path: Path):
        image = tmp_path / "bg.png"
        image.write_bytes(b"x")

        appearance = load_appearance(make_config({"background_image": str(image)}))

        assert appearance.has_image
        assert appearance.background_image == image

    def test_panels_become_translucent_for_the_image(self, tmp_path: Path):
        """有背景图时面板必须半透明——不然图被切成一块一块的。"""
        image = tmp_path / "bg.png"
        image.write_bytes(b"x")

        appearance = load_appearance(make_config({"background_image": str(image)}))

        assert appearance.theme.surface_alpha < 1.0

    def test_dim_can_be_tuned(self, tmp_path: Path):
        image = tmp_path / "bg.png"
        image.write_bytes(b"x")

        appearance = load_appearance(
            make_config({"background_image": str(image), "background_dim": 0.5})
        )

        assert appearance.dim == 0.5

    def test_dim_is_clamped(self, tmp_path: Path):
        image = tmp_path / "bg.png"
        image.write_bytes(b"x")

        too_big = load_appearance(
            make_config({"background_image": str(image), "background_dim": 9})
        )
        too_small = load_appearance(
            make_config({"background_image": str(image), "background_dim": -3})
        )

        assert too_big.dim == 1.0
        assert too_small.dim == 0.0

    def test_bad_dim_falls_back(self, tmp_path: Path):
        image = tmp_path / "bg.png"
        image.write_bytes(b"x")

        appearance = load_appearance(
            make_config({"background_image": str(image), "background_dim": "很暗"})
        )

        assert 0 <= appearance.dim <= 1


class TestStylesheet:
    def test_contains_the_theme_colors(self):
        sheet = DARK.stylesheet()

        assert DARK.bg in sheet
        assert DARK.accent in sheet

    def test_covers_the_widgets_we_actually_use(self):
        """漏掉哪个控件，它就会是刺眼的系统默认色。"""
        sheet = DARK.stylesheet()

        for widget in (
            "QPushButton",
            "QLineEdit",
            "QPlainTextEdit",
            "QTextBrowser",
            "QListWidget",
            "QComboBox",
            "QCheckBox",
            "QRadioButton",
            "QScrollBar",
            "QTabBar::tab",
            "QStatusBar",
        ):
            assert widget in sheet, f"{widget} 没设样式，会是系统默认外观"

    def test_alpha_shows_up_as_rgba(self):
        from dataclasses import replace

        sheet = replace(DARK, surface_alpha=0.9).stylesheet()

        assert "rgba(" in sheet

    def test_code_areas_stay_more_opaque_than_panels(self, tmp_path: Path):
        """图再好看也不能盖过代码的可读性——输入框/代码区要比面板实。"""
        image = tmp_path / "bg.png"
        image.write_bytes(b"x")

        theme = load_appearance(make_config({"background_image": str(image)})).theme

        assert theme.field_alpha > theme.surface_alpha
