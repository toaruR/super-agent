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

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from harness.extract.robots import RobotsChecker, load_design_extract_config


def extract_breakpoints_from_css(css_text: str) -> List[int]:
    """CSS テキストから @media クエリやブレークポイント用カスタムプロパティに定義された
    ブレークポイント幅（px単位、整数値）を抽出してソート・重複排除して返す。
    """
    if not css_text or not isinstance(css_text, str):
        return []

    breakpoints = set()

    def _convert_to_px(val_str: str, unit: str) -> Optional[int]:
        try:
            val = float(val_str)
        except (ValueError, TypeError):
            return None
        unit = unit.lower()
        if unit in ("rem", "em"):
            px = val * 16.0
        elif unit == "pt":
            px = val * 4.0 / 3.0
        elif unit == "px" or not unit:
            px = val
        else:
            return None
        rounded = int(round(px))
        if 320 <= rounded <= 3840:
            return rounded
        return None

    # Pattern 1: (min-width / max-width / min-device-width / max-device-width : <num><unit>)
    pattern_prop = re.compile(
        r"(?:min-width|max-width|min-device-width|max-device-width)\s*:\s*(\d+(?:\.\d+)?)\s*(px|rem|em|pt)",
        re.IGNORECASE,
    )
    for m in pattern_prop.finditer(css_text):
        px = _convert_to_px(m.group(1), m.group(2))
        if px is not None:
            breakpoints.add(px)

    # Pattern 2a: Range syntax before 'width' (e.g. 640px <= width)
    pattern_range_before = re.compile(
        r"(\d+(?:\.\d+)?)\s*(px|rem|em|pt)\s*[<>]=?\s*width",
        re.IGNORECASE,
    )
    for m in pattern_range_before.finditer(css_text):
        px = _convert_to_px(m.group(1), m.group(2))
        if px is not None:
            breakpoints.add(px)

    # Pattern 2b: Range syntax after 'width' (e.g. width >= 1024px or width <= 1280px)
    pattern_range_after = re.compile(
        r"width\s*[<>]=?\s*(\d+(?:\.\d+)?)\s*(px|rem|em|pt)",
        re.IGNORECASE,
    )
    for m in pattern_range_after.finditer(css_text):
        px = _convert_to_px(m.group(1), m.group(2))
        if px is not None:
            breakpoints.add(px)

    # Pattern 3: CSS custom properties like --breakpoint-sm: 640px; --screen-md: 768px;
    pattern_vars = re.compile(
        r"--(?:breakpoint|screen|bp)-[a-zA-Z0-9_-]+\s*:\s*(\d+(?:\.\d+)?)\s*(px|rem|em|pt)",
        re.IGNORECASE,
    )
    for m in pattern_vars.finditer(css_text):
        px = _convert_to_px(m.group(1), m.group(2))
        if px is not None:
            breakpoints.add(px)

    return sorted(breakpoints)


