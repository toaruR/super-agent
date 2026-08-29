"""harness/extract/tokens.py の単体テスト。

- build_design_tokens() が T3(analyze)のコンポーネント抽出結果から、
  W3C Design Tokens Community Group (DTCG) 形式のJSON（$type/$value を持つ）を
  color/typography/spacing/radius/shadow の5カテゴリで欠落なく生成すること。
- コンポーネント単位（button/card/nav/form）のグルーピングが保持されること。
- 入力にテキスト内容・画像URL等のコンテンツ情報が混入していても、トークンJSONには
  一切書き出されないこと（防御的実装）。
"""
from __future__ import annotations

import json

from harness.extract.analyze import BreakpointComponents, ComponentAnalysis, ComponentStyle
from harness.extract.tokens import TOKEN_CATEGORIES, build_design_tokens


def _sample_analysis() -> ComponentAnalysis:
    button = ComponentStyle(
        key="button:1",
        component_type="button",
        tag="button",
        classes=("btn", "btn-primary"),
        styles={
            "color": "#ffffff",
            "background-color": "#0055ff",
            "padding": "8px 16px",
            "font-size": "14px",
            "font-weight": "600",
            "border-radius": "4px",
        },
    )
    card = ComponentStyle(
        key="div:2",
        component_type="card",
        tag="div",
        classes=("card", "product-card"),
        styles={
            "border-radius": "8px",
            "box-shadow": "0 1px 2px rgba(0,0,0,0.1)",
            "padding": "16px",
        },
    )
    nav = ComponentStyle(
        key="nav:4",
        component_type="nav",
        tag="nav",
        classes=("site-nav",),
        styles={
            "display": "flex",
            "gap": "16px",
            "color": "#333333",
        },
    )
    form = ComponentStyle(
        key="form:5",
        component_type="form",
        tag="form",
        classes=(),
        styles={
            "display": "grid",
            "gap": "8px",
        },
    )

    return ComponentAnalysis(
        url="https://example.com/",
        breakpoints=[
            BreakpointComponents(
                viewport_width=1280,
                components=[button, card, nav, form],
            )
        ],
        responsive_diffs=[],
    )


def test_output_conforms_to_w3c_token_categories() -> None:
    tokens = build_design_tokens(_sample_analysis())

    assert set(TOKEN_CATEGORIES) == {"color", "typography", "spacing", "radius", "shadow"}
    for category in TOKEN_CATEGORIES:
        assert category in tokens, f"missing category: {category}"

    # 実データがある代表的なカテゴリには少なくとも1件のトークンが存在すること。
    assert any(tokens["color"][component_type] for component_type in tokens["color"])
    assert any(tokens["typography"][component_type] for component_type in tokens["typography"])
    assert any(tokens["spacing"][component_type] for component_type in tokens["spacing"])
    assert any(tokens["radius"][component_type] for component_type in tokens["radius"])
    assert any(tokens["shadow"][component_type] for component_type in tokens["shadow"])


def test_token_values_use_dollar_type_and_dollar_value_keys() -> None:
    tokens = build_design_tokens(_sample_analysis())

    def walk_leaf_tokens(node):
        if isinstance(node, dict) and "$type" in node and "$value" in node:
            yield node
            return
        if isinstance(node, dict):
            for value in node.values():
                yield from walk_leaf_tokens(value)

    leaf_tokens = list(walk_leaf_tokens(tokens))
    assert leaf_tokens, "expected at least one $type/$value token"
    for token in leaf_tokens:
        assert set(token.keys()) == {"$type", "$value"}
        assert isinstance(token["$type"], str) and token["$type"]
        assert token["$value"] not in (None, "")

    button_color = tokens["color"]["button"]["1280:button:1"]["color"]
    assert button_color == {"$type": "color", "$value": "#ffffff"}

    button_typography = tokens["typography"]["button"]["1280:button:1"]
    assert button_typography["$type"] == "typography"
    assert button_typography["$value"]["fontSize"] == "14px"
    assert button_typography["$value"]["fontWeight"] == "600"

    card_shadow = tokens["shadow"]["card"]["1280:div:2"]["box-shadow"]
    assert card_shadow == {"$type": "shadow", "$value": "0 1px 2px rgba(0,0,0,0.1)"}


def test_component_grouping_preserved_in_token_json() -> None:
    tokens = build_design_tokens(_sample_analysis())

    for category in TOKEN_CATEGORIES:
        assert set(tokens[category].keys()) >= {"button", "card", "nav", "form"}

    # ボタンのプロパティはbuttonグループにのみ現れ、他コンポーネント種別に漏れ出さない。
    assert "1280:button:1" in tokens["color"]["button"]
    assert "1280:button:1" not in tokens["color"]["card"]
    assert "1280:button:1" not in tokens["color"]["nav"]
    assert "1280:button:1" not in tokens["color"]["form"]

    # カードのradius/shadowはcardグループに、buttonのradiusはbuttonグループに、
    # それぞれ別々に保持される（コンポーネント単位のグルーピング）。
    assert "1280:div:2" in tokens["radius"]["card"]
    assert "1280:button:1" in tokens["radius"]["button"]
    assert "1280:div:2" in tokens["shadow"]["card"]
    assert "1280:div:2" not in tokens["shadow"]["button"]

    # navはgap(spacing)/color(color)のみ持ち、radius/shadowグループには何も追加されない。
    assert "1280:nav:4" in tokens["spacing"]["nav"]
    assert "1280:nav:4" in tokens["color"]["nav"]
    assert tokens["radius"]["nav"] == {}
    assert tokens["shadow"]["nav"] == {}


def test_content_information_is_never_written_to_token_json() -> None:
    """テキスト内容・画像URL等が万一 styles に混入しても、トークンJSONには出さない。"""
    tainted_button = ComponentStyle(
        key="button:9",
        component_type="button",
        tag="button",
        classes=("btn",),
        styles={
            "color": "#ffffff",
            # 既知のCSSプロパティ名の許可リストに無いキーはそもそも書き出されない。
            "content": '"Buy Now Today"',
            "innerText": "Buy Now Today",
            "href": "https://example.com/secret-page",
            "alt": "Company Logo",
            # url(...) を含む値は許可リスト内のプロパティ名であっても除外する。
            "background-image": 'url("https://example.com/secret-logo.png")',
        },
    )
    analysis = ComponentAnalysis(
        url="https://example.com/",
        breakpoints=[
            BreakpointComponents(viewport_width=1280, components=[tainted_button])
        ],
        responsive_diffs=[],
    )

    tokens = build_design_tokens(analysis)
    serialized = json.dumps(tokens, ensure_ascii=False)

    assert "Buy Now Today" not in serialized
    assert "secret-page" not in serialized
    assert "Company Logo" not in serialized
    assert "secret-logo.png" not in serialized
    assert "url(" not in serialized
    assert "innerText" not in serialized
    assert "href" not in serialized
    assert "alt" not in serialized

    # 許可リストに含まれる color は正しく書き出される。
    assert tokens["color"]["button"]["1280:button:9"]["color"]["$value"] == "#ffffff"
