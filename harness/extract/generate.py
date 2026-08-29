"""design-extract パイプラインの Generate 段階（デザイントークンJSON→Markdownデザインプロンプト）。

- render_prompt(): harness.extract.tokens.build_design_tokens() が生成したW3C Design
  Tokens形式のJSON（category -> component_type -> composite_key -> token）を中間表現として
  受け取り、テンプレートエンジン（自然言語生成LLMではなく機械的な文字列組み立て）で
  Markdown形式のデザインプロンプトを生成する。生成は文字列テンプレートの組み立てのみで
  行い、LLM呼び出しは一切行わない。
- 出力はトークンカテゴリ（color/typography/spacing/radius/shadow）ごとに見出しを分け、
  さらにその中でコンポーネント種別（button/card/nav/form等）ごとに見出しを分けるため、
  Claude/Codex/Figmaのいずれの利用先であっても、見出し構造から機械的にセクションを
  たどりやすい。
- 同一のトークンJSON入力に対しては、辞書のキー挿入順に依存せず常に同一のMarkdown文字列を
  返す（カテゴリ・コンポーネント種別・トークンキーをすべて明示的にソートしてから組み立てる）。
- トークンJSONに存在しないカテゴリ/コンポーネントがあっても例外を送出せず、
  「抽出結果なし」を示す一文を出力するだけで処理を継続する（防御的実装）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from harness.extract.tokens import TOKEN_CATEGORIES

_NO_TOKENS_NOTE = "_No tokens extracted for this category._"

_CATEGORY_TITLES: Dict[str, str] = {
    "color": "Color",
    "typography": "Typography",
    "spacing": "Spacing",
    "radius": "Radius",
    "shadow": "Shadow",
}


def _title_for_category(category: str) -> str:
    return _CATEGORY_TITLES.get(category, category.replace("-", " ").replace("_", " ").title())


def _title_for_component(component_type: str) -> str:
    return component_type.replace("-", " ").replace("_", " ").title() or component_type


def _is_leaf_token(node: Any) -> bool:
    """$type/$value のみを持つDTCGトークン(葉)かどうかを判定する。"""
    return isinstance(node, dict) and set(node.keys()) == {"$type", "$value"}


def _format_value(value: Any) -> str:
    """typographyのような複合型($valueがサブプロパティの辞書)も含め、値を1行の文字列にする。"""
    if isinstance(value, dict):
        parts = ", ".join(f"{key}: {value[key]}" for key in sorted(value.keys()))
        return f"{{ {parts} }}"
    return str(value)


def _render_component_token_lines(composite_tokens: Any) -> List[str]:
    """1コンポーネント種別分のトークン(composite_key単位)を箇条書き行のリストにする。

    composite_tokens は以下の2形状のいずれかを取りうる(harness.extract.tokens 参照):
    - typographyのように composite_key -> {$type, $value} (葉トークンを直接持つ)
    - それ以外のカテゴリのように composite_key -> {prop -> {$type, $value}}
    未知の形状(想定外の値)は静かに無視し、破綻させない。
    """
    if not isinstance(composite_tokens, dict):
        return []

    lines: List[str] = []
    for composite_key in sorted(composite_tokens.keys()):
        entry = composite_tokens[composite_key]
        if _is_leaf_token(entry):
            lines.append(f"- `{composite_key}` ({entry['$type']}): {_format_value(entry['$value'])}")
        elif isinstance(entry, dict):
            for prop in sorted(entry.keys()):
                token = entry[prop]
                if _is_leaf_token(token):
                    lines.append(
                        f"- `{composite_key}` / `{prop}` ({token['$type']}): "
                        f"{_format_value(token['$value'])}"
                    )
    return lines


def _ordered_categories(tokens: Dict[str, Any]) -> List[str]:
    """既知のカテゴリ(TOKEN_CATEGORIES順)を優先し、未知のカテゴリはアルファベット順で末尾に追加する。"""
    known = list(TOKEN_CATEGORIES)
    unknown = sorted(category for category in tokens.keys() if category not in known)
    return known + unknown


def render_prompt(tokens: Dict[str, Any], *, url: Optional[str] = None) -> str:
    """デザイントークンJSONから決定的なMarkdown形式のデザインプロンプトを生成する。

    - tokens は harness.extract.tokens.build_design_tokens() が返すW3C Design Tokens形式の
      JSON(dict)を想定するが、一部カテゴリ/コンポーネントが欠落していても例外を送出しない。
    - 自然言語生成LLMは一切呼び出さず、テンプレート文字列とソート済みキーの組み立てのみで
      Markdownを構築するため、同一入力に対しては常に同一の文字列(バイト単位)を返す。
    - url を渡すと、抽出元サイトの参照情報として先頭に付記する(省略可)。
    """
    tokens = tokens or {}

    lines: List[str] = ["# Design Prompt"]
    if url:
        lines.append("")
        lines.append(f"Source: {url}")
    lines.append("")
    lines.append(
        "This document is generated mechanically from extracted design tokens. "
        "It is intended to be pasted into Claude, Codex, or Figma to reproduce the "
        "visual design system (color, typography, spacing, radius, shadow) of the "
        "components described below."
    )

    for category in _ordered_categories(tokens):
        category_tokens = tokens.get(category) or {}
        lines.append("")
        lines.append(f"## {_title_for_category(category)}")

        component_types = sorted(category_tokens.keys()) if isinstance(category_tokens, dict) else []
        rendered_any_component = False
        for component_type in component_types:
            token_lines = _render_component_token_lines(category_tokens.get(component_type))
            if not token_lines:
                continue
            rendered_any_component = True
            lines.append("")
            lines.append(f"### {_title_for_component(component_type)}")
            lines.extend(token_lines)

        if not rendered_any_component:
            lines.append("")
            lines.append(_NO_TOKENS_NOTE)

    return "\n".join(lines) + "\n"
