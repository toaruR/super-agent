"""harness/extract/analyze.py および generate.py のカラー抽出・テーマ連動・トークン衝突防止のテスト。"""
from __future__ import annotations

import pytest

from harness.extract.analyze import (
    BreakpointCapture,
    ColorToken,
    DesignSystemExtractor,
    PageFetchResult,
    analyze_components,
)
from harness.extract.generate import render_components_css, render_design_md, render_skeleton_html, render_tokens_css
from harness.extract.tokens import build_design_tokens


def test_button_weighted_brand_color_detection() -> None:
    """ページ全体で出現回数の多い装飾色（Teal）よりも、ボタン背景色のオレンジが高スコアで Primary Brand に選定されること。"""
    # HTML: 1つのオレンジボタンと、多数のティールリンク/テキスト
    outer_html = """
    <html>
      <body>
        <header>
          <a class="nav-link" id="n1">Nav 1</a>
          <a class="nav-link" id="n2">Nav 2</a>
          <a class="nav-link" id="n3">Nav 3</a>
          <a class="nav-link" id="n4">Nav 4</a>
          <a class="nav-link" id="n5">Nav 5</a>
          <a class="btn btn-primary" id="btn-cta">Get a Demo</a>
        </header>
        <div class="content">
          <p class="teal-text" id="p1">Text 1</p>
          <p class="teal-text" id="p2">Text 2</p>
          <p class="teal-text" id="p3">Text 3</p>
        </div>
      </body>
    </html>
    """

    # computed styles:
    # #124548 (Teal) が多数の要素で出現 (8要素)
    # #ff5c35 (Orange) はボタン背景 (1要素)
    computed_styles = {
        "html:0": {"background-color": "#ffffff"},
        "body:1": {"background-color": "#ffffff", "color": "#1f1f1f"},
        "a:3": {"color": "#124548"},
        "a:4": {"color": "#124548"},
        "a:5": {"color": "#124548"},
        "a:6": {"color": "#124548"},
        "a:7": {"color": "#124548"},
        "a:8": {
            "background-color": "rgb(255, 92, 53)",  # #ff5c35 (HubSpot Orange)
            "color": "rgb(255, 255, 255)",
            "border-radius": "6px",
            "padding": "12px 20px",
        },
        "p:10": {"color": "#124548"},
        "p:11": {"color": "#124548"},
        "p:12": {"color": "#124548"},
    }

    fetch_result = PageFetchResult(
        url="https://www.hubspot.com/",
        breakpoints=[
            BreakpointCapture(
                viewport_width=1280,
                outer_html=outer_html,
                computed_styles=computed_styles,
            )
        ],
        metadata={"title": "HubSpot | Inbound Marketing"},
    )

    extractor = DesignSystemExtractor(fetch_result)
    ds = extractor.extract()

    # テーマ判定: light
    assert ds.theme == "light"

    # ブランドカラー: オレンジ (#ff5c35) が選定されていること
    brand_tokens = [c for c in ds.colors if c.category == "brand"]
    primary_token = next((c for c in ds.colors if c.token_name == "--color-primary"), None)
    assert primary_token is not None
    assert primary_token.hex_value.lower() == "#ff5c35"

    # 文字色 (--color-on-primary): 白 (#ffffff) が選定されていること
    on_primary_token = next((c for c in ds.colors if c.token_name == "--color-on-primary"), None)
    assert on_primary_token is not None
    assert on_primary_token.hex_value.lower() == "#ffffff"

    # 装飾色の Teal (#124548) はアクセントカラーとして保持されていること
    teal_token = next((c for c in ds.colors if c.hex_value.lower() == "#124548"), None)
    assert teal_token is not None
    assert teal_token.category == "accent"


def test_no_token_name_collisions() -> None:
    """同一色相の複数の色が抽出された場合でも、トークン名が重複・上書きされないこと。"""
    outer_html = """
    <html>
      <body>
        <button id="b1" class="btn">Primary Orange</button>
        <div id="d1" class="badge">Light Coral</div>
      </body>
    </html>
    """

    computed_styles = {
        "html:0": {"background-color": "#ffffff"},
        "body:1": {"background-color": "#ffffff"},
        "button:3": {
            "background-color": "rgb(255, 72, 0)",  # #ff4800 (Coral Red)
            "color": "#ffffff",
        },
        "div:4": {
            "background-color": "rgb(252, 222, 210)",  # #fcded2 (Coral Red Light)
            "color": "#1f1f1f",
        },
    }

    fetch_result = PageFetchResult(
        url="https://example.com/",
        breakpoints=[
            BreakpointCapture(
                viewport_width=1280,
                outer_html=outer_html,
                computed_styles=computed_styles,
            )
        ],
    )

    extractor = DesignSystemExtractor(fetch_result)
    ds = extractor.extract()

    token_names = [c.token_name for c in ds.colors]
    # トークン名に重複がないこと
    assert len(token_names) == len(set(token_names))


