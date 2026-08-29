"""harness/extract/generate.py の単体テスト。

- render_prompt() が、T4(tokens)が生成するW3C Design Tokens形式のJSONから、
  トークンカテゴリ・コンポーネント単位で見出しを分けたMarkdownを生成すること。
- 同一のトークンJSON入力に対しては常に同一のMarkdown文字列を返す(決定的)こと。
- トークンJSONに存在しないカテゴリ/コンポーネントがあっても出力が破綻しない(例外を
  送出せず、抽出結果なしを示す一文を出すだけで処理を継続する)こと。
"""
from __future__ import annotations

import collections

from harness.extract.generate import render_prompt
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
