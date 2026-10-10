"""B 站字幕获取的测试。

重点是**错误路径**：字幕下载失败必须变成说人话的 BilibiliError（不能让用户
看到 requests 的英文报错），而且不能因为字幕接口抽风就让整个导入失败——
要能退到本地语音转写。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests
from bilibili_api import video as bili_video
from bilibili_api.exceptions import ApiException, NetworkException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy import pipeline as pipeline_module  # noqa: E402
from cstudy.bilibili import video as video_module  # noqa: E402
from cstudy.bilibili.video import (  # noqa: E402
    BilibiliError,
    VideoInfo,
    _download_subtitle_body,
    _extract_subtitle_entries,
    fetch_subtitle,
)
from cstudy.config import Config  # noqa: E402
from cstudy.pipeline import PipelineError, StudyPipeline  # noqa: E402
from cstudy.subtitle import Segment  # noqa: E402

SUBTITLE_URL = "https://aisubtitle.hdslb.com/bfs/subtitle/abc.json"


def make_config(transcribe_enabled: bool = True, **bilibili_overrides) -> Config:
    """最小可用的配置。max_retries 默认设成 1，免得测试真的去 sleep 重试。"""
    raw = {
        "paths": {
            "audio": "data/audio",
            "database": "data/t.db",
            "notes": "notes",
            "cookies": "data/cookies/bilibili.json",
        },
        "bilibili": {"max_retries": 1, **bilibili_overrides},
        "transcribe": {"enabled": transcribe_enabled},
    }
    return Config(raw, Path("."))


def make_info(cid: int = 12345) -> VideoInfo:
    return VideoInfo(
        bvid="BV1xx411c7mD",
        page=1,
        title="示例视频",
        cid=cid,
        duration=600,
        url="https://www.bilibili.com/video/BV1xx411c7mD",
    )


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, bad_json: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")


class FakeDatabase:
    """只提供 _resolve_subtitle 用到的那几个方法。"""

    def __init__(self, cached: list[Segment] | None = None) -> None:
        self._cached = cached or []
        self.saved: list[tuple[int, str, list[Segment]]] = []

    def get_subtitle_segments(self, video_id: int):
        return self._cached

    def save_subtitle(self, video_id: int, source: str, segments: list[Segment]) -> None:
        self.saved.append((video_id, source, segments))


class TestDownloadSubtitleBody:
    def test_returns_body_on_success(self, monkeypatch):
        payload = {"body": [{"from": 0.0, "content": "第一句"}]}
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, payload))
        assert _download_subtitle_body(SUBTITLE_URL, make_config()) == payload["body"]

    def test_client_error_becomes_human_readable(self, monkeypatch):
        # 曾经的 bug：raise_for_status 在 _retry 外面，用户看到裸的 HTTPError
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(404))
        with pytest.raises(BilibiliError) as exc:
            _download_subtitle_body(SUBTITLE_URL, make_config())
        message = str(exc.value)
        assert "404" in message
        assert "不存在" in message
        assert "Client Error" not in message  # 不能把 requests 的英文原样丢给用户

    def test_server_error_is_retried_then_reported(self, monkeypatch):
        # 关键是要真的证明"重试了"：max_retries 必须 >1，
        # 否则"重试了"和"没重试"两种实现下调用次数都一样，测试会假阳性。
        calls = []

        def fake_get(*args, **kwargs):
            calls.append(1)
            return FakeResponse(503)

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(video_module.time, "sleep", lambda _: None)  # 别真等 9 秒

        with pytest.raises(BilibiliError) as exc:
            _download_subtitle_body(SUBTITLE_URL, make_config(max_retries=3))

        assert len(calls) == 3  # 5xx 确实重试了三次
        assert "已重试 3 次" in str(exc.value)  # 走的是"重试耗尽"那条路径

    def test_invalid_json_becomes_human_readable(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, bad_json=True))
        with pytest.raises(BilibiliError) as exc:
            _download_subtitle_body(SUBTITLE_URL, make_config())
        assert "不是预期格式" in str(exc.value)

    def test_unexpected_shape_returns_empty(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, {"body": "oops"}))
        assert _download_subtitle_body(SUBTITLE_URL, make_config()) == []

    def test_cdn_403_does_not_blame_login(self, monkeypatch):
        """字幕 CDN 的 403 是临时地址失效，不是登录态问题。

        提示错了会把用户引去白扫一次码（接口的 403 才是登录态问题）。
        """
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(403))
        with pytest.raises(BilibiliError) as exc:
            _download_subtitle_body(SUBTITLE_URL, make_config())
        message = str(exc.value)
        assert "重新导入" in message
        assert "扫码登录" not in message


class TestExtractSubtitleEntries:
    def test_nested_shape(self):
        payload = {"subtitle": {"subtitles": [{"lan": "zh-CN"}]}}
        assert _extract_subtitle_entries(payload) == [{"lan": "zh-CN"}]

    def test_flat_shape(self):
        assert _extract_subtitle_entries({"subtitles": [{"lan": "en"}]}) == [{"lan": "en"}]

    def test_empty_or_wrong_type(self):
        assert _extract_subtitle_entries({}) == []
        assert _extract_subtitle_entries(None) == []
        assert _extract_subtitle_entries("nonsense") == []


class TestFetchSubtitle:
    def test_without_cid_returns_none(self):
        assert fetch_subtitle(make_info(cid=0), config=make_config()) is None

    def test_without_entries_returns_none(self, monkeypatch):
        class FakeVideo:
            def __init__(self, *args, **kwargs):
                pass

            def get_subtitle(self, cid):
                return {"subtitles": []}

        monkeypatch.setattr("cstudy.bilibili.video.bili_video.Video", FakeVideo)
        assert fetch_subtitle(make_info(), config=make_config()) is None

    def test_builds_segments_and_prefers_chinese(self, monkeypatch):
        class FakeVideo:
            def __init__(self, *args, **kwargs):
                pass

            def get_subtitle(self, cid):
                return {
                    "subtitles": [
                        {"lan": "en-US", "subtitle_url": "//example.com/en.json"},
                        {"lan": "zh-CN", "subtitle_url": "//example.com/zh.json"},
                    ]
                }

        used_urls = []

        def fake_get(url, *args, **kwargs):
            used_urls.append(url)
            return FakeResponse(200, {"body": [{"from": 1.5, "content": "你好"}, {"from": 3.0, "content": "  "}]})

        monkeypatch.setattr("cstudy.bilibili.video.bili_video.Video", FakeVideo)
        monkeypatch.setattr(requests, "get", fake_get)

        segments = fetch_subtitle(make_info(), config=make_config())
        assert used_urls == ["https://example.com/zh.json"]  # 中文优先，且补了 https:
        assert segments == [Segment(start=1.5, text="你好")]  # 空内容被过滤

    def test_skips_malformed_entries_instead_of_crashing(self, monkeypatch):
        """字幕文件偶尔有脏数据，丢一条就行，不能让整个导入崩掉。"""

        class FakeVideo:
            def __init__(self, *args, **kwargs):
                pass

            def get_subtitle(self, cid):
                return {"subtitles": [{"lan": "zh-CN", "subtitle_url": "https://e.com/zh.json"}]}

        body = [
            {"from": 0.0, "content": "正常一条"},
            {"from": None, "content": "时间戳坏了"},  # float(None) 会炸
            "这根本不是字典",
            {"from": 2.0, "content": "   "},  # 空内容
            {"content": "没有 from 字段"},
        ]
        monkeypatch.setattr("cstudy.bilibili.video.bili_video.Video", FakeVideo)
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(200, {"body": body}))

        segments = fetch_subtitle(make_info(), config=make_config())
        assert segments == [
            Segment(0.0, "正常一条"),
            Segment(0.0, "时间戳坏了"),
            Segment(0.0, "没有 from 字段"),
        ]


class TestLibraryExceptionsAreTranslated:
    """bilibili_api 抛的是 NetworkException / ApiException，不是我们的 BilibiliError。

    漏接的话，界面会显示"出现了预期之外的错误（NetworkException）"这种中英夹杂的
    东西，而且 pipeline 的降级回退也接不住，整个导入会失败。
    """

    @staticmethod
    def _raise_from_get_subtitle(exc):
        def fake_get_subtitle(self, cid):
            raise exc

        return fake_get_subtitle

    def test_network_4xx_translated_without_retry(self, monkeypatch):
        calls = []

        def fake_get_subtitle(self, cid):
            calls.append(1)
            raise NetworkException(403, "Forbidden")

        monkeypatch.setattr(bili_video.Video, "get_subtitle", fake_get_subtitle)
        with pytest.raises(BilibiliError) as exc:
            fetch_subtitle(make_info(), config=make_config(max_retries=3))

        message = str(exc.value)
        assert len(calls) == 1  # 403 重试没意义
        assert "403" in message
        assert "扫码登录" in message  # 说人话，而不是 "网络错误，状态码：403 - Forbidden"

    def test_network_5xx_is_retried_then_translated(self, monkeypatch):
        calls = []

        def fake_get_subtitle(self, cid):
            calls.append(1)
            raise NetworkException(503, "Service Unavailable")

        monkeypatch.setattr(bili_video.Video, "get_subtitle", fake_get_subtitle)
        monkeypatch.setattr(video_module.time, "sleep", lambda _: None)

        with pytest.raises(BilibiliError) as exc:
            fetch_subtitle(make_info(), config=make_config(max_retries=3))

        assert len(calls) == 3  # 5xx 重试了三次
        # 重试耗尽后要说是 B 站的问题，不能反过来让用户去查自己的网络
        message = str(exc.value)
        assert "B 站服务器出问题" in message
        assert "检查网络" not in message

    def test_other_api_exception_is_translated(self, monkeypatch):
        monkeypatch.setattr(
            bili_video.Video,
            "get_subtitle",
            self._raise_from_get_subtitle(ApiException("库内部出了点事")),
        )
        with pytest.raises(BilibiliError) as exc:
            fetch_subtitle(make_info(), config=make_config())
        assert "库内部出了点事" in str(exc.value)

    def test_rate_limit_gets_its_own_hint(self, monkeypatch):
        monkeypatch.setattr(
            bili_video.Video,
            "get_subtitle",
            self._raise_from_get_subtitle(NetworkException(429, "Too Many Requests")),
        )
        with pytest.raises(BilibiliError) as exc:
            fetch_subtitle(make_info(), config=make_config())
        assert "限流" in str(exc.value)
        assert "等几分钟" in str(exc.value)


class TestResolveSubtitleFallback:
    """字幕接口报错时必须退到语音转写，而不是让整个导入失败。"""

    def _pipeline(self, monkeypatch, db, transcribe_result):
        monkeypatch.setattr(
            pipeline_module, "download_audio", lambda *a, **k: Path("fake.wav")
        )

        class FakeTranscriber:
            def __init__(self, config):
                pass

            def transcribe(self, audio_path, on_progress=None):
                return transcribe_result

        monkeypatch.setattr(pipeline_module, "Transcriber", FakeTranscriber)
        return StudyPipeline(make_config(), db)

    def test_falls_back_when_subtitle_raises(self, monkeypatch):
        db = FakeDatabase()

        def boom(*args, **kwargs):
            raise BilibiliError("查询字幕失败，已重试 3 次，请检查网络。")

        monkeypatch.setattr(pipeline_module, "fetch_subtitle", boom)

        expected = [Segment(0.0, "转写出来的内容")]
        pipe = self._pipeline(monkeypatch, db, expected)

        messages: list[str] = []
        segments, source = pipe._resolve_subtitle(1, make_info(), credential=object(), on_progress=messages.append)

        assert source == "whisper"
        assert segments == expected
        assert any("改用本地语音转写" in m for m in messages)
        assert db.saved == [(1, "whisper", expected)]

    def test_falls_back_on_unexpected_error_too(self, monkeypatch):
        """不只是 BilibiliError——字幕这步任何意外都不该拖垮整个导入。"""
        db = FakeDatabase()

        def boom(*args, **kwargs):
            raise TypeError("模拟字幕解析里没预料到的 bug")

        monkeypatch.setattr(pipeline_module, "fetch_subtitle", boom)

        expected = [Segment(0.0, "转写内容")]
        pipe = self._pipeline(monkeypatch, db, expected)
        segments, source = pipe._resolve_subtitle(
            1, make_info(), credential=object(), on_progress=lambda _: None
        )

        assert source == "whisper"
        assert segments == expected

    def test_disabled_transcribe_blames_subtitle_failure_not_the_video(self, monkeypatch):
        """抓字幕失败时不能提示用户"换个带字幕的视频"，那是错误的方向。"""
        db = FakeDatabase()

        def boom(*args, **kwargs):
            raise BilibiliError("请求太频繁，被 B 站限流了。")

        monkeypatch.setattr(pipeline_module, "fetch_subtitle", boom)
        pipe = StudyPipeline(make_config(transcribe_enabled=False), db)

        with pytest.raises(PipelineError) as exc:
            pipe._resolve_subtitle(1, make_info(), credential=object(), on_progress=lambda _: None)

        message = str(exc.value)
        assert "被 B 站限流了" in message  # 带上真实原因
        assert "换个带字幕的视频" not in message  # 不能误导用户

    def test_disabled_transcribe_with_no_subtitle_says_so(self, monkeypatch):
        db = FakeDatabase()
        monkeypatch.setattr(pipeline_module, "fetch_subtitle", lambda *a, **k: None)
        pipe = StudyPipeline(make_config(transcribe_enabled=False), db)

        with pytest.raises(PipelineError) as exc:
            pipe._resolve_subtitle(1, make_info(), credential=object(), on_progress=lambda _: None)

        assert "没有可用的 CC 字幕" in str(exc.value)

    def test_disabled_transcribe_blames_missing_login(self, monkeypatch):
        """未登录也是一种"抓不到字幕"，提示里要说清楚，别让用户去换视频。"""
        db = FakeDatabase()
        pipe = StudyPipeline(make_config(transcribe_enabled=False), db)

        with pytest.raises(PipelineError) as exc:
            pipe._resolve_subtitle(1, make_info(), credential=None, on_progress=lambda _: None)

        message = str(exc.value)
        assert "没有登录 B 站" in message
        assert "换个带字幕的视频" not in message

    def test_uses_cc_subtitle_when_available(self, monkeypatch):
        db = FakeDatabase()
        expected = [Segment(0.0, "CC 字幕内容")]
        monkeypatch.setattr(pipeline_module, "fetch_subtitle", lambda *a, **k: expected)

        pipe = StudyPipeline(make_config(), db)
        segments, source = pipe._resolve_subtitle(7, make_info(), credential=object(), on_progress=lambda _: None)

        assert source == "bilibili_cc"
        assert segments == expected
        assert db.saved == [(7, "bilibili_cc", expected)]

    def test_cached_subtitle_wins(self, monkeypatch):
        cached = [Segment(0.0, "缓存内容")]
        db = FakeDatabase(cached=cached)

        def should_not_run(*args, **kwargs):
            raise AssertionError("有缓存就不该再去请求 B 站")

        monkeypatch.setattr(pipeline_module, "fetch_subtitle", should_not_run)
        # 兜底也 patch 掉：万一缓存逻辑坏了，测试要干脆地失败，
        # 而不是掉进真实网络请求里卡住。
        monkeypatch.setattr(pipeline_module, "download_audio", should_not_run)
        monkeypatch.setattr(pipeline_module, "Transcriber", should_not_run)
        pipe = StudyPipeline(make_config(), db)
        segments, source = pipe._resolve_subtitle(3, make_info(), credential=object(), on_progress=lambda _: None)

        assert source == "cache"
        assert segments == cached


class TestLoadPassesCredential:
    """有些视频不登录连标题都拿不到，所以 credential 必须传下去。"""

    def test_credential_reaches_fetch_video_info(self, monkeypatch):
        captured = {}
        fake_credential = object()

        def fake_fetch(bvid, page, credential=None, config=None):
            captured["credential"] = credential
            return make_info()

        db = FakeDatabase()
        monkeypatch.setattr(pipeline_module, "fetch_video_info", fake_fetch)
        monkeypatch.setattr(pipeline_module, "load_credential", lambda path: fake_credential)
        monkeypatch.setattr(
            pipeline_module,
            "fetch_subtitle",
            lambda *a, **k: [Segment(0.0, "字幕")],
        )

        class FakeDB(FakeDatabase):
            def upsert_video(self, **kwargs):
                return 1

        pipe = StudyPipeline(make_config(), FakeDB())
        pipe.load("https://www.bilibili.com/video/BV1xx411c7mD")

        assert captured["credential"] is fake_credential
