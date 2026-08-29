"""design-extract パイプラインの Fetch 段階（レンダリング後DOM/computed style取得）。

- BrowserDriver: ヘッドレスブラウザ操作を抽象化するインターフェース。fetch_rendered_page()
  はこのインターフェース越しにのみブラウザを操作するため、実ブラウザ（Playwright等）を
  起動せずにフェイク/モック実装でユニットテストできる。
- PlaywrightBrowserDriver: BrowserDriver の実運用向け実装。Playwright への依存は
  インスタンス生成時（__init__）まで遅延importするため、playwright未インストールでも
  本モジュールのimportやテストには影響しない。
- fetch_rendered_page: harness.extract.robots の RobotsChecker で許可判定した上で、
  design_extract.yaml の breakpoints で指定された各ビューポート幅ごとに
  outerHTML相当のDOMとcomputed styleを取得し、ブレークポイントごとに独立した
  構造化結果として返す。禁止URLは一切fetchしない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Sequence, runtime_checkable

from harness.extract.robots import RobotsChecker, load_design_extract_config


@dataclass(frozen=True)
class RenderResult:
    """1つのURL×1つのビューポート幅に対する、ブラウザドライバの生の描画結果。"""

    outer_html: str
    computed_styles: Dict[str, Dict[str, str]]


@runtime_checkable
class BrowserDriver(Protocol):
    """ヘッドレスブラウザ操作の差し替え可能インターフェース。

    実装（Playwright等）はこの render() だけを満たせばよい。fetch_rendered_page()
    はこの1メソッド越しにしかブラウザを操作しないため、テストではフェイク実装を
    注入するだけで実ブラウザなしに検証できる。
    """

    def render(self, url: str, viewport_width: int) -> RenderResult:
        ...


class PlaywrightBrowserDriver:
    """BrowserDriver の Playwright ベースの実運用向け実装。

    playwright への依存はコンストラクタで初めて import するため、
    本モジュール自体は playwright 未インストールの環境でも import 可能。
    """

    def __init__(self, browser_type: str = "chromium", headless: bool = True) -> None:
        from playwright.sync_api import sync_playwright  # 遅延import

        self._playwright = sync_playwright().start()
        self._browser = getattr(self._playwright, browser_type).launch(headless=headless)

    def render(self, url: str, viewport_width: int) -> RenderResult:
        page = self._browser.new_page(viewport={"width": viewport_width, "height": 1024})
        try:
            page.goto(url)
            outer_html = page.eval_on_selector("html", "el => el.outerHTML")
            computed_styles = page.evaluate(
                """() => {
                    const result = {};
                    document.querySelectorAll('*').forEach((el, i) => {
                        const key = el.tagName.toLowerCase() + ':' + i;
                        const style = getComputedStyle(el);
                        result[key] = Object.fromEntries(
                            Array.from(style).map(prop => [prop, style.getPropertyValue(prop)])
                        );
                    });
                    return result;
                }"""
            )
            return RenderResult(outer_html=outer_html, computed_styles=computed_styles)
        finally:
            page.close()

    def close(self) -> None:
        self._browser.close()
        self._playwright.stop()


class RobotsDisallowedError(Exception):
    """robots.txt により許可されていないURLに対してfetchが要求された場合に送出する。"""


@dataclass(frozen=True)
class BreakpointCapture:
    """1つのビューポート幅における取得結果。他のブレークポイントとは独立して保持する。"""

    viewport_width: int
    outer_html: str
    computed_styles: Dict[str, Dict[str, str]]


@dataclass(frozen=True)
class PageFetchResult:
    """fetch_rendered_page() の結果。breakpointsはビューポート幅ごとに独立した要素のリスト。"""

    url: str
    breakpoints: List[BreakpointCapture] = field(default_factory=list)

    def by_width(self, viewport_width: int) -> Optional[BreakpointCapture]:
        for capture in self.breakpoints:
            if capture.viewport_width == viewport_width:
                return capture
        return None


def fetch_rendered_page(
    url: str,
    driver: BrowserDriver,
    *,
    robots_checker: RobotsChecker,
    breakpoints: Optional[Sequence[int]] = None,
    config: Optional[dict] = None,
) -> PageFetchResult:
    """指定URLをdriver越しにレンダリングし、breakpointごとのDOM/computed styleを取得する。

    取得前に robots_checker.is_allowed(url) で許可判定を行い、禁止されている場合は
    driver には一切アクセスせず RobotsDisallowedError を送出する（fetchしない）。

    breakpoints を省略した場合は design_extract.yaml（config で差し替え可）の
    "breakpoints" を用いる。各ビューポート幅の取得結果は BreakpointCapture として
    互いに独立した要素にまとめ、breakpoint間の結果が混在しないようにする。
    """
    if not robots_checker.is_allowed(url):
        raise RobotsDisallowedError(f"{url} is disallowed by robots.txt")

    if breakpoints is None:
        cfg = config if config is not None else load_design_extract_config()
        breakpoints = cfg.get("breakpoints", [])

    captures: List[BreakpointCapture] = []
    for viewport_width in breakpoints:
        result = driver.render(url, viewport_width)
        captures.append(
            BreakpointCapture(
                viewport_width=viewport_width,
                outer_html=result.outer_html,
                computed_styles=result.computed_styles,
            )
        )

    return PageFetchResult(url=url, breakpoints=captures)