def determine_sampling_breakpoints(
    discovered_breakpoints: Sequence[int],
    default_breakpoints: Sequence[int],
    max_samples: int = 6,
) -> List[int]:
    """CSSから抽出されたブレークポイントに基づき、サンプリング対象のビューポート幅リストを生成する。
    抽出されたブレークポイントが多数ある場合は、代表的なブレークポイント（モバイル〜デスクトップの各バンド）
    を最大 max_samples 個選定して過剰なブラウザレンダリングを防ぐ。
    抽出が空の場合は default_breakpoints を返す。
    """
    if not discovered_breakpoints:
        return list(default_breakpoints)

    unique_bps = sorted(set(discovered_breakpoints))
    if len(unique_bps) <= max_samples:
        bps = set(unique_bps)
        if min(bps) > 375:
            bps.add(375)
        if max(bps) < 1280:
            bps.add(1280)
        return sorted(bps)

    # 多数のブレークポイントがある場合は、レスポンシブの主要バンドごとに代表値を選定
    # バンド: Mobile (<640), Tablet-SM (640-767), Tablet-MD (768-1023), Desktop (1024-1279), Wide (>=1280)
    bands = [
        [b for b in unique_bps if b < 640],
        [b for b in unique_bps if 640 <= b < 768],
        [b for b in unique_bps if 768 <= b < 1024],
        [b for b in unique_bps if 1024 <= b < 1280],
        [b for b in unique_bps if b >= 1280],
    ]

    selected: Set[int] = {375}  # 常にモバイルベースラインを含める
    for band in bands:
        if band:
            selected.add(band[len(band) // 2])

    if max(selected) < 1280:
        selected.add(1280)

    if len(selected) > max_samples:
        sorted_sel = sorted(selected)
        indices = [int(i * (len(sorted_sel) - 1) / (max_samples - 1)) for i in range(max_samples)]
        selected = {sorted_sel[i] for i in indices}

    return sorted(selected)


@dataclass(frozen=True)
class RenderResult:
    """1つのURL×1つのビューポート幅に対する、ブラウザドライバの生の描画結果。"""

    outer_html: str
    computed_styles: Dict[str, Dict[str, str]]
    metadata: Dict[str, Any] = field(default_factory=dict)
    css_variables: Dict[str, str] = field(default_factory=dict)
    css_breakpoints: List[int] = field(default_factory=list)


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
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_load_state("load", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(500)
            except Exception:
                pass

            outer_html = page.eval_on_selector("html", "el => el.outerHTML")
            data = page.evaluate(
                """() => {
                    const KEY_PROPS = [
                        'color', 'background-color', 'background', 'font-family', 'font-size', 'font-weight',
                        'line-height', 'letter-spacing', 'border', 'border-color', 'border-style', 'border-width',
                        'border-radius', 'border-top-left-radius', 'border-top-right-radius', 'border-bottom-left-radius', 'border-bottom-right-radius',
                        'padding', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
                        'margin', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
                        'box-shadow', 'display', 'flex-direction', 'align-items', 'justify-content', 'gap',
                        'width', 'height', 'max-width', 'min-width', 'opacity', 'text-align', 'text-decoration', 'text-transform'
                    ];

                    const result = {};
                    document.querySelectorAll('*').forEach((el, i) => {
                        const key = el.tagName.toLowerCase() + ':' + i;
                        const style = getComputedStyle(el);
                        const item = {};
                        for (let p of KEY_PROPS) {
                            const v = style.getPropertyValue(p);
                            if (v) item[p] = v;
                        }
                        result[key] = item;
                    });

                    // メタデータの抽出
                    const meta = {
                        title: document.title || '',
                        description: document.querySelector('meta[name="description"]')?.getAttribute('content') || '',
                        themeColor: document.querySelector('meta[name="theme-color"]')?.getAttribute('content') || '',
                        ogTitle: document.querySelector('meta[property="og:title"]')?.getAttribute('content') || '',
                        ogDescription: document.querySelector('meta[property="og:description"]')?.getAttribute('content') || '',
                        ogSiteName: document.querySelector('meta[property="og:site_name"]')?.getAttribute('content') || '',
                    };

                    // CSSカスタムプロパティ (--*) の抽出
                    const cssVars = {};
                    const cssBreakpoints = new Set();

                    function parseMediaText(mediaText) {
                        if (!mediaText) return;
                        const regex = /(?:min-width|max-width|min-device-width|max-device-width)\\s*:\\s*(\\d+(?:\\.\\d+)?)\\s*(px|rem|em|pt)/gi;
                        let match;
                        while ((match = regex.exec(mediaText)) !== null) {
                            let val = parseFloat(match[1]);
                            const unit = (match[2] || 'px').toLowerCase();
                            if (unit === 'rem' || unit === 'em') val *= 16;
                            else if (unit === 'pt') val = val * 4 / 3;
                            val = Math.round(val);
                            if (val >= 320 && val <= 3840) cssBreakpoints.add(val);
                        }
                        const rangeRegex = /(?:(\\d+(?:\\.\\d+)?)\\s*(px|rem|em|pt)\\s*[<>]=?\\s*width|width\\s*[<>]=?\\s*(\\d+(?:\\.\\d+)?)\\s*(px|rem|em|pt))/gi;
                        while ((match = rangeRegex.exec(mediaText)) !== null) {
                            let numStr = match[1] || match[3];
                            let unit = (match[2] || match[4] || 'px').toLowerCase();
                            let val = parseFloat(numStr);
                            if (unit === 'rem' || unit === 'em') val *= 16;
                            else if (unit === 'pt') val = val * 4 / 3;
                            val = Math.round(val);
                            if (val >= 320 && val <= 3840) cssBreakpoints.add(val);
                        }
                    }

                    function scanRule(rule) {
                        try {
                            if (rule.type === CSSRule.MEDIA_RULE || rule.media) {
                                parseMediaText(rule.media ? rule.media.mediaText : (rule.conditionText || ''));
                                if (rule.cssRules) {
                                    for (let nested of rule.cssRules) scanRule(nested);
                                }
                            } else if (rule.cssRules) {
                                for (let nested of rule.cssRules) scanRule(nested);
                            }
                        } catch (e) {}
                    }

                    try {
                        const rootStyle = getComputedStyle(document.documentElement);
                        for (let sheet of document.styleSheets) {
                            try {
                                for (let rule of (sheet.cssRules || [])) {
                                    scanRule(rule);
                                    if (rule.style) {
                                        for (let prop of rule.style) {
                                            if (prop.startsWith('--')) {
                                                const val = rootStyle.getPropertyValue(prop).trim();
                                                cssVars[prop] = val;
                                                if (prop.includes('breakpoint') || prop.includes('screen') || prop.includes('bp')) {
                                                    parseMediaText(prop + ':' + val);
                                                }
                                            }
                                        }
                                    }
                                }
                            } catch (e) {}
                        }
                    } catch (e) {}

                    document.querySelectorAll('style').forEach(styleEl => {
                        if (styleEl.textContent) {
                            parseMediaText(styleEl.textContent);
                        }
                    });

                    return {
                        styles: result,
                        metadata: meta,
                        cssVars: cssVars,
                        cssBreakpoints: Array.from(cssBreakpoints).sort((a, b) => a - b)
                    };
                }"""
            )
            css_bps = data.get("cssBreakpoints", [])
            # Also supplement with Python regex on outer_html style tags
            extracted_html_bps = extract_breakpoints_from_css(outer_html)
            all_bps = sorted(set(css_bps) | set(extracted_html_bps))

            return RenderResult(
                outer_html=outer_html,
                computed_styles=data.get("styles", {}),
                metadata=data.get("metadata", {}),
                css_variables=data.get("cssVars", {}),
                css_breakpoints=all_bps,
            )
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
    metadata: Dict[str, Any] = field(default_factory=dict)
    css_variables: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PageFetchResult:
    """fetch_rendered_page() の結果。breakpointsはビューポート幅ごとに独立した要素のリスト。"""

    url: str
    breakpoints: List[BreakpointCapture] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    css_variables: Dict[str, str] = field(default_factory=dict)
    css_breakpoints: List[int] = field(default_factory=list)

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
    log_fn: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[str, str], None]] = None,
) -> PageFetchResult:
    """指定URLをdriver越しにレンダリングし、breakpointごとのDOM/computed styleを取得する。

    取得前に robots_checker.is_allowed(url) で許可判定を行い、禁止されている場合は
    driver には一切アクセスせず RobotsDisallowedError を送出する（fetchしない）。

    breakpoints を省略した場合は、初回のレンダリングから CSS ブレークポイントを自動抽出し、
    代表的なビューポート幅リストを自動決定する（CSSからブレークポイントが得られない場合は
    design_extract.yaml のデフォルト値を使用）。
    """
    if not robots_checker.is_allowed(url):
        raise RobotsDisallowedError(f"{url} is disallowed by robots.txt")

    if log_fn:
        log_fn(f"[fetch] robots.txt allowed for {url}")

    cfg = config if config is not None else load_design_extract_config()
    default_breakpoints = cfg.get("breakpoints", [375, 768, 1280, 1920])

    captures: List[BreakpointCapture] = []
    first_metadata: Dict[str, Any] = {}
    first_css_vars: Dict[str, str] = {}
    discovered_css_breakpoints: List[int] = []

    if breakpoints is not None:
        target_widths = list(breakpoints)
        if log_fn:
            log_fn(f"[fetch] using explicit breakpoints: {target_widths}")
    else:
        # Initial probe width to discover CSS breakpoints
        probe_width = default_breakpoints[0] if default_breakpoints else 1280
        if log_fn:
            log_fn(f"[fetch] probing page at viewport {probe_width}px to extract CSS breakpoints...")
        if progress_cb:
            progress_cb("extracting", f"Probing {url} at {probe_width}px (extracting CSS breakpoints)...")

        initial_result = driver.render(url, probe_width)
        first_metadata = getattr(initial_result, "metadata", {}) or {}
        first_css_vars = getattr(initial_result, "css_variables", {}) or {}

        # CSS breakpoints from driver result or outer_html
        raw_bps = getattr(initial_result, "css_breakpoints", []) or []
        html_bps = extract_breakpoints_from_css(initial_result.outer_html)
        discovered_css_breakpoints = sorted(set(raw_bps) | set(html_bps))

        target_widths = determine_sampling_breakpoints(discovered_css_breakpoints, default_breakpoints)

        if discovered_css_breakpoints:
            if log_fn:
                log_fn(f"[fetch] auto-extracted CSS breakpoints: {discovered_css_breakpoints} -> sampling viewports: {target_widths}")
        else:
            if log_fn:
                log_fn(f"[fetch] no CSS breakpoints found; using default breakpoints: {target_widths}")

        # Store the probe capture if its width is in target_widths
        if probe_width in target_widths:
            captures.append(
                BreakpointCapture(
                    viewport_width=probe_width,
                    outer_html=initial_result.outer_html,
                    computed_styles=initial_result.computed_styles,
                    metadata=first_metadata,
                    css_variables=first_css_vars,
                )
            )

    for viewport_width in target_widths:
        if any(c.viewport_width == viewport_width for c in captures):
            continue

        if log_fn:
            log_fn(f"[fetch] rendering page at viewport {viewport_width}px...")
        if progress_cb:
            progress_cb("extracting", f"Rendering viewport {viewport_width}px...")

        result = driver.render(url, viewport_width)
        meta = getattr(result, "metadata", {}) or {}
        css_vars = getattr(result, "css_variables", {}) or {}
        if not first_metadata and meta:
            first_metadata = meta
        if not first_css_vars and css_vars:
            first_css_vars = css_vars

        captures.append(
            BreakpointCapture(
                viewport_width=viewport_width,
                outer_html=result.outer_html,
                computed_styles=result.computed_styles,
                metadata=meta,
                css_variables=css_vars,
            )
        )

    # Sort captures by viewport_width
    captures.sort(key=lambda c: c.viewport_width)

    return PageFetchResult(
        url=url,
        breakpoints=captures,
        metadata=first_metadata,
        css_variables=first_css_vars,
        css_breakpoints=discovered_css_breakpoints,
    )

