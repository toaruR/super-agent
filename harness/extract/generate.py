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


def render_components_css(
    design_system: Optional[DesignSystemAnalysis] = None,
) -> str:
    """主要コンポーネント（.btn-primary, .btn-secondary, .card-surface, .nav-container, .input-field, .badge 等）の具現化CSSルールを生成する。"""
    theme = getattr(design_system, "theme", "light") if design_system else "light"
    is_dark = (theme == "dark")

    brand_hex = "#ff5c35" if not is_dark else "#e4f222"
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

    if is_dark:
        css_blocks = [
            "/* Main Components CSS (Dark Theme) */",
            "",
            "/* Button Components */",
            ".btn-primary {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  justify-content: center;",
            "  padding: 10px 16px;",
            f"  background-color: {brand_color};",
            f"  color: {on_primary_color};",
            "  border-radius: var(--radius-md, 6px);",
            "  font-family: var(--font-primary, 'Inter Variable', sans-serif);",
            "  font-size: var(--text-body-sm, 14px);",
            "  font-weight: 510;",
            "  border: none;",
            "  cursor: pointer;",
            "  transition: background-color 0.15s ease, transform 0.15s ease;",
            "}",
            ".btn-primary:hover {",
            "  opacity: 0.9;",
            "}",
            "",
            ".btn-secondary {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  justify-content: center;",
            "  padding: 8px 14px;",
            "  background-color: transparent;",
            "  color: var(--color-text-body, #d0d6e0);",
            "  border: 1px solid var(--color-border, #23252a);",
            "  border-radius: var(--radius-md, 6px);",
            "  font-family: var(--font-primary, 'Inter Variable', sans-serif);",
            "  font-size: var(--text-body-sm, 14px);",
            "  font-weight: 400;",
            "  cursor: pointer;",
            "  transition: background-color 0.15s ease, border-color 0.15s ease;",
            "}",
            ".btn-secondary:hover {",
            "  background-color: rgba(255, 255, 255, 0.05);",
            "  border-color: var(--color-text-muted, #8a8f98);",
            "}",
            "",
            "/* Card Components */",
            ".card-surface {",
            "  background-color: var(--surface-carbon, #0f1011);",
            "  border: 1px solid var(--color-border, #23252a);",
            "  border-radius: var(--radius-xl, 12px);",
            "  padding: var(--card-padding, 24px);",
            "  box-shadow: var(--shadow-sm, 0 2px 4px rgba(0, 0, 0, 0.4));",
            "}",
            "",
            "/* Navigation Components */",
            ".nav-container {",
            "  display: flex;",
            "  align-items: center;",
            "  justify-content: space-between;",
            "  padding: 16px var(--spacing-24, 24px);",
            "  background-color: var(--surface-void, #08090a);",
            "  border-bottom: 1px solid var(--color-border, #23252a);",
            "  max-width: var(--page-max-width, 1200px);",
            "  margin: 0 auto;",
            "}",
            "",
            "/* Form Input Components */",
            ".input-field {",
            "  width: 100%;",
            "  padding: 10px 14px;",
            "  background-color: rgba(255, 255, 255, 0.02);",
            "  border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));",
            "  border-radius: var(--radius-inputs, 6px);",
            "  color: var(--color-text-body, #d0d6e0);",
            "  font-family: var(--font-primary, 'Inter Variable', sans-serif);",
            "  font-size: 14px;",
            "  outline: none;",
            "  transition: border-color 0.15s ease;",
            "}",
            ".input-field:focus {",
            f"  border-color: {brand_color};",
            "}",
            "",
            "/* Badge Components */",
            ".badge {",
            "  display: inline-flex;",
            "  align-items: center;",
            "  padding: 2px 8px;",
            "  background-color: rgba(255, 255, 255, 0.05);",
            "  color: var(--color-text-muted, #8a8f98);",
            "  border-radius: var(--radius-badges, 4px);",
            "  font-size: 12px;",
            "  font-weight: 400;",
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
            "  padding: 10px 18px;",
            f"  background-color: {brand_color};",
            f"  color: {on_primary_color};",
            "  border-radius: var(--radius-md, 6px);",
            "  font-family: var(--font-primary, 'Inter Variable', sans-serif);",
            "  font-size: var(--text-body-sm, 14px);",
            "  font-weight: 510;",
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
            "  padding: 9px 16px;",
            "  background-color: var(--surface-canvas, #ffffff);",
            "  color: var(--color-body, #1f1f1f);",
            "  border: 1px solid var(--color-border-strong, #d1d5db);",
            "  border-radius: var(--radius-md, 6px);",
            "  font-family: var(--font-primary, 'Inter Variable', sans-serif);",
            "  font-size: var(--text-body-sm, 14px);",
            "  font-weight: 500;",
            "  cursor: pointer;",
            "  transition: background-color 0.15s ease, border-color 0.15s ease, transform 0.15s ease;",
            "}",
            ".btn-secondary:hover {",
            "  background-color: var(--surface-subtle, #f3f4f6);",
            "  border-color: var(--color-secondary, #9ca3af);",
            "  transform: translateY(-1px);",
            "}",
            "",
            "/* Card Components */",
            ".card-surface {",
            "  background-color: var(--surface-surface, #ffffff);",
            "  border: 1px solid var(--color-border, #f0f0f0);",
            "  border-radius: var(--radius-xl, 12px);",
            "  padding: var(--card-padding, 24px);",
            "  box-shadow: var(--shadow-sm, 0 2px 4px rgba(0, 0, 0, 0.04));",
            "}",
            "",
            "/* Navigation Components */",
            ".nav-container {",
            "  display: flex;",
            "  align-items: center;",
            "  justify-content: space-between;",
            "  padding: 16px var(--spacing-24, 24px);",
            "  background-color: var(--surface-canvas, #ffffff);",
            "  border-bottom: 1px solid var(--color-border, #f0f0f0);",
            "  max-width: var(--page-max-width, 1200px);",
            "  margin: 0 auto;",
            "}",
            "",
            "/* Form Input Components */",
            ".input-field {",
            "  width: 100%;",
            "  padding: 10px 14px;",
            "  background-color: var(--surface-canvas, #ffffff);",
            "  border: 1px solid var(--color-border-strong, #d1d5db);",
            "  border-radius: var(--radius-inputs, 6px);",
            "  color: var(--color-body, #1f1f1f);",
            "  font-family: var(--font-primary, 'Inter Variable', sans-serif);",
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
            "  padding: 2px 8px;",
            "  background-color: var(--surface-subtle, #f3f4f6);",
            "  color: var(--color-body, #1f1f1f);",
            "  border-radius: var(--radius-badges, 4px);",
            "  font-size: 12px;",
            "  font-weight: 500;",
            "}",
        ]

    return "\n".join(css_blocks) + "\n"


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

    html_lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
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
        "      font-family: var(--font-primary, 'Inter', sans-serif);",
        f"      background-color: {body_bg};",
        f"      color: {body_color};",
        "    }",
        "  </style>",
        "</head>",
        "<body>",
        '  <header class="nav-container">',
        '    <div class="brand-logo">',
        f'      <span>{title.split("—")[0].strip() if "—" in title else title}</span>',
        "    </div>",
        "    <nav>",
        '      <button class="btn-secondary">Overview</button>',
        '      <button class="btn-primary">Get Started</button>',
        "    </nav>",
        "  </header>",
        '  <main style="max-width: var(--page-max-width, 1200px); margin: 0 auto; padding: 32px 24px;">',
        '    <section class="card-surface">',
        '      <h2>Component Showcase</h2>',
        '      <p style="margin: 16px 0;">This skeleton template uses tokens.css and components.css to reproduce the UI layout deterministically.</p>',
        '      <div style="display: flex; gap: 12px; align-items: center; margin-top: 16px;">',
        '        <span class="badge">Active</span>',
        '        <input type="text" class="input-field" placeholder="Search components..." style="max-width: 300px;">',
        '        <button class="btn-primary">Action</button>',
        "      </div>",
        "    </section>",
        "  </main>",
        "</body>",
        "</html>",
    ]

    return "\n".join(html_lines) + "\n"

