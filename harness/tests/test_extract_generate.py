"""harness/extract/generate.py の単体テスト。

- render_prompt() が、T4(tokens)が生成するW3C Design Tokens形式のJSONから、
  トークンカテゴリ・コンポーネント単位で見出しを分けたMarkdownを生成すること。
- 同一のトークンJSON入力に対しては常に同一のMarkdown文字列を返す(決定的)こと。
- トークンJSONに存在しないカテゴリ/コンポーネントがあっても出力が破綻しない(例外を
  送出せず、抽出結果なしを示す一文を出すだけで処理を継続する)こと。
"""
from __future__ import annotations

import collections

from harness.extract.generate import (
    render_components_css,
    render_prompt,
    render_skeleton_html,
    render_tokens_css,
)
from harness.extract.tokens import TOKEN_CATEGORIES


def _sample_tokens() -> dict:
    return {
        "color": {
            "button": {
                "1280:button:1": {
                    "color": {"$type": "color", "$value": "#ffffff"},
                    "background-color": {"$type": "color", "$value": "#0055ff"},
                },
            },
            "card": {},
            "nav": {
                "1280:nav:4": {
                    "color": {"$type": "color", "$value": "#333333"},
                },
            },
            "form": {},
        },
        "typography": {
            "button": {
                "1280:button:1": {
                    "$type": "typography",
                    "$value": {"fontSize": "14px", "fontWeight": "600"},
                },
            },
            "card": {},
            "nav": {},
            "form": {},
        },
        "spacing": {
            "button": {
                "1280:button:1": {
                    "padding": {"$type": "dimension", "$value": "8px 16px"},
                },
            },
            "card": {
                "1280:div:2": {
                    "padding": {"$type": "dimension", "$value": "16px"},
                },
            },
            "nav": {},
            "form": {},
        },
        "radius": {
            "button": {
                "1280:button:1": {
                    "border-radius": {"$type": "dimension", "$value": "4px"},
                },
            },
            "card": {
                "1280:div:2": {
                    "border-radius": {"$type": "dimension", "$value": "8px"},
                },
            },
            "nav": {},
            "form": {},
        },
        "shadow": {
            "button": {},
            "card": {
                "1280:div:2": {
                    "box-shadow": {"$type": "shadow", "$value": "0 1px 2px rgba(0,0,0,0.1)"},
                },
            },
            "nav": {},
            "form": {},
        },
    }


def test_renders_markdown_from_token_categories() -> None:
    markdown = render_prompt(_sample_tokens())

    assert isinstance(markdown, str)
    assert markdown.startswith("# Design Prompt")

    # W3C Design Tokens の5カテゴリすべてが見出しとして出力される。
    assert set(TOKEN_CATEGORIES) == {"color", "typography", "spacing", "radius", "shadow"}
    for category in TOKEN_CATEGORIES:
        assert f"## {category.title()}" in markdown

    # 各カテゴリの実データ(値)がMarkdown本文に反映されている。
    assert "#ffffff" in markdown
    assert "#0055ff" in markdown
    assert "fontSize: 14px" in markdown
    assert "8px 16px" in markdown
    assert "4px" in markdown
    assert "0 1px 2px rgba(0,0,0,0.1)" in markdown


def test_output_is_deterministic_for_same_tokens() -> None:
    tokens = _sample_tokens()

    first = render_prompt(tokens)
    second = render_prompt(tokens)
    assert first == second

    # dict のキー挿入順を変えても(内容が同一であれば)出力は変わらない。
    def reorder(node):
        if isinstance(node, dict):
            reordered = collections.OrderedDict()
            for key in sorted(node.keys(), reverse=True):
                reordered[key] = reorder(node[key])
            return dict(reordered)
        return node

    reordered_tokens = reorder(tokens)
    third = render_prompt(reordered_tokens)
    assert first == third


