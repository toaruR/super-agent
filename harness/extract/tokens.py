"""design-extract パイプラインの Tokenize 段階（W3C Design Tokens 形式への変換）。

- build_design_tokens(): harness.extract.analyze.analyze_components() の結果
  （コンポーネント単位に浄化済みのcomputed style）を、W3C Design Tokens Community Group
  (DTCG) 形式のJSONに変換する。各トークンは "$type"/"$value" を持つオブジェクトとして
  表現し、color/typography/spacing/radius/shadow の5カテゴリに分類する。
- 出力はカテゴリ→コンポーネント種別（button/card/nav/form等）→コンポーネントキーの順に
  グルーピングされ、どのコンポーネントに由来するトークンかが常に追跡できる。
- 著作権制約・防御的実装: CSSプロパティ名の許可リスト（_COLOR_PROPERTIES等）に
  含まれるプロパティのみをトークン化するため、テキスト内容やhref/src等の資産系情報が
  万一 ComponentStyle.styles に紛れ込んでいても、既知のスタイルプロパティ名でない限り
  トークンJSONには一切書き出されない。加えて url(...) を含む値（画像URL等）は
  カテゴリを問わず常に除外する。
"""
from __future__ import annotations

from typing import Any, Dict, Set

from harness.extract.analyze import COMPONENT_TYPES, ComponentAnalysis, ComponentStyle

# --- カテゴリ名 ---
COLOR = "color"
TYPOGRAPHY = "typography"
SPACING = "spacing"
RADIUS = "radius"
SHADOW = "shadow"

TOKEN_CATEGORIES = (COLOR, TYPOGRAPHY, SPACING, RADIUS, SHADOW)

# --- カテゴリごとの許可リスト（このプロパティ名以外は一切トークン化しない = 防御的実装）---
_COLOR_PROPERTIES: Set[str] = {
    "color",
    "background-color",
    "border-color",
    "border-top-color",
    "border-right-color",
    "border-bottom-color",
    "border-left-color",
    "outline-color",
    "fill",
    "stroke",
}

# DTCG の複合型 "typography" が定義する5つのサブプロパティに限定する。
_TYPOGRAPHY_PROPERTY_MAP: Dict[str, str] = {
    "font-family": "fontFamily",
    "font-size": "fontSize",
    "font-weight": "fontWeight",
    "letter-spacing": "letterSpacing",
    "line-height": "lineHeight",
}

_SPACING_PROPERTIES: Set[str] = {
    "padding",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "margin",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "gap",
    "row-gap",
    "column-gap",
}

_RADIUS_PROPERTIES: Set[str] = {
    "border-radius",
    "border-top-left-radius",
    "border-top-right-radius",
    "border-bottom-left-radius",
    "border-bottom-right-radius",
}

_SHADOW_PROPERTIES: Set[str] = {
    "box-shadow",
    "text-shadow",
}


def _is_content_leak(value: str) -> bool:
    """画像URL等、コンテンツ資産を保持しうる値かどうかを判定する（防御的な二重チェック）。

    harness.extract.analyze で既に url(...) を含む値は除外されているはずだが、
    本モジュール単体で呼び出された場合にも同じ保証を持たせるため、ここでも判定する。
    """
    return "url(" in (value or "").lower()


def _token(token_type: str, value: Any) -> Dict[str, Any]:
    return {"$type": token_type, "$value": value}


def _composite_key(viewport_width: int, component_key: str) -> str:
    return f"{viewport_width}:{component_key}"


def _seed_category(component_types: Set[str]) -> Dict[str, Dict[str, Any]]:
    """カテゴリ辞書を、既知の全コンポーネント種別で空グループのまま初期化する。

    該当プロパティが1つも見つからないコンポーネント種別（例: navにはradiusが無い）でも
    グルーピング構造自体は欠落なく残す。
    """
    return {component_type: {} for component_type in sorted(component_types)}


def _add_simple_tokens(
    category_group: Dict[str, Dict[str, Any]],
    component: ComponentStyle,
    composite_key: str,
    properties: Set[str],
    token_type: str,
) -> None:
    matched: Dict[str, Any] = {}
    for prop, value in component.styles.items():
        if prop not in properties:
            continue
        if _is_content_leak(value):
            continue
        matched[prop] = _token(token_type, value)
    if matched:
        category_group.setdefault(component.component_type, {})[composite_key] = matched


def _add_typography_token(
    category_group: Dict[str, Dict[str, Any]],
    component: ComponentStyle,
    composite_key: str,
) -> None:
    sub_values: Dict[str, Any] = {}
    for prop, sub_key in _TYPOGRAPHY_PROPERTY_MAP.items():
        value = component.styles.get(prop)
        if value is None or _is_content_leak(value):
            continue
        sub_values[sub_key] = value
    if sub_values:
        category_group.setdefault(component.component_type, {})[composite_key] = _token(
            TYPOGRAPHY, sub_values
        )


def build_design_tokens(analysis: ComponentAnalysis) -> Dict[str, Any]:
    """T3(analyze)のコンポーネント抽出結果をW3C Design Tokens形式のJSONに変換する。

    - 戻り値は color/typography/spacing/radius/shadow の5カテゴリを常に持つ
      （該当データが無いコンポーネント種別・カテゴリも空グループとして保持し、
      「欠落なくマッピングする」という要件を満たす）。
    - 各カテゴリ内は component_type（button/card/nav/form等）→
      "<viewport_width>:<component.key>" の順にグルーピングし、どのコンポーネント・
      どのブレークポイントに由来するトークンかを追跡できるようにする。
    - 各トークンは "$type"/"$value" を持つオブジェクトとして表現する
      （typographyのみDTCGの複合型に倣い、$value はサブプロパティの辞書とする）。
    - CSSプロパティ名の許可リストに含まれないもの（コンテンツ情報が万一含まれていた
      場合を含む）は一切書き出さない。
    """
    component_types: Set[str] = set(COMPONENT_TYPES)
    for bp in analysis.breakpoints:
        component_types.update(c.component_type for c in bp.components)

    tokens: Dict[str, Dict[str, Dict[str, Any]]] = {
        COLOR: _seed_category(component_types),
        TYPOGRAPHY: _seed_category(component_types),
        SPACING: _seed_category(component_types),
        RADIUS: _seed_category(component_types),
        SHADOW: _seed_category(component_types),
    }

    for bp in analysis.breakpoints:
        for component in bp.components:
            composite_key = _composite_key(bp.viewport_width, component.key)
            _add_simple_tokens(tokens[COLOR], component, composite_key, _COLOR_PROPERTIES, COLOR)
            _add_typography_token(tokens[TYPOGRAPHY], component, composite_key)
            _add_simple_tokens(tokens[SPACING], component, composite_key, _SPACING_PROPERTIES, "dimension")
            _add_simple_tokens(tokens[RADIUS], component, composite_key, _RADIUS_PROPERTIES, "dimension")
            _add_simple_tokens(tokens[SHADOW], component, composite_key, _SHADOW_PROPERTIES, SHADOW)

    return tokens
