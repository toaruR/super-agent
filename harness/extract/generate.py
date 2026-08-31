"""design-extract パイプラインの Generate 段階（デザイントークンJSON→MarkdownデザインプロンプトおよびRefero Styles準拠DESIGN.md）。

- render_prompt(): harness.extract.tokens.build_design_tokens() が生成したW3C Design
  Tokens形式のJSONを受け取り、決定論的な Markdown 形式のデザインプロンプト（prompt.md）
  を生成する。
- render_design_md(): Refero Styles (https://styles.refero.design/) のサンプルと同等水準の
  包括的なスタイルリファレンスドキュメント（DESIGN.md）を生成する。
  (Brand, Tagline, Theme, Aesthetic, Color Tokens, Typography Tokens, Spacing & Shapes,
  Components, Do's and Don'ts, Surfaces, Elevation, Imagery, Layout, Agent Prompt Guide,
  Similar Brands, Quick Start CSS/Tailwind v4)
- 同一のトークンJSON入力に対しては常に決定的な同一のMarkdown文字列を返す。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from harness.extract.analyze import DesignSystemAnalysis, DesignSystemExtractor
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
    return isinstance(node, dict) and set(node.keys()) == {"$type", "$value"}


def _format_value(value: Any) -> str:
    if isinstance(value, dict):
        parts = ", ".join(f"{key}: {value[key]}" for key in sorted(value.keys()))
        return f"{{{parts}}}" if not parts else f"{{ {parts} }}"
    return str(value)


def _render_component_token_lines(composite_tokens: Any) -> List[str]:
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
    known = list(TOKEN_CATEGORIES)
    unknown = sorted(category for category in tokens.keys() if category not in known and category != "design_system")
    return known + unknown


def render_prompt(tokens: Dict[str, Any], *, url: Optional[str] = None) -> str:
    """デザイントークンJSONから決定的なMarkdown形式のデザインプロンプトを生成する。"""
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


def render_design_md(
    tokens: Dict[str, Any],
    *,
    url: Optional[str] = None,
    design_system: Optional[DesignSystemAnalysis] = None,
) -> str:
    """デザイントークンまたは DesignSystemAnalysis から Refero Styles 準拠の DESIGN.md を生成する。"""
    ds = design_system

    # design_system が渡されていない場合、tokens["design_system"] または自動抽出を試みる
    if ds is None:
        raw_ds = tokens.get("design_system") if isinstance(tokens, dict) else None
        if isinstance(raw_ds, dict):
            # 辞書から最低限の構成をレンダリング
            brand_name = raw_ds.get("brand_name", "Product")
            tagline = raw_ds.get("tagline", "midnight precision instrument")
            theme = raw_ds.get("theme", "dark")
            aesthetic = raw_ds.get("aesthetic_summary", "")

            lines: List[str] = [
                f"# {brand_name} — Style Reference",
                f"> {tagline}",
                "",
                f"**Theme:** {theme}",
                "",
                aesthetic,
                "",
                "## Tokens — Colors",
                "",
                "| Name | Value | Token | Role |",
                "|------|-------|-------|------|",
            ]
            for c in raw_ds.get("colors", []):
                lines.append(f"| {c.get('name')} | `{c.get('hex')}` | `{c.get('token')}` | {c.get('role')} |")
            lines.append("")

            lines.append("## Tokens — Typography")
            lines.append("")
            for f in raw_ds.get("font_families", []):
                lines.append(f"### {f.get('name')} — {f.get('usage_role')} · `{f.get('token')}`")
                lines.append(f"- **Substitute:** {f.get('substitute')}")
                lines.append(f"- **Weights:** {', '.join(f.get('weights', []))}")
                lines.append(f"- **Sizes:** {', '.join(f.get('sizes', []))}")
                lines.append(f"- **Line height:** {f.get('line_height_range')}")
                lines.append(f"- **Letter spacing:** {f.get('letter_spacing_summary')}")
                if f.get("opentype_features"):
                    lines.append(f"- **OpenType features:** `{f.get('opentype_features')}`")
                lines.append(f"- **Role:** {f.get('usage_role')}")
                lines.append("")

            lines.append("### Type Scale")
            lines.append("")
            lines.append("| Role | Size | Line Height | Letter Spacing | Token |")
            lines.append("|------|------|-------------|----------------|-------|")
            for s in raw_ds.get("type_scale", []):
                lines.append(f"| {s.get('role')} | {s.get('size')} | {s.get('line_height')} | {s.get('letter_spacing')} | `{s.get('token')}` |")
            lines.append("")

            sp = raw_ds.get("spacing_shapes", {})
            lines.append("## Tokens — Spacing & Shapes")
            lines.append("")
            lines.append(f"**Base unit:** {sp.get('base_unit', '4px')}")
            lines.append("")
            lines.append(f"**Density:** {sp.get('density', 'compact')}")
            lines.append("")
            lines.append("### Spacing Scale")
            lines.append("")
            lines.append("| Name | Value | Token |")
            lines.append("|------|-------|-------|")
            for item in sp.get("spacing_scale", []):
                lines.append(f"| {item[0]} | {item[1]} | `{item[2]}` |")
            lines.append("")
            lines.append("### Border Radius")
            lines.append("")
            lines.append("| Element | Value |")
            lines.append("|---------|-------|")
            for item in sp.get("border_radius", []):
                lines.append(f"| {item[0]} | {item[1]} |")
            lines.append("")
            lines.append("### Shadows")
            lines.append("")
            lines.append("| Name | Value | Token |")
            lines.append("|------|-------|-------|")
            for item in sp.get("shadows", []):
                lines.append(f"| {item[0]} | `{item[1]}` | `{item[2]}` |")
            lines.append("")
            lines.append("### Layout")
            lines.append("")
            lines.append(f"- **Page max-width:** {sp.get('page_max_width', '1200px')}")
            lines.append(f"- **Section gap:** {sp.get('section_gap', '96px')}")
            lines.append(f"- **Card padding:** {sp.get('card_padding', '24px')}")
            lines.append(f"- **Element gap:** {sp.get('element_gap', '8px')}")
            lines.append("")

            lines.append("## Components")
            lines.append("")
            for comp in raw_ds.get("components", []):
                lines.append(f"### {comp.get('name')}")
                lines.append(f"**Role:** {comp.get('role')}")
                lines.append("")
                lines.append(comp.get("spec_summary", ""))
                lines.append("")

            principles = raw_ds.get("principles", {})
            lines.append("## Do's and Don'ts")
            lines.append("")
            lines.append("### Do")
            for do in principles.get("dos", []):
                lines.append(f"- {do}")
            lines.append("")
            lines.append("### Don't")
            for dont in principles.get("donts", []):
                lines.append(f"- {dont}")
            lines.append("")

            lines.append("## Surfaces")
            lines.append("")
            lines.append("| Level | Name | Value | Purpose |")
            lines.append("|-------|------|-------|---------|")
            for surf in raw_ds.get("surfaces", []):
                lines.append(f"| {surf.get('level')} | {surf.get('name')} | `{surf.get('value')}` | {surf.get('purpose')} |")
            lines.append("")

            lines.append("## Elevation")
            lines.append("")
            lines.append(raw_ds.get("elevation_summary", ""))
            lines.append("")

            lines.append("## Imagery")
            lines.append("")
            lines.append(raw_ds.get("imagery_summary", ""))
            lines.append("")

            lines.append("## Layout")
            lines.append("")
            lines.append(raw_ds.get("layout_summary", ""))
            lines.append("")

            ap = raw_ds.get("agent_prompts", {})
            lines.append("## Agent Prompt Guide")
            lines.append("")
            lines.append("**Quick Color Reference:**")
            for k, v in ap.get("quick_colors", {}).items():
                lines.append(f"- {k}: {v}")
            lines.append("")
            lines.append("**3-5 Example Component Prompts:**")
            lines.append("")
            for i, (p_title, p_body) in enumerate(ap.get("component_prompts", []), 1):
                lines.append(f"{i}. **{p_title}:** {p_body}")
                lines.append("")

            lines.append("## Similar Brands")
            lines.append("")
            for b in raw_ds.get("similar_brands", []):
                lines.append(f"- **{b.get('name')}** — {b.get('description')}")
            lines.append("")

            lines.append("## Quick Start")
            lines.append("")
            lines.append("### CSS Custom Properties")
            lines.append("")
            lines.append("```css")
            lines.append(raw_ds.get("css_custom_properties", ""))
            lines.append("```")
            lines.append("")
            lines.append("### Tailwind v4")
            lines.append("")
            lines.append("```css")
            lines.append(raw_ds.get("tailwind_v4_theme", ""))
            lines.append("```")
            lines.append("")

            return "\n".join(lines)

    if ds is not None:
        lines: List[str] = [
            f"# {ds.brand_name} — Style Reference",
            f"> {ds.tagline}",
            "",
            f"**Theme:** {ds.theme}",
            "",
            ds.aesthetic_summary,
            "",
            "## Tokens — Colors",
            "",
            "| Name | Value | Token | Role |",
            "|------|-------|-------|------|",
        ]
        for c in ds.colors:
            lines.append(f"| {c.name} | `{c.hex_value}` | `{c.token_name}` | {c.role} |")
        lines.append("")

        lines.append("## Tokens — Typography")
        lines.append("")
        for f in ds.font_families:
            lines.append(f"### {f.name} — {f.usage_role} · `{f.token_name}`")
            lines.append(f"- **Substitute:** {f.substitute}")
            lines.append(f"- **Weights:** {', '.join(f.weights)}")
            lines.append(f"- **Sizes:** {', '.join(f.sizes)}")
            lines.append(f"- **Line height:** {f.line_height_range}")
            lines.append(f"- **Letter spacing:** {f.letter_spacing_summary}")
            if f.opentype_features:
                lines.append(f"- **OpenType features:** `{f.opentype_features}`")
            lines.append(f"- **Role:** {f.usage_role}")
            lines.append("")

        lines.append("### Type Scale")
        lines.append("")
        lines.append("| Role | Size | Line Height | Letter Spacing | Token |")
        lines.append("|------|------|-------------|----------------|-------|")
        for s in ds.type_scale:
            lines.append(f"| {s.role} | {s.size} | {s.line_height} | {s.letter_spacing} | `{s.token_name}` |")
        lines.append("")

        sp = ds.spacing_shapes
        lines.append("## Tokens — Spacing & Shapes")
        lines.append("")
        lines.append(f"**Base unit:** {sp.base_unit}")
        lines.append("")
        lines.append(f"**Density:** {sp.density}")
        lines.append("")
        lines.append("### Spacing Scale")
        lines.append("")
        lines.append("| Name | Value | Token |")
        lines.append("|------|-------|-------|")
        for item in sp.spacing_scale:
            lines.append(f"| {item[0]} | {item[1]} | `{item[2]}` |")
        lines.append("")
        lines.append("### Border Radius")
        lines.append("")
        lines.append("| Element | Value |")
        lines.append("|---------|-------|")
        for item in sp.border_radius:
            lines.append(f"| {item[0]} | {item[1]} |")
        lines.append("")
        lines.append("### Shadows")
        lines.append("")
        lines.append("| Name | Value | Token |")
        lines.append("|------|-------|-------|")
        for item in sp.shadows:
            lines.append(f"| {item[0]} | `{item[1]}` | `{item[2]}` |")
        lines.append("")
        lines.append("### Layout")
        lines.append("")
        lines.append(f"- **Page max-width:** {sp.page_max_width}")
        lines.append(f"- **Section gap:** {sp.section_gap}")
        lines.append(f"- **Card padding:** {sp.card_padding}")
        lines.append(f"- **Element gap:** {sp.element_gap}")
        lines.append("")

        lines.append("## Components")
        lines.append("")
        for comp in ds.components:
            lines.append(f"### {comp.name}")
            lines.append(f"**Role:** {comp.role}")
            lines.append("")
            lines.append(comp.spec_summary)
            lines.append("")

        lines.append("## Do's and Don'ts")
        lines.append("")
        lines.append("### Do")
        for do in ds.principles.dos:
            lines.append(f"- {do}")
        lines.append("")
        lines.append("### Don't")
        for dont in ds.principles.donts:
            lines.append(f"- {dont}")
        lines.append("")

        lines.append("## Surfaces")
        lines.append("")
        lines.append("| Level | Name | Value | Purpose |")
        lines.append("|-------|------|-------|---------|")
        for surf in ds.surfaces:
            lines.append(f"| {surf.level} | {surf.name} | `{surf.value}` | {surf.purpose} |")
        lines.append("")

        lines.append("## Elevation")
        lines.append("")
        lines.append(ds.elevation_summary)
        lines.append("")

        lines.append("## Imagery")
        lines.append("")
        lines.append(ds.imagery_summary)
        lines.append("")

        lines.append("## Layout")
        lines.append("")
        lines.append(ds.layout_summary)
        lines.append("")

        lines.append("## Agent Prompt Guide")
        lines.append("")
        lines.append("**Quick Color Reference:**")
        for k, v in ds.agent_prompts.quick_colors.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("**3-5 Example Component Prompts:**")
        lines.append("")
        for i, (p_title, p_body) in enumerate(ds.agent_prompts.component_prompts, 1):
            lines.append(f"{i}. **{p_title}:** {p_body}")
            lines.append("")

        lines.append("## Similar Brands")
        lines.append("")
        for b in ds.similar_brands:
            lines.append(f"- **{b.name}** — {b.description}")
        lines.append("")

        lines.append("## Quick Start")
        lines.append("")
        lines.append("### CSS Custom Properties")
        lines.append("")
        lines.append("```css")
        lines.append(ds.css_custom_properties)
        lines.append("```")
        lines.append("")
        lines.append("### Tailwind v4")
        lines.append("")
        lines.append("```css")
        lines.append(ds.tailwind_v4_theme)
        lines.append("```")
        lines.append("")

        return "\n".join(lines)

    # どちらもない場合はレガシープロンプトを返す
    return render_prompt(tokens, url=url)


def render_tokens_css(
    tokens: Dict[str, Any],
    design_system: Optional[DesignSystemAnalysis] = None,
) -> str:
    """W3C Design Tokens または DesignSystemAnalysis から決定的な :root CSSカスタムプロパティ文字列を生成する。"""
    # 1. design_system オブジェクトが渡されている場合
    if design_system is not None and getattr(design_system, "css_custom_properties", None):
        css = design_system.css_custom_properties.strip()
        if not css.startswith(":root"):
            css = f":root {{\n{css}\n}}"
        return css + "\n"

    # 2. tokens に design_system (DesignSystemAnalysis または dict) が含まれている場合
    if isinstance(tokens, dict):
        raw_ds = tokens.get("design_system")
        if isinstance(raw_ds, DesignSystemAnalysis) and raw_ds.css_custom_properties:
            css = raw_ds.css_custom_properties.strip()
            if not css.startswith(":root"):
                css = f":root {{\n{css}\n}}"
            return css + "\n"
        elif isinstance(raw_ds, dict) and raw_ds.get("css_custom_properties"):
            css = str(raw_ds["css_custom_properties"]).strip()
            if not css.startswith(":root"):
                css = f":root {{\n{css}\n}}"
            return css + "\n"

    # 3. W3C Design Tokens から :root カスタムプロパティを構築する
    lines: List[str] = [":root {"]
    seen_props = set()

    if isinstance(tokens, dict):
        for category in _ordered_categories(tokens):
            cat_dict = tokens.get(category)
            if not isinstance(cat_dict, dict):
                continue
            for comp_type in sorted(cat_dict.keys()):
                comp_dict = cat_dict[comp_type]
                if not isinstance(comp_dict, dict):
                    continue
                for item_key in sorted(comp_dict.keys()):
                    entry = comp_dict[item_key]
                    if _is_leaf_token(entry):
                        prop_name = f"--{category}-{comp_type}-{item_key}".replace(":", "-").replace("_", "-")
                        if prop_name not in seen_props:
                            seen_props.add(prop_name)
                            val = entry.get("$value")
                            lines.append(f"  {prop_name}: {val};")
                    elif isinstance(entry, dict):
                        for sub_prop in sorted(entry.keys()):
                            sub_token = entry[sub_prop]
                            if _is_leaf_token(sub_token):
                                prop_name = f"--{category}-{comp_type}-{sub_prop}".replace(":", "-").replace("_", "-")
                                if prop_name not in seen_props:
                                    seen_props.add(prop_name)
                                    val = sub_token.get("$value")
                                    lines.append(f"  {prop_name}: {val};")

    # もしトークンが空・あるいは定義なしの場合は標準的なベーストークンを出力
    if len(lines) == 1:
        lines.extend([
            "  --color-brand: #e4f222;",
            "  --color-surface: #0f1011;",
            "  --color-text: #ffffff;",
            "  --font-primary: 'Inter Variable', sans-serif;",
            "  --radius-md: 6px;",
            "  --spacing-unit: 4px;",
        ])

    lines.append("}")
    return "\n".join(lines) + "\n"


def _prop(comp: Optional[Any], key: str, default: str) -> str:
    """ComponentSpec.properties から構造化された値を取得する（正規表現パース不要）。"""
    if comp is None:
        return default
    val = (getattr(comp, "properties", None) or {}).get(key)
    return val if val else default


def _find_component(
    components: List[Any],
    component_type: str,
    variant_key: Optional[str] = None,
    name_hint: Optional[str] = None,
    strict: bool = False,
) -> Optional[Any]:
    """`component_type`/`variant_key`/`name_hint` に一致するコンポーネントを探す。

    `strict=True` の場合、variant_key/name_hint に一致するものが無ければ
    (同一 component_type の別バリアントへの誤フォールバックを避けるため) None を返す。
    button のように複数バリアントが並存しうる component_type ではこれを使う。
    card/nav/input/badge のようにバリアント区別のない単一インスタンス型では
    strict=False のまま「同type内の先頭」へのフォールバックを許容する。
    """
    candidates = [c for c in components if getattr(c, "component_type", None) == component_type]
    if variant_key is not None:
        for c in candidates:
            if getattr(c, "variant_key", None) == variant_key:
                return c
    if name_hint is not None:
        for c in candidates:
            if name_hint in getattr(c, "name", ""):
                return c
    if strict:
        return None
    return candidates[0] if candidates else None


def render_components_css(
    design_system: Optional[DesignSystemAnalysis] = None,
) -> str:
    """主要コンポーネント（.btn-primary, .btn-secondary, .card-surface, .nav-container, .input-field, .badge 等）の具現化CSSルールを生成する。

    `design_system.components`（`ComponentSpec.properties`）から実測由来の値を構造化フィールド
    として取得し、対象サイトごとに変動する。データが乏しい場合のみ既存のハードコード値へ
    フォールバックするため、モデル/シード差異による出力の揺らぎは発生しない。
    """
    theme = getattr(design_system, "theme", "light") if design_system else "light"
    is_dark = (theme == "dark")

    brand_hex = "#ff4800" if not is_dark else "#e4f222"
    on_primary_hex = "#ffffff" if not is_dark else "#08090a"

    if design_system is not None and getattr(design_system, "colors", None):
        brand_tok = next((c for c in design_system.colors if c.category == "brand" and c.token_name == "--color-primary"), None)
        if not brand_tok:
            brand_tok = next((c for c in design_system.colors if c.category == "brand"), None)
        if brand_tok:
            brand_hex = brand_tok.hex_value

        on_prim_tok = next((c for c in design_system.colors if c.token_name == "--color-on-primary"), None)
        if on_prim_tok:
            on_primary_hex = on_prim_tok.hex_value

    brand_color = f"var(--color-primary, {brand_hex})"
    on_primary_color = f"var(--color-on-primary, {on_primary_hex})"

    components: List[Any] = list(getattr(design_system, "components", None) or []) if design_system is not None else []
    primary_comp = _find_component(components, "button", variant_key="primary", name_hint="Primary", strict=True)
    secondary_comp = _find_component(components, "button", variant_key="secondary", name_hint="Secondary", strict=True)
    ghost_comp = _find_component(components, "button", variant_key="ghost", name_hint="Ghost", strict=True)
    card_comp = _find_component(components, "card")
    nav_comp = _find_component(components, "nav")
    input_comp = _find_component(components, "input")
    badge_comp = _find_component(components, "badge")

    # ボタン（Primary）: 背景/文字色はセマンティックトークンに委ね、形状のみ実測値を反映する。
    btn_padding = _prop(primary_comp, "padding", "12px 24px")
    btn_radius = f"var(--radius-buttons, {_prop(primary_comp, 'border_radius', 'var(--radius-md, 6px)')})"
    btn_fs = f"var(--text-body-sm, {_prop(primary_comp, 'font_size', '15px')})"
    btn_fw = _prop(primary_comp, "font_weight", "500")
    btn_lh = _prop(primary_comp, "line_height", "1.5")

    # ボタン（Secondary）: 実測の outline バケットがあればその配色を、無ければ既定ヒューリスティックを使う。
    sec_bg_default = "transparent" if is_dark else "var(--surface-canvas, #ffffff)"
    sec_text_default = "var(--color-text-body, #d0d6e0)" if is_dark else brand_color
    sec_border_default = "1px solid var(--color-border, #23252a)" if is_dark else f"1px solid {brand_color}"
    sec_bg = _prop(secondary_comp, "background", sec_bg_default)
    sec_text = _prop(secondary_comp, "text", sec_text_default)
    sec_border = _prop(secondary_comp, "border", sec_border_default)

    # ボタン（Ghost）: 実測の text バケットが一定数あるサイトでのみ生成される追加バリアント。
    has_ghost = ghost_comp is not None
    ghost_text = _prop(ghost_comp, "text", "var(--color-text-muted, #8a8f98)" if is_dark else "var(--color-secondary, #6b7280)")

    # 背景/文字色は「実測値」を、必ず対応する意味論的CSS変数(var())のフォールバックとして
    # 埋め込む（reproduce-ui スキルの「常に var() を使う、色を直書きしない」規約に、
    # コンポーネント固有の実測値と tokens.css 側のトークンカスケードを両立させるため）。
    # border など「幅+スタイル+色」の複合値は _prop() の実測値をそのまま使う
    # (compound文字列を var() でラップすると値の途中に構造が入り込み壊れるため)。
    card_padding = f"var(--card-padding, {_prop(card_comp, 'padding', '24px')})"
    card_radius = f"var(--radius-cards, {_prop(card_comp, 'border_radius', 'var(--radius-xl, 12px)')})"
    card_shadow_default = "0 2px 4px rgba(0, 0, 0, 0.04)" if not is_dark else "0 2px 4px rgba(0, 0, 0, 0.4)"
    card_shadow = f"var(--shadow-sm, {_prop(card_comp, 'box_shadow', card_shadow_default)})"
    card_bg = f"var(--surface-surface, {_prop(card_comp, 'background', '#0f1011' if is_dark else '#ffffff')})"
    card_border = _prop(card_comp, "border", "1px solid var(--color-border, #23252a)" if is_dark else "1px solid var(--color-border, #f0f0f0)")

    nav_bg = f"var(--surface-canvas, {_prop(nav_comp, 'background', '#08090a' if is_dark else '#ffffff')})"
    nav_border = _prop(nav_comp, "border_bottom", "1px solid var(--color-border, #23252a)" if is_dark else "1px solid var(--color-border, #f0f0f0)")
    nav_max_width = f"var(--page-max-width, {_prop(nav_comp, 'max_width', '1200px')})"

    input_bg = f"var(--surface-canvas, {_prop(input_comp, 'background', '#ffffff')})" if not is_dark else _prop(input_comp, "background", "rgba(255, 255, 255, 0.02)")
    input_border = _prop(input_comp, "border", "1px solid var(--color-border, rgba(255, 255, 255, 0.08))" if is_dark else "1px solid var(--color-border-strong, #d1d5db)")
    input_radius = f"var(--radius-inputs, {_prop(input_comp, 'border_radius', '6px')})"
    input_text = f"var(--color-text-body, {_prop(input_comp, 'text', '#d0d6e0')})" if is_dark else f"var(--color-body, {_prop(input_comp, 'text', '#1f1f1f')})"

    badge_bg = _prop(badge_comp, "background", "rgba(255, 255, 255, 0.05)") if is_dark else f"var(--surface-subtle, {_prop(badge_comp, 'background', '#f3f4f6')})"
    badge_text = f"var(--color-text-muted, {_prop(badge_comp, 'text', '#8a8f98')})" if is_dark else _prop(badge_comp, "text", brand_color)
    badge_radius = f"var(--radius-badges, {_prop(badge_comp, 'border_radius', '9999px' if not is_dark else '4px')})"

    ghost_block: List[str] = []
    if has_ghost:
        ghost_pad = _prop(ghost_comp, "padding", btn_padding)
        ghost_fs = f"var(--text-body-sm, {_prop(ghost_comp, 'font_size', '15px')})"
        ghost_fw = _prop(ghost_comp, "font_weight", "500")
        ghost_hover_bg = "rgba(255, 255, 255, 0.05)" if is_dark else "var(--surface-subtle, #f3f4f6)"
        ghost_block = [
            "",
            ".btn-ghost {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  justify-content: center;",
            f"  padding: {ghost_pad};",
            "  background-color: transparent;",
            f"  color: {ghost_text};",
            "  border: 1px solid transparent;",
            f"  border-radius: {btn_radius};",
            "  font-family: var(--font-primary, sans-serif);",
            f"  font-size: {ghost_fs};",
            f"  font-weight: {ghost_fw};",
            "  cursor: pointer;",
            "  transition: background-color 0.15s ease, color 0.15s ease;",
            "}",
            ".btn-ghost:hover {",
            f"  background-color: {ghost_hover_bg};",
            "}",
        ]

    if is_dark:
        css_blocks = [
            "/* Main Components CSS (Dark Theme) */",
            "",
            "/* Button Components */",
            ".btn-primary {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  justify-content: center;",
            f"  padding: {btn_padding};",
            f"  background-color: {brand_color};",
            f"  color: {on_primary_color};",
            f"  border-radius: {btn_radius};",
            "  font-family: var(--font-primary, sans-serif);",
            f"  font-size: {btn_fs};",
            f"  font-weight: {btn_fw};",
            f"  line-height: {btn_lh};",
            "  border: 1px solid transparent;",
            "  cursor: pointer;",
            "  transition: background-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;",
            "}",
            ".btn-primary:hover {",
            "  opacity: 0.92;",
            "  transform: translateY(-1px);",
            "}",
            "",
            ".btn-secondary {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  justify-content: center;",
            f"  padding: {btn_padding};",
            f"  background-color: {sec_bg};",
            f"  color: {sec_text};",
            f"  border: {sec_border};",
            f"  border-radius: {btn_radius};",
            "  font-family: var(--font-primary, sans-serif);",
            f"  font-size: {btn_fs};",
            f"  font-weight: {btn_fw};",
            "  cursor: pointer;",
            "  transition: background-color 0.15s ease, border-color 0.15s ease, transform 0.15s ease;",
            "}",
            ".btn-secondary:hover {",
            "  background-color: rgba(255, 255, 255, 0.05);",
            "  border-color: var(--color-text-muted, #8a8f98);",
            "  transform: translateY(-1px);",
            "}",
            *ghost_block,
            "",
            "/* Card Components */",
            ".card-surface {",
            f"  background-color: {card_bg};",
            f"  border: {card_border};",
            f"  border-radius: {card_radius};",
            f"  padding: {card_padding};",
            f"  box-shadow: {card_shadow};",
            "}",
            "",
            "/* Navigation Components */",
            ".nav-container {",
            "  display: flex;",
            "  align-items: center;",
            "  justify-content: space-between;",
            "  padding: 16px var(--spacing-24, 24px);",
            f"  background-color: {nav_bg};",
            f"  border-bottom: {nav_border};",
            f"  max-width: {nav_max_width};",
            "  margin: 0 auto;",
            "}",
            "",
            "/* Form Input Components */",
            ".input-field {",
            "  width: 100%;",
            "  padding: 12px 14px;",
            f"  background-color: {input_bg};",
            f"  border: {input_border};",
            f"  border-radius: {input_radius};",
            f"  color: {input_text};",
            "  font-family: var(--font-primary, sans-serif);",
            "  font-size: 14px;",
            "  outline: none;",
            "  transition: border-color 0.15s ease, box-shadow 0.15s ease;",
            "}",
            ".input-field:focus {",
            f"  border-color: {brand_color};",
            "  box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.05);",
            "}",
            "",
            "/* Badge Components */",
            ".badge {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  padding: 4px 10px;",
            f"  background-color: {badge_bg};",
            f"  color: {badge_text};",
            f"  border-radius: {badge_radius};",
            "  font-size: 12px;",
            "  font-weight: 500;",
            "}",
        ]
    else:
        css_blocks = [
            "/* Main Components CSS (Light Theme) */",
            "",
            "/* Button Components */",
            ".btn-primary {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  justify-content: center;",
            f"  padding: {btn_padding};",
            f"  background-color: {brand_color};",
            f"  color: {on_primary_color};",
            f"  border-radius: {btn_radius};",
            "  font-family: var(--font-primary, sans-serif);",
            f"  font-size: {btn_fs};",
            f"  font-weight: {btn_fw};",
            f"  line-height: {btn_lh};",
            "  border: 1px solid transparent;",
            "  cursor: pointer;",
            "  transition: background-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;",
            "}",
            ".btn-primary:hover {",
            "  opacity: 0.92;",
            "  transform: translateY(-1px);",
            "  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);",
            "}",
            "",
            ".btn-secondary {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  justify-content: center;",
            f"  padding: {btn_padding};",
            f"  background-color: {sec_bg};",
            f"  color: {sec_text};",
            f"  border: {sec_border};",
            f"  border-radius: {btn_radius};",
            "  font-family: var(--font-primary, sans-serif);",
            f"  font-size: {btn_fs};",
            f"  font-weight: {btn_fw};",
            "  cursor: pointer;",
            "  transition: background-color 0.15s ease, border-color 0.15s ease, transform 0.15s ease;",
            "}",
            ".btn-secondary:hover {",
            "  background-color: var(--surface-subtle, #f3f4f6);",
            "  transform: translateY(-1px);",
            "}",
            *ghost_block,
            "",
            "/* Card Components */",
            ".card-surface {",
            f"  background-color: {card_bg};",
            f"  border: {card_border};",
            f"  border-radius: {card_radius};",
            f"  padding: {card_padding};",
            f"  box-shadow: {card_shadow};",
            "}",
            "",
            "/* Navigation Components */",
            ".nav-container {",
            "  display: flex;",
            "  align-items: center;",
            "  justify-content: space-between;",
            "  padding: 16px var(--spacing-24, 24px);",
            f"  background-color: {nav_bg};",
            f"  border-bottom: {nav_border};",
            f"  max-width: {nav_max_width};",
            "  margin: 0 auto;",
            "}",
            "",
            "/* Form Input Components */",
            ".input-field {",
            "  width: 100%;",
            "  padding: 12px 14px;",
            f"  background-color: {input_bg};",
            f"  border: {input_border};",
            f"  border-radius: {input_radius};",
            f"  color: {input_text};",
            "  font-family: var(--font-primary, sans-serif);",
            "  font-size: 14px;",
            "  outline: none;",
            "  transition: border-color 0.15s ease, box-shadow 0.15s ease;",
            "}",
            ".input-field:focus {",
            f"  border-color: {brand_color};",
            "  box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.05);",
            "}",
            "",
            "/* Badge Components */",
            ".badge {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  padding: 4px 10px;",
            f"  background-color: {badge_bg};",
            f"  color: {badge_text};",
            f"  border-radius: {badge_radius};",
            "  font-size: 12px;",
            "  font-weight: 600;",
            "}",
        ]

    return "\n".join(css_blocks) + "\n"


_BUTTON_VARIANT_CLASS: Dict[str, str] = {"primary": "btn-primary", "secondary": "btn-secondary", "ghost": "btn-ghost"}


def _render_component_catalog(components: List[Any]) -> List[str]:
    """検出済みコンポーネント/バリアントを、名前・用途をHTMLコメントで注記した
    「パターンライブラリ」形式のカタログとして描画する（同ページ編集・類似ページ生成の両方で
    再利用できるよう、各バリアントの意味論的な役割を明示する）。
    """
    buttons = [c for c in components if getattr(c, "component_type", None) == "button"]
    badge = next((c for c in components if getattr(c, "component_type", None) == "badge"), None)
    input_comp = next((c for c in components if getattr(c, "component_type", None) == "input"), None)

    lines = [
        '    <section class="card-surface">',
        '      <h2>Component Showcase</h2>',
        '      <p style="margin: 16px 0;">This skeleton template uses tokens.css and components.css to reproduce the UI layout deterministically.</p>',
        '      <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-top: 16px;">',
    ]
    if badge is not None:
        lines.append(f'        <!-- Variant: {badge.name} ({badge.semantic_role or "badge"}) -->')
        lines.append('        <span class="badge">Active</span>')
    if input_comp is not None:
        lines.append(f'        <!-- Variant: {input_comp.name} ({input_comp.semantic_role or "input"}) -->')
        lines.append('        <input type="text" class="input-field" placeholder="Search components..." style="max-width: 300px;">')
    for comp in buttons:
        css_class = _BUTTON_VARIANT_CLASS.get(getattr(comp, "variant_key", "default"), "btn-primary")
        label = comp.name.split("(")[0].strip() if comp.name else css_class
        lines.append(f'        <!-- Variant: {comp.name} ({comp.semantic_role or comp.variant_key}) -->')
        lines.append(f'        <button class="{css_class}">{label}</button>')
    lines.append("      </div>")
    lines.append("    </section>")
    return lines


def render_skeleton_html(
    metadata: Dict[str, Any],
    design_system: Optional[DesignSystemAnalysis] = None,
) -> str:
    """抽出されたフォント情報とリセットCSSを含む完全なHTMLテンプレート骨格を生成する。"""
    title = "UI Skeleton"
    if metadata and isinstance(metadata, dict):
        title = metadata.get("title") or metadata.get("ogTitle") or title
    if title == "UI Skeleton" and design_system is not None and getattr(design_system, "brand_name", None):
        title = f"{design_system.brand_name} — UI Reproduction Skeleton"

    google_fonts_url = "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap"
    if metadata and isinstance(metadata, dict) and metadata.get("google_fonts"):
        google_fonts_url = metadata["google_fonts"]

    theme = getattr(design_system, "theme", "light") if design_system else "light"
    is_dark = (theme == "dark")

    body_bg = "var(--surface-void, #08090a)" if is_dark else "var(--surface-canvas, #ffffff)"
    body_color = "var(--color-text-body, #d0d6e0)" if is_dark else "var(--color-body, #1f1f1f)"

    primary_font = "var(--font-primary, sans-serif)"
    if design_system is not None and getattr(design_system, "font_families", None):
        prim_spec = next((f for f in design_system.font_families if f.role == "Primary"), None)
        if prim_spec:
            primary_font = f"var(--font-primary, {prim_spec.substitute})"

    brand_label = title.split("—")[0].strip() if "—" in title else title
    if design_system is not None and getattr(design_system, "brand_name", None):
        brand_label = design_system.brand_name

    components: List[Any] = list(getattr(design_system, "components", None) or []) if design_system is not None else []
    showcase_lines = _render_component_catalog(components) if components else [
        '    <section class="card-surface">',
        '      <h2>Component Showcase</h2>',
        '      <p style="margin: 16px 0;">This skeleton template uses tokens.css and components.css to reproduce the UI layout deterministically.</p>',
        '      <div style="display: flex; gap: 12px; align-items: center; margin-top: 16px;">',
        '        <span class="badge">Active</span>',
        '        <input type="text" class="input-field" placeholder="Search components..." style="max-width: 300px;">',
        '        <button class="btn-primary">Action</button>',
        "      </div>",
        "    </section>",
    ]

    html_lines = [
        "<!DOCTYPE html>",
        '<html lang="ja">',
        "<head>",
        '  <meta charset="UTF-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        f"  <title>{title}</title>",
        '  <link rel="preconnect" href="https://fonts.googleapis.com">',
        '  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        f'  <link href="{google_fonts_url}" rel="stylesheet">',
        '  <link rel="stylesheet" href="tokens.css">',
        '  <link rel="stylesheet" href="components.css">',
        "  <style>",
        "    /* CSS Reset */",
        "    *, *::before, *::after {",
        "      box-sizing: border-box;",
        "      margin: 0;",
        "      padding: 0;",
        "    }",
        "    body {",
        "      min-height: 100vh;",
        "      line-height: 1.5;",
        "      -webkit-font-smoothing: antialiased;",
        f"      font-family: {primary_font};",
        f"      background-color: {body_bg};",
        f"      color: {body_color};",
        "    }",
        "  </style>",
        "</head>",
        "<body>",
        '  <header class="nav-container">',
        '    <div class="brand-logo">',
        f'      <span>{brand_label}</span>',
        "    </div>",
        "    <nav>",
        '      <button class="btn-secondary">Overview</button>',
        '      <button class="btn-primary">Get Started</button>',
        "    </nav>",
        "  </header>",
        '  <main style="max-width: var(--page-max-width, 1200px); margin: 0 auto; padding: 32px 24px;">',
        *showcase_lines,
        "  </main>",
        "</body>",
        "</html>",
    ]

    return "\n".join(html_lines) + "\n"