def test_includes_component_level_sections() -> None:
    markdown = render_prompt(_sample_tokens())

    # コンポーネント種別ごとに見出し(サブセクション)が分かれている。
    assert "### Button" in markdown
    assert "### Card" in markdown
    assert "### Nav" in markdown

    # button固有のトークンが button セクション内に、card固有のトークンが
    # card セクション内に、それぞれ現れる(コンポーネント単位の区切りが機能している)。
    button_section = markdown.split("### Button", 1)[1].split("###", 1)[0]
    assert "1280:button:1" in button_section

    card_shadow_section = markdown.split("## Shadow", 1)[1].split("### Card", 1)[1]
    assert "0 1px 2px rgba(0,0,0,0.1)" in card_shadow_section


def test_render_prompt_does_not_break_on_missing_categories_or_components() -> None:
    # 一部カテゴリ/コンポーネントが丸ごと欠落した入力でも例外にならず、
    # 「抽出結果なし」を示す一文で処理を継続する。
    partial_tokens = {
        "color": {
            "button": {
                "1280:button:1": {
                    "color": {"$type": "color", "$value": "#000000"},
                },
            },
        },
    }

    markdown = render_prompt(partial_tokens)

    for category in TOKEN_CATEGORIES:
        assert f"## {category.title()}" in markdown
    assert "_No tokens extracted for this category._" in markdown

    # 空のトークンJSON・Noneに近い入力でも破綻しない。
    empty_markdown = render_prompt({})
    assert empty_markdown.startswith("# Design Prompt")
    for category in TOKEN_CATEGORIES:
        assert f"## {category.title()}" in empty_markdown


def test_render_tokens_css() -> None:
    tokens = _sample_tokens()
    css = render_tokens_css(tokens)
    assert isinstance(css, str)
    assert ":root {" in css
    assert "}" in css
    assert css == render_tokens_css(tokens)

    empty_css = render_tokens_css({})
    assert ":root {" in empty_css

    from harness.extract.analyze import AgentPromptGuideSpec, DesignPrinciples, DesignSystemAnalysis, SpacingShapeSpec
    ds = DesignSystemAnalysis(
        brand_name="TestBrand",
        tagline="Test Tagline",
        theme="dark",
        aesthetic_summary="Test aesthetic",
        colors=[],
        font_families=[],
        type_scale=[],
        spacing_shapes=SpacingShapeSpec("4px", "compact", [], [], []),
        components=[],
        principles=DesignPrinciples([], []),
        surfaces=[],
        elevation_summary="",
        imagery_summary="",
        layout_summary="",
        agent_prompts=AgentPromptGuideSpec({}, []),
        similar_brands=[],
        css_custom_properties=":root {\n  --color-test: #123456;\n}",
        tailwind_v4_theme="",
    )
    ds_css = render_tokens_css({}, design_system=ds)
    assert ":root {" in ds_css
    assert "--color-test: #123456;" in ds_css


def test_render_components_css() -> None:
    css = render_components_css()
    assert isinstance(css, str)
    assert ".btn-primary" in css
    assert ".btn-secondary" in css
    assert ".card-surface" in css
    assert ".nav-container" in css
    assert ".input-field" in css
    assert ".badge" in css


def test_render_skeleton_html() -> None:
    metadata = {"title": "Test Page Title", "google_fonts": "https://fonts.googleapis.com/css2?family=Roboto&display=swap"}
    html = render_skeleton_html(metadata)
    assert isinstance(html, str)
    assert "<!DOCTYPE html>" in html
    assert "<title>Test Page Title</title>" in html
    assert "fonts.googleapis.com" in html
    assert "tokens.css" in html
    assert "components.css" in html
    assert "/* CSS Reset */" in html
    assert "btn-primary" in html


def _make_design_system(components):
    from harness.extract.analyze import AgentPromptGuideSpec, DesignPrinciples, DesignSystemAnalysis, SpacingShapeSpec

    return DesignSystemAnalysis(
        brand_name="TestBrand",
        tagline="Test Tagline",
        theme="light",
        aesthetic_summary="Test aesthetic",
        colors=[],
        font_families=[],
        type_scale=[],
        spacing_shapes=SpacingShapeSpec("4px", "compact", [], [], []),
        components=components,
        principles=DesignPrinciples([], []),
        surfaces=[],
        elevation_summary="",
        imagery_summary="",
        layout_summary="",
        agent_prompts=AgentPromptGuideSpec({}, []),
        similar_brands=[],
        css_custom_properties="",
        tailwind_v4_theme="",
    )


