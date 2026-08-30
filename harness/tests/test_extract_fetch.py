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


def test_extract_breakpoints_from_css() -> None:
    from harness.extract.fetch import extract_breakpoints_from_css

    css = """
    /* Standard media queries */
    @media (min-width: 640px) { .container { max-width: 640px; } }
    @media (max-width: 767.98px) { .mobile-only { display: block; } }
    @media only screen and (min-width: 48rem) { .tablet { display: flex; } }
    @media (min-width: 60em) { .desktop { font-size: 16px; } }
    @media (min-width: 750pt) { .print { width: 1000px; } }

    /* Media Queries Level 4 range syntax */
    @media (width >= 1024px) { .wide { width: 100%; } }
    @media (640px <= width <= 1280px) { .between { color: red; } }

    /* CSS Custom properties */
    :root {
        --breakpoint-sm: 640px;
        --breakpoint-xl: 1440px;
        --screen-2xl: 1536px;
    }

    /* Invalid / out of range should be ignored */
    @media (min-width: 50px) { .tiny { display: none; } }
    @media (min-width: 5000px) { .huge { display: none; } }
    """

    bps = extract_breakpoints_from_css(css)
    # 48rem = 768px, 60em = 960px, 750pt = 1000px
    # 767.98px rounds to 768px
    expected = [640, 768, 960, 1000, 1024, 1280, 1440, 1536]
    assert bps == expected


def test_determine_sampling_breakpoints() -> None:
    from harness.extract.fetch import determine_sampling_breakpoints

    default = [375, 768, 1280, 1920]

    # Empty discovered breakpoints -> returns defaults
    assert determine_sampling_breakpoints([], default) == default

    # Discovered [640, 1024] -> includes 375 (mobile < 640) and 1280 (desktop > 1024)
    sampled = determine_sampling_breakpoints([640, 1024], default)
    assert sampled == [375, 640, 1024, 1280]

    # Discovered [360, 768, 1440] -> already has <= 375 and >= 1280
    sampled2 = determine_sampling_breakpoints([360, 768, 1440], default)
    assert sampled2 == [360, 768, 1440]


def test_fetch_rendered_page_auto_extracts_breakpoints() -> None:
    class DriverWithCssBreakpoints:
        def __init__(self) -> None:
            self.calls: List[Tuple[str, int]] = []

        def render(self, url: str, viewport_width: int) -> RenderResult:
            self.calls.append((url, viewport_width))
            html = """
            <html>
            <head>
              <style>
                @media (min-width: 640px) { .card { width: 50%; } }
                @media (min-width: 1024px) { .card { width: 33%; } }
              </style>
            </head>
            <body></body>
            </html>
            """
            return RenderResult(
                outer_html=html,
                computed_styles={},
                css_breakpoints=[640, 1024],
            )

    driver = DriverWithCssBreakpoints()
    robots_checker = RobotsChecker(ALLOW_ALL_ROBOTS_TXT, user_agent="*")
    logs = []
    progress_updates = []

    result = fetch_rendered_page(
        "https://example.com/responsive",
        driver,
        robots_checker=robots_checker,
        breakpoints=None,  # auto-extract
        log_fn=lambda msg: logs.append(msg),
        progress_cb=lambda s, d: progress_updates.append((s, d)),
    )

    assert result.css_breakpoints == [640, 1024]
    # Sampled viewports: [375, 640, 1024, 1280] (or sorted list including probe and sampled)
    assert 375 in [c.viewport_width for c in result.breakpoints]
    assert 640 in [c.viewport_width for c in result.breakpoints]
    assert 1024 in [c.viewport_width for c in result.breakpoints]
    assert 1280 in [c.viewport_width for c in result.breakpoints]

    # Verify logs and progress were emitted
    assert any("auto-extracted CSS breakpoints" in log for log in logs)
    assert len(progress_updates) > 0

