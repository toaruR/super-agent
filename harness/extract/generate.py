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