def _make_component_spec(**kwargs):
    from harness.extract.analyze import ComponentSpec

    defaults = dict(name="Component", role="role", spec_summary="summary")
    defaults.update(kwargs)
    return ComponentSpec(**defaults)


def test_render_components_css_reflects_secondary_variant_properties() -> None:
    secondary = _make_component_spec(
        name="Secondary Action Button",
        role="Medium-emphasis action",
        spec_summary="",
        component_type="button",
        variant_key="secondary",
        semantic_role="secondary-cta",
        properties={"background": "#123456", "text": "#abcdef", "border": "2px solid #123456"},
    )
    ds = _make_design_system([secondary])

    css = render_components_css(design_system=ds)

    assert "#123456" in css
    assert "#abcdef" in css
    assert "2px solid #123456" in css
    # 実測値がない場合のデフォルト値(brand_colorのvar())は使われない
    assert "background-color: #123456;" in css


def test_render_components_css_emits_btn_ghost_only_when_present() -> None:
    ds_without_ghost = _make_design_system([
        _make_component_spec(
            name="Primary Action Button",
            role="High-emphasis CTA",
            spec_summary="",
            component_type="button",
            variant_key="primary",
        ),
    ])
    css_without_ghost = render_components_css(design_system=ds_without_ghost)
    assert ".btn-ghost" not in css_without_ghost

    ghost = _make_component_spec(
        name="Ghost Action Button",
        role="Low-emphasis action",
        spec_summary="",
        component_type="button",
        variant_key="ghost",
        semantic_role="tertiary-action",
        properties={"text": "#333333"},
    )
    ds_with_ghost = _make_design_system([ghost])
    css_with_ghost = render_components_css(design_system=ds_with_ghost)
    assert ".btn-ghost {" in css_with_ghost
    assert "#333333" in css_with_ghost


def test_render_components_css_is_deterministic() -> None:
    ds = _make_design_system([
        _make_component_spec(
            name="Secondary Action Button",
            role="Medium-emphasis action",
            spec_summary="",
            component_type="button",
            variant_key="secondary",
            properties={"background": "#123456", "text": "#abcdef"},
        ),
    ])
    first = render_components_css(design_system=ds)
    second = render_components_css(design_system=ds)
    assert first == second


def test_render_skeleton_html_catalog_reflects_detected_variants() -> None:
    primary = _make_component_spec(
        name="Primary Action Button",
        role="High-emphasis CTA",
        spec_summary="",
        component_type="button",
        variant_key="primary",
        semantic_role="primary-cta",
    )
    secondary = _make_component_spec(
        name="Secondary Action Button",
        role="Medium-emphasis action",
        spec_summary="",
        component_type="button",
        variant_key="secondary",
        semantic_role="secondary-cta",
    )
    ds = _make_design_system([primary, secondary])

    html = render_skeleton_html({"title": "Test Page Title"}, design_system=ds)

    assert "<!-- Variant: Primary Action Button (primary-cta) -->" in html
    assert "<!-- Variant: Secondary Action Button (secondary-cta) -->" in html
    assert "btn-ghost" not in html


def test_render_skeleton_html_falls_back_without_design_system_components() -> None:
    # design_system がNone、またはcomponentsが空の場合は元の固定literal markupにフォールバックする。
    html_no_ds = render_skeleton_html({"title": "Test Page Title"})
    ds_empty = _make_design_system([])
    html_empty_components = render_skeleton_html({"title": "Test Page Title"}, design_system=ds_empty)

    for html in (html_no_ds, html_empty_components):
        assert "<!-- Variant:" not in html
        assert '<button class="btn-primary">Action</button>' in html


def test_render_skeleton_html_is_deterministic() -> None:
    ghost = _make_component_spec(
        name="Ghost Action Button",
        role="Low-emphasis action",
        spec_summary="",
        component_type="button",
        variant_key="ghost",
        semantic_role="tertiary-action",
    )
    ds = _make_design_system([ghost])

    first = render_skeleton_html({"title": "Test Page Title"}, design_system=ds)
    second = render_skeleton_html({"title": "Test Page Title"}, design_system=ds)
    assert first == second