def test_theme_aware_components_css_light() -> None:
    """ライトテーマにおいて、白背景・クリーンボーダー・プライマリカラーが正しく反映された components.css が生成されること。"""
    outer_html = "<html><body><button class='btn'>Go</button></body></html>"
    computed_styles = {
        "body:1": {"background-color": "#ffffff"},
        "button:2": {"background-color": "rgb(255, 92, 53)", "color": "#ffffff"},
    }
    fetch_result = PageFetchResult(
        url="https://www.hubspot.com/",
        breakpoints=[
            BreakpointCapture(
                viewport_width=1280,
                outer_html=outer_html,
                computed_styles=computed_styles,
            )
        ],
    )
    analysis = analyze_components(fetch_result)
    ds = analysis.design_system
    assert ds is not None
    assert ds.theme == "light"

    css = render_components_css(ds)
    assert "/* Main Components CSS (Light Theme) */" in css
    assert "var(--color-primary, #ff5c35)" in css
    assert "var(--color-on-primary, #ffffff)" in css
    assert "var(--surface-canvas, #ffffff)" in css

    skeleton = render_skeleton_html({}, design_system=ds)
    assert "var(--surface-canvas, #ffffff)" in skeleton
    assert "var(--color-body, #1f1f1f)" in skeleton


def test_theme_aware_components_css_dark() -> None:
    """ダークテーマにおいて、ダーク背景用の components.css が生成されること。"""
    outer_html = "<html><body><button class='btn'>Go</button></body></html>"
    computed_styles = {
        "body:1": {"background-color": "#08090a"},
        "button:2": {"background-color": "rgb(228, 242, 34)", "color": "#08090a"},
    }
    fetch_result = PageFetchResult(
        url="https://linear.app/",
        breakpoints=[
            BreakpointCapture(
                viewport_width=1280,
                outer_html=outer_html,
                computed_styles=computed_styles,
            )
        ],
    )
    analysis = analyze_components(fetch_result)
    ds = analysis.design_system
    assert ds is not None
    assert ds.theme == "dark"

    css = render_components_css(ds)
    assert "/* Main Components CSS (Dark Theme) */" in css
    assert "var(--color-primary, #e4f222)" in css
    assert "var(--surface-surface, #0f1011)" in css


def test_dynamic_component_and_font_extraction() -> None:
    """対象サイトの実際の font-family, ボタン padding, border-radius が動的に抽出され tokens.css / components.css に反映されること。"""
    outer_html = """
    <html>
      <body>
        <button class="btn" id="b1">Demo</button>
        <div class="card" id="c1">Card</div>
      </body>
    </html>
    """
    computed_styles = {
        "html:0": {"background-color": "#ffffff"},
        "body:1": {
            "background-color": "#ffffff",
            "font-family": '"HubSpot Sans", sans-serif',
            "color": "#1f1f1f",
        },
        "button:3": {
            "background-color": "rgb(255, 72, 0)",
            "color": "#ffffff",
            "border-radius": "8px",
            "padding": "16px 40px",
            "font-family": '"HubSpot Sans", sans-serif',
            "font-size": "18px",
            "font-weight": "500",
            "line-height": "32px",
        },
        "div:4": {
            "background-color": "#ffffff",
            "border-radius": "8px",
            "padding": "24px",
            "font-family": '"HubSpot Sans", sans-serif',
        },
    }

    fetch_result = PageFetchResult(
        url="https://www.hubspot.com/",
        breakpoints=[
            BreakpointCapture(
                viewport_width=1280,
                outer_html=outer_html,
                computed_styles=computed_styles,
            )
        ],
        metadata={"title": "HubSpot"},
    )

    extractor = DesignSystemExtractor(fetch_result)
    ds = extractor.extract()

    # 抽出されたフォント: HubSpot Sans
    primary_font = next((f for f in ds.font_families if f.role == "Primary"), None)
    assert primary_font is not None
    assert primary_font.name == "HubSpot Sans"
    assert '"HubSpot Sans", sans-serif' in primary_font.substitute

    # 抽出された CSS カスタムプロパティ (tokens.css)
    tokens_css = render_tokens_css({}, design_system=ds)
    assert "--font-primary: \"HubSpot Sans\", sans-serif;" in tokens_css
    assert "--radius-buttons: 8px;" in tokens_css

    # 抽出された components.css
    comp_css = render_components_css(ds)
    assert "padding: 16px 40px;" in comp_css
    assert "var(--radius-buttons, 8px)" in comp_css
    assert "line-height: 32px;" in comp_css
