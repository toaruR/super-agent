"""harness/extract/fetch.py の単体テスト。

- fetch_rendered_page() が design_extract.yaml の breakpoints ごとに独立した
  DOM(outerHTML相当)/computed styleを取得すること。
- robots.txt で禁止されたURLに対しては、ブラウザドライバに一切アクセスせず
  fetchをスキップすること。
- ブラウザ操作が BrowserDriver インターフェース越しに呼び出され、実ブラウザなしに
  フェイク/モック実装で差し替え可能であること。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import pytest

from harness.extract.fetch import (
    BreakpointCapture,
    BrowserDriver,
    PageFetchResult,
    RenderResult,
    RobotsDisallowedError,
    fetch_rendered_page,
)
from harness.extract.robots import RobotsChecker

ALLOW_ALL_ROBOTS_TXT = "User-agent: *\nAllow: /\n"
DISALLOW_ALL_ROBOTS_TXT = "User-agent: *\nDisallow: /\n"


class FakeBrowserDriver:
    """BrowserDriver を満たすフェイク実装（実ブラウザを一切起動しない）。"""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, int]] = []

    def render(self, url: str, viewport_width: int) -> RenderResult:
        self.calls.append((url, viewport_width))
        return RenderResult(
            outer_html=f"<html data-url='{url}' data-width='{viewport_width}'></html>",
            computed_styles={
                "body": {"font-size": f"{viewport_width // 100}px", "color": "#111"},
            },
        )


class RecordingRefusingDriver:
    """呼び出されたら即座に失敗させる、fetchが一切走らないことを検証するためのドライバ。"""

    def render(self, url: str, viewport_width: int) -> RenderResult:
        raise AssertionError("robots.txt で禁止されたURLに対して driver.render() が呼ばれてはならない")


def test_fetches_dom_and_computed_style_per_breakpoint() -> None:
    driver = FakeBrowserDriver()
    robots_checker = RobotsChecker(ALLOW_ALL_ROBOTS_TXT, user_agent="*")
    breakpoints = [375, 768, 1280]

    result = fetch_rendered_page(
        "https://example.com/",
        driver,
        robots_checker=robots_checker,
        breakpoints=breakpoints,
    )

    assert isinstance(result, PageFetchResult)
    assert result.url == "https://example.com/"
    assert [c.viewport_width for c in result.breakpoints] == breakpoints

    # driver は breakpoint の数だけ、それぞれの幅で個別に呼ばれること。
    assert driver.calls == [("https://example.com/", w) for w in breakpoints]

    # 各breakpointの結果は他のbreakpointと混ざらず独立して保持されていること。
    for width in breakpoints:
        capture = result.by_width(width)
        assert isinstance(capture, BreakpointCapture)
        assert capture.viewport_width == width
        assert f"data-width='{width}'" in capture.outer_html
        assert capture.computed_styles["body"]["font-size"] == f"{width // 100}px"

    widths_seen = {c.viewport_width for c in result.breakpoints}
    assert widths_seen == set(breakpoints)
    # 一方のbreakpointを取得しても他方のcomputed_stylesが変化しない（独立コピー）こと。
    capture_375 = result.by_width(375)
    capture_1280 = result.by_width(1280)
    assert capture_375.computed_styles is not capture_1280.computed_styles
    assert capture_375.outer_html != capture_1280.outer_html


def test_skips_urls_disallowed_by_robots() -> None:
    driver = RecordingRefusingDriver()
    robots_checker = RobotsChecker(DISALLOW_ALL_ROBOTS_TXT, user_agent="*")

    with pytest.raises(RobotsDisallowedError):
        fetch_rendered_page(
            "https://example.com/secret",
            driver,
            robots_checker=robots_checker,
            breakpoints=[375, 1280],
        )


def test_browser_driver_is_injectable_and_mockable() -> None:
    class MockDriver:
        def __init__(self) -> None:
            self.render_calls: List[Tuple[str, int]] = []

        def render(self, url: str, viewport_width: int) -> RenderResult:
            self.render_calls.append((url, viewport_width))
            return RenderResult(outer_html="<html></html>", computed_styles={})

    mock_driver = MockDriver()
    # duck-typing的にBrowserDriverインターフェースを満たしているかを確認できること。
    assert isinstance(mock_driver, BrowserDriver)

    robots_checker = RobotsChecker(ALLOW_ALL_ROBOTS_TXT, user_agent="*")
    result = fetch_rendered_page(
        "https://example.com/page",
        mock_driver,
        robots_checker=robots_checker,
        breakpoints=[768],
    )

    assert mock_driver.render_calls == [("https://example.com/page", 768)]
    assert result.breakpoints[0].viewport_width == 768

    # 型注釈上もBrowserDriverを差し替え可能であることの確認（同じ関数がフェイク実装でも動く）。
    other_driver = FakeBrowserDriver()
    other_result = fetch_rendered_page(
        "https://example.com/page",
        other_driver,
        robots_checker=robots_checker,
        breakpoints=[768],
    )
    assert other_result.breakpoints[0].outer_html != result.breakpoints[0].outer_html
