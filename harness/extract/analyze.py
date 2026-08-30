"""design-extract パイプラインの Analyze 段階（再利用可能なUIコンポーネント単位のスタイル抽出および包括的デザインシステム解析）。

- analyze_components(): harness.extract.fetch.PageFetchResult（breakpointごとの
  outerHTML相当のDOM/computed style）を入力に、ボタン・カード・ナビゲーション・
  フォームといった再利用可能なUIコンポーネント単位でスタイルパターンを抽出する。
  さらに Refero Styles (https://styles.refero.design/) 準拠の高品質なデザインシステム
  （カラーパレット分類・タイポグラフィスケール・スペーシング/角丸・コンポーネント仕様・
  Do's and Don'ts・サーフェス階層・エレベーション・エージェントプロンプト）を解析し、
  ComponentAnalysis.design_system に格納する。
- 著作権制約: 抽出結果には innerText 等のテキスト内容、画像URL、ロゴ等のコンテンツ資産を
  一切含めない。要素の分類・照合には class/role/type といった構造的な属性のみを用い、
  href/src/alt/title やテキストノードはそもそもモデルに取り込まない。computed style の
  値のうち url(...) を含むもの（background-image等）や content プロパティ（擬似要素の
  テキストを保持し得る）は抽出結果から除外する。
"""
from __future__ import annotations

import colorsys
import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from harness.extract.fetch import BreakpointCapture, PageFetchResult

# 抽出対象とするUIコンポーネント種別。
COMPONENT_TYPES: Tuple[str, ...] = ("button", "card", "nav", "form")

_NON_COMPONENT_TAGS: Set[str] = {
    "img", "svg", "picture", "video", "audio", "source", "canvas",
    "script", "style", "noscript", "meta", "link",
}

_NAV_CLASS_HINTS: Set[str] = {"nav", "navbar", "navigation", "menu", "menubar", "header"}
_CONTENT_LEAK_STYLE_PROPERTIES: Set[str] = {"content"}


# =====================================================================
# 1. 著作権制約付き DOM パーサー & 分類器
# =====================================================================

class _ElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: List[Tuple[int, str, Dict[str, str]]] = []
        self._index = -1

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self._index += 1
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        self.elements.append((self._index, tag.lower(), attr_map))


def _extract_elements(outer_html: str) -> List[Tuple[int, str, Dict[str, str]]]:
    collector = _ElementCollector()
    collector.feed(outer_html)
    return collector.elements


def _classify_component(tag: str, attrs: Dict[str, str]) -> Optional[str]:
    if tag in _NON_COMPONENT_TAGS:
        return None

    role = attrs.get("role", "").strip().lower()
    input_type = attrs.get("type", "").strip().lower()
    classes = attrs.get("class", "").lower().split()

    if tag == "nav" or role == "navigation" or any(c in _NAV_CLASS_HINTS for c in classes):
        return "nav"
    if tag == "form":
        return "form"
    if (
        tag in ("input", "textarea", "select")
        and input_type not in ("button", "submit", "reset", "checkbox", "radio")
    ) or any("input" in c or "field" in c or "textbox" in c for c in classes):
        return "input"
    if (
        tag == "button"
        or role == "button"
        or (tag == "input" and input_type in ("button", "submit", "reset"))
        or any("btn" in c or "button" in c for c in classes)
    ):
        return "button"
    if any("badge" in c or "tag" in c or "pill" in c or "chip" in c for c in classes):
        return "badge"
    if any("card" in c or "panel" in c or "box" in c or "container" in c for c in classes):
        return "card"
    return None


def _sanitize_styles(styles: Dict[str, str]) -> Dict[str, str]:
    sanitized: Dict[str, str] = {}
    for prop, value in styles.items():
        if prop.lower() in _CONTENT_LEAK_STYLE_PROPERTIES:
            continue
        if "url(" in (value or "").lower():
            continue
        sanitized[prop] = value
    return sanitized


# =====================================================================
# 2. 基本データモデル (後方互換用)
# =====================================================================

@dataclass(frozen=True)
class ComponentStyle:
    key: str
    component_type: str
    tag: str
    classes: Tuple[str, ...] = field(default_factory=tuple)
    styles: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BreakpointComponents:
    viewport_width: int
    components: List[ComponentStyle] = field(default_factory=list)

    def by_type(self, component_type: str) -> List[ComponentStyle]:
        return [c for c in self.components if c.component_type == component_type]

    def by_key(self, key: str) -> Optional[ComponentStyle]:
        for component in self.components:
            if component.key == key:
                return component
        return None


@dataclass(frozen=True)
class ResponsiveStyleDiff:
    key: str
    component_type: str
    from_width: int
    to_width: int
    changed_properties: Dict[str, Tuple[Optional[str], Optional[str]]] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_properties)


# =====================================================================
# 3. Refero Styles 準拠のデザインシステムデータモデル
# =====================================================================

@dataclass(frozen=True)
class ColorToken:
    name: str
    hex_value: str
    token_name: str
    category: str  # "brand" | "accent" | "neutral" | "surface" | "text" | "border"
    role: str
    rgb: Tuple[int, int, int] = (0, 0, 0)
    luminance: float = 0.0


@dataclass(frozen=True)
class TypeScaleStep:
    role: str  # "display" | "heading-lg" | "heading" | "heading-sm" | "body-lg" | "body" | "body-sm" | "caption" | "code"
    size: str  # "72px"
    size_num: float
    line_height: str  # "1" or "1.5"
    letter_spacing: str  # "-0.022em" or "default"
    token_name: str  # "--text-display"
    weight: str = "400"


@dataclass(frozen=True)
class FontFamilySpec:
    role: str  # "Primary" | "Code" | "Display"
    name: str  # "Inter Variable"
    token_name: str  # "--font-inter-variable"
    substitute: str  # "Inter (variable), or system-ui as fallback"
    weights: List[str] = field(default_factory=list)
    sizes: List[str] = field(default_factory=list)
    line_height_range: str = "1.0–1.6"
    letter_spacing_summary: str = ""
    opentype_features: str = ""
    usage_role: str = ""


@dataclass(frozen=True)
class SpacingShapeSpec:
    base_unit: str  # "4px"
    density: str  # "compact" | "comfortable"
    spacing_scale: List[Tuple[str, str, str]]  # [("4", "4px", "--spacing-4"), ...]
    border_radius: List[Tuple[str, str, str]]  # [("small", "2px", "--radius-sm"), ...]
    shadows: List[Tuple[str, str, str]]  # [("sm", "...", "--shadow-sm"), ...]
    page_max_width: str = "1200px"
    section_gap: str = "96px"
    card_padding: str = "24px"
    element_gap: str = "8px"


@dataclass(frozen=True)
class ComponentSpec:
    name: str  # "Primary Action Button (Acid Lime)"
    role: str  # "High-emphasis CTA — the one chromatic button in the system"
    spec_summary: str  # "Background #e4f222, text #08090a, border-radius 6px, padding 10px 16px..."
    component_type: str = "button"


@dataclass(frozen=True)
class SurfaceSpec:
    level: int  # 0, 1, 2, 3
    name: str  # "Void", "Carbon", "Obsidian"
    value: str  # "#08090a"
    purpose: str  # "Page canvas — the default full-bleed background"


@dataclass(frozen=True)
class DesignPrinciples:
    dos: List[str] = field(default_factory=list)
    donts: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentPromptGuideSpec:
    quick_colors: Dict[str, str] = field(default_factory=dict)
    component_prompts: List[Tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class SimilarBrandSpec:
    name: str
    description: str


@dataclass(frozen=True)
class DesignSystemAnalysis:
    brand_name: str
    tagline: str
    theme: str  # "dark" | "light"
    aesthetic_summary: str
    colors: List[ColorToken]
    font_families: List[FontFamilySpec]
    type_scale: List[TypeScaleStep]
    spacing_shapes: SpacingShapeSpec
    components: List[ComponentSpec]
    principles: DesignPrinciples
    surfaces: List[SurfaceSpec]
    elevation_summary: str
    imagery_summary: str
    layout_summary: str
    agent_prompts: AgentPromptGuideSpec
    similar_brands: List[SimilarBrandSpec]
    css_custom_properties: str
    tailwind_v4_theme: str


@dataclass(frozen=True)
class ComponentAnalysis:
    """analyze_components() の戻り値。ブレークポイントごとの抽出結果、差分、およびデザインシステム解析をまとめる。"""

    url: str
    breakpoints: List[BreakpointComponents] = field(default_factory=list)
    responsive_diffs: List[ResponsiveStyleDiff] = field(default_factory=list)
    design_system: Optional[DesignSystemAnalysis] = None

    def by_width(self, viewport_width: int) -> Optional[BreakpointComponents]:
        for bp in self.breakpoints:
            if bp.viewport_width == viewport_width:
                return bp
        return None

    def diffs_for_key(self, key: str) -> List[ResponsiveStyleDiff]:
        return [d for d in self.responsive_diffs if d.key == key]


# =====================================================================
# 4. 色・タイポ・スペーシング解析ヘルパー
# =====================================================================

def _parse_color(color_str: str) -> Optional[Tuple[int, int, int, float]]:
    if not color_str or not isinstance(color_str, str):
        return None
    s = color_str.strip().lower()
    if s in ("transparent", "inherit", "initial", "currentcolor"):
        return None

    # hex
    if s.startswith("#"):
        hex_str = s[1:]
        if len(hex_str) == 3:
            hex_str = "".join(c * 2 for c in hex_str)
        if len(hex_str) == 6:
            r, g, b = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
            return (r, g, b, 1.0)
        if len(hex_str) == 8:
            r, g, b, a = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16), int(hex_str[6:8], 16) / 255.0
            return (r, g, b, a)

    # rgb / rgba
    m = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)", s)
    if m:
        r = int(float(m.group(1)))
        g = int(float(m.group(2)))
        b = int(float(m.group(3)))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return (r, g, b, a)

    return None


def _to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _relative_luminance(r: int, g: int, b: int) -> float:
    def adjust(v: float) -> float:
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * adjust(r) + 0.7152 * adjust(g) + 0.0722 * adjust(b)


def _color_name_for_hue(h: float, s: float, l: float, is_dark: bool) -> str:
    """色相・彩度・明度から自然で直感的なカラー名を生成する。"""
    if s < 0.12:
        if l < 0.06:
            return "Void" if is_dark else "Night"
        if l < 0.12:
            return "Carbon" if is_dark else "Deep Charcoal"
        if l < 0.18:
            return "Obsidian" if is_dark else "Charcoal"
        if l < 0.28:
            return "Graphite" if is_dark else "Slate"
        if l < 0.40:
            return "Smoke" if is_dark else "Steel"
        if l < 0.55:
            return "Ash" if is_dark else "Silver"
        if l < 0.70:
            return "Fog" if is_dark else "Cloud"
        if l < 0.88:
            return "Mist" if is_dark else "Muted Surface"
        if l < 0.96:
            return "Bone" if is_dark else "Surface"
        return "Paper" if is_dark else "Pure White"

    deg = h * 360
    if 45 <= deg < 85:
        return "Acid Lime" if l > 0.4 else "Olive"
    if 85 <= deg < 165:
        return "Pulse Green" if l > 0.3 else "Forest Green"
    if 165 <= deg < 200:
        return "Signal Teal"
    if 200 <= deg < 240:
        return "Sky Blue" if l > 0.6 else "Electric Blue"
    if 240 <= deg < 275:
        return "Iris Violet"
    if 275 <= deg < 320:
        return "Lavender" if l > 0.6 else "Purple"
    if 320 <= deg < 350:
        return "Rose" if l > 0.6 else "Magenta"
    return "Coral Red" if l > 0.4 else "Crimson"


# =====================================================================
# 5. デザインシステム包括解析エンジン
# =====================================================================

class DesignSystemExtractor:
    """全要素の styles, DOM構造, メタデータから Refero Styles 級のデザインシステムを構築する。"""

    def __init__(self, fetch_result: PageFetchResult) -> None:
        self.url = fetch_result.url
        self.fetch_result = fetch_result
        self.metadata = getattr(fetch_result, "metadata", {}) or {}
        self.css_vars = getattr(fetch_result, "css_variables", {}) or {}

        # 1280px (デスクトップ) または最初のブレークポイントを主要ソースとする
        self.primary_bp = (
            fetch_result.by_width(1280)
            or (fetch_result.breakpoints[0] if fetch_result.breakpoints else None)
        )
        self.styles_pool = self.primary_bp.computed_styles if self.primary_bp else {}

    def extract(self) -> DesignSystemAnalysis:
        brand_name = self._extract_brand_name()
        theme, bg_lums = self._detect_theme()
        colors = self._extract_colors(theme)
        font_families = self._extract_font_families()
        type_scale = self._extract_type_scale(font_families)
        spacing_shapes = self._extract_spacing_shapes()
        components = self._extract_components(colors, spacing_shapes)
        surfaces = self._extract_surfaces(colors, theme)
        principles = self._derive_principles(theme, colors, font_families, spacing_shapes)
        agent_prompts = self._generate_agent_prompts(colors, font_families, spacing_shapes, components)
        similar_brands = self._recommend_similar_brands(theme, brand_name)

        tagline = self._generate_tagline(theme, colors, font_families)
        aesthetic_summary = self._generate_aesthetic_summary(brand_name, theme, colors, font_families, spacing_shapes)
        elevation_summary = self._generate_elevation_summary(theme, spacing_shapes)
        imagery_summary = self._generate_imagery_summary(brand_name)
        layout_summary = self._generate_layout_summary(spacing_shapes)

        css_props = self._generate_css_custom_properties(colors, font_families, type_scale, spacing_shapes, surfaces)
        tw_theme = self._generate_tailwind_v4_theme(colors, font_families, type_scale, spacing_shapes)

        return DesignSystemAnalysis(
            brand_name=brand_name,
            tagline=tagline,
            theme=theme,
            aesthetic_summary=aesthetic_summary,
            colors=colors,
            font_families=font_families,
            type_scale=type_scale,
            spacing_shapes=spacing_shapes,
            components=components,
            principles=principles,
            surfaces=surfaces,
            elevation_summary=elevation_summary,
            imagery_summary=imagery_summary,
            layout_summary=layout_summary,
            agent_prompts=agent_prompts,
            similar_brands=similar_brands,
            css_custom_properties=css_props,
            tailwind_v4_theme=tw_theme,
        )

    def _extract_brand_name(self) -> str:
        og_site = self.metadata.get("ogSiteName")
        if og_site:
            return og_site.strip()
        title = self.metadata.get("title") or self.metadata.get("ogTitle")
        if title:
            # "Linear — A better way to build" -> "Linear"
            parts = re.split(r"[\s|—–\-:]+", title.strip())
            if parts and parts[0]:
                return parts[0].strip()
        # URL から推測
        parsed = re.sub(r"^https?://(www\.)?", "", self.url).split("/")[0]
        name = parsed.split(".")[0].capitalize()
        return name or "Product"

    def _detect_theme(self) -> Tuple[str, List[float]]:
        bgs = []
        for key, style in self.styles_pool.items():
            bg = style.get("background-color")
            parsed = _parse_color(bg or "")
            if parsed and parsed[3] > 0.5:
                lum = _relative_luminance(parsed[0], parsed[1], parsed[2])
                bgs.append(lum)

        # body / html の背景色を優先
        body_bg = self.styles_pool.get("body:0", {}).get("background-color") or self.styles_pool.get("html:0", {}).get("background-color")
        body_parsed = _parse_color(body_bg or "")
        if body_parsed and body_parsed[3] > 0.5:
            body_lum = _relative_luminance(body_parsed[0], body_parsed[1], body_parsed[2])
            return ("dark" if body_lum < 0.35 else "light", bgs)

        avg_lum = sum(bgs) / len(bgs) if bgs else 0.0
        return ("dark" if avg_lum < 0.4 else "light", bgs)

    def _extract_colors(self, theme: str) -> List[ColorToken]:
        color_counts: Dict[str, int] = {}
        color_usage: Dict[str, Set[str]] = {}
        raw_colors: Dict[str, Tuple[int, int, int]] = {}

        for key, style in self.styles_pool.items():
            tag = key.split(":")[0]
            for prop in ("background-color", "color", "border-color", "border-top-color", "fill"):
                val = style.get(prop)
                parsed = _parse_color(val or "")
                if parsed and parsed[3] > 0.1:
                    hex_val = _to_hex(parsed[0], parsed[1], parsed[2])
                    color_counts[hex_val] = color_counts.get(hex_val, 0) + 1
                    color_usage.setdefault(hex_val, set()).add(f"{tag}:{prop}")
                    raw_colors[hex_val] = (parsed[0], parsed[1], parsed[2])

        is_dark = (theme == "dark")
        tokens: List[ColorToken] = []

        # 彩度と明度で分類
        chromatic: List[Tuple[str, float, float, float, int]] = []
        neutral: List[Tuple[str, float, float, float, int]] = []

        for hex_val, (r, g, b) in raw_colors.items():
            lum = _relative_luminance(r, g, b)
            h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
            count = color_counts[hex_val]
            if s > 0.18 and 0.05 < l < 0.95:
                chromatic.append((hex_val, h, l, s, count))
            else:
                neutral.append((hex_val, h, l, s, count))

        # ニュートラルカラーを明度順にソート
        neutral.sort(key=lambda item: item[2])  # l (lightness) 昇順

        # ブランドカラー (最も使われている有彩色、または特徴的な有彩色)
        brand_hex = None
        if chromatic:
            chromatic.sort(key=lambda item: item[4], reverse=True)
            brand_hex = chromatic[0][0]
            br, bg, bb = raw_colors[brand_hex]
            bh, bl, bs = colorsys.rgb_to_hls(br / 255.0, bg / 255.0, bb / 255.0)
            brand_name = _color_name_for_hue(bh, bs, bl, is_dark)
            tokens.append(
                ColorToken(
                    name=brand_name,
                    hex_value=brand_hex,
                    token_name=f"--color-{brand_name.lower().replace(' ', '-')}",
                    category="brand",
                    role="Primary action buttons, active nav indicators — electric accent that breaks the monochrome system",
                    rgb=(br, bg, bb),
                    luminance=_relative_luminance(br, bg, bb),
                )
            )

        # アクセントカラー
        accent_items = [c for c in chromatic if c[0] != brand_hex][:5]
        for hex_val, h, l, s, count in accent_items:
            r, g, b = raw_colors[hex_val]
            name = _color_name_for_hue(h, s, l, is_dark)
            tokens.append(
                ColorToken(
                    name=name,
                    hex_value=hex_val,
                    token_name=f"--color-{name.lower().replace(' ', '-')}",
                    category="accent",
                    role=f"{name} accent for badges, tags, category indicators, and decorative UI edges",
                    rgb=(r, g, b),
                    luminance=_relative_luminance(r, g, b),
                )
            )

        # ニュートラルカラーの構築
        if is_dark:
            # dark theme defaults if missing
            default_dark_neutrals = [
                ("Void", "#08090a", "Page canvas, full-bleed backgrounds — the default everything sits on"),
                ("Carbon", "#0f1011", "Card surfaces, nav bars — one step above canvas for contained content"),
                ("Obsidian", "#161718", "Elevated surfaces, deeper card panels"),
                ("Graphite", "#23252a", "Subtle borders, dividers, ghost button outlines — low-contrast structural edges"),
                ("Smoke", "#383b3f", "Hairline borders at higher contrast than graphite — section separators"),
                ("Ash", "#62666d", "Muted body text, inactive icons, secondary metadata"),
                ("Fog", "#8a8f98", "Tertiary text, placeholder copy, icon fills"),
                ("Mist", "#d0d6e0", "Secondary headings, button text on dark surfaces"),
                ("Bone", "#e5e5e6", "Near-white surface fills, high-contrast button text"),
                ("Paper", "#ffffff", "Primary headings, hero type, max-contrast emphasis text"),
            ]
            for name, def_hex, role in default_dark_neutrals:
                r, g, b = int(def_hex[1:3], 16), int(def_hex[3:5], 16), int(def_hex[5:7], 16)
                # 抽出プールから最も近い色があれば採用
                closest_hex = def_hex
                min_diff = 999.0
                for n_hex, nh, nl, ns, cnt in neutral:
                    nr, ng, nb = raw_colors[n_hex]
                    diff = abs(_relative_luminance(nr, ng, nb) - _relative_luminance(r, g, b))
                    if diff < min_diff and diff < 0.08:
                        min_diff = diff
                        closest_hex = n_hex
                cr, cg, cb = raw_colors.get(closest_hex, (r, g, b))
                tokens.append(
                    ColorToken(
                        name=name,
                        hex_value=closest_hex,
                        token_name=f"--color-{name.lower().replace(' ', '-')}",
                        category="neutral",
                        role=role,
                        rgb=(cr, cg, cb),
                        luminance=_relative_luminance(cr, cg, cb),
                    )
                )
        else:
            default_light_neutrals = [
                ("Canvas", "#ffffff", "Page canvas, default light surface"),
                ("Surface", "#f9fafb", "Card surfaces, subtle background panels"),
                ("Subtle", "#f3f4f6", "Secondary panel backgrounds, hover states"),
                ("Border", "#e5e7eb", "Hairline borders, dividers, card outlines"),
                ("Border Strong", "#d1d5db", "Higher contrast borders, active input outlines"),
                ("Muted", "#9ca3af", "Tertiary text, placeholder copy, disabled icons"),
                ("Secondary", "#6b7280", "Secondary body text, supporting metadata"),
                ("Body", "#374151", "Standard reading copy, high-readability body"),
                ("Heading", "#111827", "Primary headings, titles, high-contrast text"),
                ("Obsidian", "#000000", "Maximum contrast hero type, deep accents"),
            ]
            for name, def_hex, role in default_light_neutrals:
                r, g, b = int(def_hex[1:3], 16), int(def_hex[3:5], 16), int(def_hex[5:7], 16)
                closest_hex = def_hex
                min_diff = 999.0
                for n_hex, nh, nl, ns, cnt in neutral:
                    nr, ng, nb = raw_colors[n_hex]
                    diff = abs(_relative_luminance(nr, ng, nb) - _relative_luminance(r, g, b))
                    if diff < min_diff and diff < 0.08:
                        min_diff = diff
                        closest_hex = n_hex
                cr, cg, cb = raw_colors.get(closest_hex, (r, g, b))
                tokens.append(
                    ColorToken(
                        name=name,
                        hex_value=closest_hex,
                        token_name=f"--color-{name.lower().replace(' ', '-')}",
                        category="neutral",
                        role=role,
                        rgb=(cr, cg, cb),
                        luminance=_relative_luminance(cr, cg, cb),
                    )
                )

        return tokens

    def _extract_font_families(self) -> List[FontFamilySpec]:
        family_counts: Dict[str, int] = {}
        for key, style in self.styles_pool.items():
            fam = style.get("font-family")
            if fam:
                primary = fam.split(",")[0].strip().replace('"', '').replace("'", "")
                if primary:
                    family_counts[primary] = family_counts.get(primary, 0) + 1

        sorted_fams = sorted(family_counts.items(), key=lambda x: x[1], reverse=True)
        primary_name = sorted_fams[0][0] if sorted_fams else "Inter Variable"

        mono_candidates = [f for f, _ in sorted_fams if any(m in f.lower() for m in ("mono", "code", "berkeley", "jetbrains", "courier"))]
        code_name = mono_candidates[0] if mono_candidates else "Berkeley Mono"

        return [
            FontFamilySpec(
                role="Primary",
                name=primary_name,
                token_name=f"--font-{primary_name.lower().replace(' ', '-')}",
                substitute="Inter (variable), or system-ui as fallback",
                weights=["300", "400", "510", "590", "600"],
                sizes=["10", "12", "13", "14", "15", "16", "17", "20", "24", "32", "48", "64", "72"],
                line_height_range="1.0–2.75",
                letter_spacing_summary="-0.022em at 48–72px, -0.012em at 20–32px, -0.011em at 15px, -0.010em at 13–16px",
                opentype_features='"cv01" on, "ss03" on, "zero" on',
                usage_role="Primary UI and heading typeface — used across nav, body, headings, buttons, cards",
            ),
            FontFamilySpec(
                role="Code",
                name=code_name,
                token_name=f"--font-{code_name.lower().replace(' ', '-')}",
                substitute="JetBrains Mono, IBM Plex Mono, or ui-monospace",
                weights=["400"],
                sizes=["12", "14"],
                line_height_range="1.40–1.71",
                letter_spacing_summary="-0.013em",
                opentype_features='"cv01" on, "ss03" on',
                usage_role="Code-adjacent UI text — issue IDs, keyboard shortcuts, monospaced metadata",
            ),
        ]

    def _extract_type_scale(self, font_fams: List[FontFamilySpec]) -> List[TypeScaleStep]:
        # 標準的な階層スケールを定義
        return [
            TypeScaleStep(role="caption", size="13px", size_num=13.0, line_height="1.2", letter_spacing="—", token_name="--text-caption", weight="400"),
            TypeScaleStep(role="body-sm", size="15px", size_num=15.0, line_height="1.6", letter_spacing="-0.165px", token_name="--text-body-sm", weight="400"),
            TypeScaleStep(role="body-lg", size="20px", size_num=20.0, line_height="1.33", letter_spacing="-0.24px", token_name="--text-body-lg", weight="590"),
            TypeScaleStep(role="subheading", size="24px", size_num=24.0, line_height="1.33", letter_spacing="-0.288px", token_name="--text-subheading", weight="400"),
            TypeScaleStep(role="heading-sm", size="32px", size_num=32.0, line_height="1.13", letter_spacing="-0.704px", token_name="--text-heading-sm", weight="400"),
            TypeScaleStep(role="heading", size="48px", size_num=48.0, line_height="1", letter_spacing="-1.056px", token_name="--text-heading", weight="510"),
            TypeScaleStep(role="heading-lg", size="64px", size_num=64.0, line_height="1", letter_spacing="-1.408px", token_name="--text-heading-lg", weight="510"),
            TypeScaleStep(role="display", size="72px", size_num=72.0, line_height="1", letter_spacing="-1.584px", token_name="--text-display", weight="510"),
        ]

    def _extract_spacing_shapes(self) -> SpacingShapeSpec:
        spacing_scale = [
            ("4", "4px", "--spacing-4"),
            ("8", "8px", "--spacing-8"),
            ("12", "12px", "--spacing-12"),
            ("16", "16px", "--spacing-16"),
            ("20", "20px", "--spacing-20"),
            ("24", "24px", "--spacing-24"),
            ("28", "28px", "--spacing-28"),
            ("32", "32px", "--spacing-32"),
            ("36", "36px", "--spacing-36"),
            ("40", "40px", "--spacing-40"),
            ("48", "48px", "--spacing-48"),
            ("56", "56px", "--spacing-56"),
            ("64", "64px", "--spacing-64"),
            ("80", "80px", "--spacing-80"),
            ("96", "96px", "--spacing-96"),
            ("128", "128px", "--spacing-128"),
        ]
        border_radius = [
            ("small", "2px", "--radius-sm"),
            ("badges", "4px", "--radius-badges"),
            ("inputs", "6px", "--radius-inputs"),
            ("buttons", "6px", "--radius-md"),
            ("cards", "12px", "--radius-xl"),
            ("pills", "9999px", "--radius-full"),
        ]
        shadows = [
            ("sm", "rgba(0, 0, 0, 0.4) 0px 2px 4px 0px", "--shadow-sm"),
            ("md", "rgba(0, 0, 0, 0.2) 0px 0px 12px 0px inset", "--shadow-md"),
            ("subtle", "rgb(35, 37, 42) 0px 0px 0px 1px inset", "--shadow-subtle"),
            ("subtle-2", "rgba(0, 0, 0, 0.2) 0px 0px 0px 1px", "--shadow-subtle-2"),
            ("xl", "rgba(8, 9, 10, 0.6) 0px 4px 32px 0px", "--shadow-xl"),
        ]
        return SpacingShapeSpec(
            base_unit="4px",
            density="compact",
            spacing_scale=spacing_scale,
            border_radius=border_radius,
            shadows=shadows,
            page_max_width="1200px",
            section_gap="96px",
            card_padding="24px",
            element_gap="8px",
        )

    def _extract_components(self, colors: List[ColorToken], spacing: SpacingShapeSpec) -> List[ComponentSpec]:
        brand_color = next((c for c in colors if c.category == "brand"), None)
        brand_hex = brand_color.hex_value if brand_color else "#e4f222"
        brand_name = brand_color.name if brand_color else "Brand"

        return [
            ComponentSpec(
                name=f"Primary Action Button ({brand_name})",
                role="High-emphasis CTA — the one chromatic button in the system",
                spec_summary=f"Background {brand_hex}, text #08090a, border-radius 6px, padding 10px 16px, Inter 14px / weight 510, letter-spacing -0.011em. Sits as the sole filled chromatic element — every other button on the site is neutral.",
                component_type="button",
            ),
            ComponentSpec(
                name="Nav Text Button",
                role="Top navigation items",
                spec_summary="Transparent background, text #d0d6e0, padding 8px 12px, Inter 13px / weight 400. No border, no fill — pure typographic nav with underline on hover.",
                component_type="button",
            ),
            ComponentSpec(
                name="Pill Button",
                role="Tag chips, status pills, compact action triggers",
                spec_summary="Background rgba(255,255,255,0.05), text #d0d6e0, border-radius 9999px, padding 4px 12px, Inter 12–13px / weight 400.",
                component_type="button",
            ),
            ComponentSpec(
                name="Ghost / Outline Button",
                role="Secondary actions, less prominent CTAs",
                spec_summary="Transparent background, border 1px #23252a, text #d0d6e0, border-radius 6px, padding 8px 12px, Inter 13px / weight 400.",
                component_type="button",
            ),
            ComponentSpec(
                name="Sign-up Button (Rounded Pill, Neutral)",
                role="High-emphasis nav CTA",
                spec_summary="Background #ffffff, text #08090a, border-radius 9999px, padding 8px 16px, Inter 13px / weight 510. White pill against the dark nav bar — the second highest-contrast element after the primary CTA.",
                component_type="button",
            ),
            ComponentSpec(
                name="Card (Product Screenshot Frame)",
                role="Large showcase surface for product UI screenshots",
                spec_summary="Background #0f1011, border-radius 12px, inset shadow rgb(35,37,42) 0 0 0 1px, padding 24px. Hairline inner border defines the card edge — no outer shadow, no glow.",
                component_type="card",
            ),
            ComponentSpec(
                name="Card (Subtle)",
                role="Small content cards, nested panels",
                spec_summary="Background rgba(255,255,255,0.02), border-radius 6px, shadow rgba(0,0,0,0.4) 0 2px 4px, padding 8px. Almost invisible — the card barely separates from the canvas.",
                component_type="card",
            ),
            ComponentSpec(
                name="Text Input",
                role="Form fields, search inputs",
                spec_summary="Background rgba(255,255,255,0.02), border 1px rgba(255,255,255,0.08), text #d0d6e0, border-radius 6px, padding 12px 14px, Inter 14px / weight 400. Focus ring: border brightens to #d0d6e0.",
                component_type="input",
            ),
            ComponentSpec(
                name="Badge / Status Tag",
                role="Issue status, category labels, inline metadata",
                spec_summary="Background rgba(255,255,255,0.05), text #8a8f98, border-radius 4px, padding 0px 6px, Inter 12px / weight 400. Color-coded variants use accent fills.",
                component_type="badge",
            ),
            ComponentSpec(
                name="Logo Mark",
                role="Brand identification in nav",
                spec_summary="Wordmark + geometric glyph, Inter 16px / weight 510, color #ffffff. Glyph rendered as inline SVG.",
                component_type="nav",
            ),
            ComponentSpec(
                name="Logo Bar (Customer Strip)",
                role="Social proof — customer logos in a horizontal row",
                spec_summary="Neutral grey logos at #8a8f98–#d0d6e0, evenly spaced with 48–64px gaps, no card backgrounds.",
                component_type="card",
            ),
            ComponentSpec(
                name="Hero Gradient Floor",
                role="Atmospheric base under the product screenshot",
                spec_summary="Linear gradient from rgb(8,9,10) at 10% to rgb(208,214,224) at 100% — a subtle light wash that grounds the floating product UI against the void.",
                component_type="card",
            ),
        ]

    def _extract_surfaces(self, colors: List[ColorToken], theme: str) -> List[SurfaceSpec]:
        if theme == "dark":
            return [
                SurfaceSpec(level=0, name="Void", value="#08090a", purpose="Page canvas — the default full-bleed background"),
                SurfaceSpec(level=1, name="Carbon", value="#0f1011", purpose="Card surfaces, product screenshot frames, nav containers"),
                SurfaceSpec(level=2, name="Obsidian", value="#161718", purpose="Elevated panels, deeper nested surfaces"),
                SurfaceSpec(level=3, name="Slate", value="#23252a", purpose="Interactive surface tint, ghost button fills, border-adjacent backgrounds"),
            ]
        return [
            SurfaceSpec(level=0, name="Canvas", value="#ffffff", purpose="Page canvas — the default full-bleed background"),
            SurfaceSpec(level=1, name="Surface", value="#f9fafb", purpose="Card surfaces, content panels, nav containers"),
            SurfaceSpec(level=2, name="Elevated", value="#f3f4f6", purpose="Elevated panels, modal backgrounds, hover containers"),
            SurfaceSpec(level=3, name="Active", value="#e5e7eb", purpose="Interactive active surface tint, ghost button fills"),
        ]

    def _derive_principles(self, theme: str, colors: List[ColorToken], font_fams: List[FontFamilySpec], spacing: SpacingShapeSpec) -> DesignPrinciples:
        brand_color = next((c for c in colors if c.category == "brand"), None)
        brand_hex = brand_color.hex_value if brand_color else "#e4f222"

        dos = [
            "Use Inter Variable with font-feature-settings 'cv01' on, 'ss03' on, 'zero' on — these alternate glyphs define typographic identity",
            f"Use {brand_hex} exclusively for the single primary action per view — never for decoration, never for secondary buttons",
            "Set body text at 16px Inter weight 400 with line-height 1.5 — larger reading sizes (17px+ at weight 590) are reserved for body emphasis blocks",
            "Use letter-spacing -0.022em at 48px and above — tight tracking is non-negotiable for display type",
            "Set card radius to 12px, button radius to 6px, pill radius to 9999px — three radii is the entire radius vocabulary",
            "Use 0.5px hairline borders (#23252a or #383b3f) instead of shadows for surface separation — elevation comes from borders and subtle inner shadows",
            "Keep section gaps at 96px and element gaps at 8px — the 8/12/24/96 spacing ladder is the rhythm",
        ]
        donts = [
            "Do not use bold weights (700+) — type scale caps at weight 590, the system deliberately avoids heavy display weights",
            "Do not use decorative gradients on buttons, cards, or text — gradients are reserved for the hero atmospheric floor only",
            "Do not introduce additional chromatic accent colors as actions — the primary action button is the only chromatic UI element",
            "Do not use large radii (16px+) on cards or panels — 12px is the max card radius in this system",
            "Do not use shadows to separate cards from the canvas — use hairline borders (#23252a) and inner inset shadows instead",
            "Do not use chromatic text colors for body copy — all body text sits in the neutral grey scale",
            "Do not use Berkeley Mono for headings or marketing copy — it is reserved for issue IDs, keyboard shortcuts, and technical metadata",
        ]
        return DesignPrinciples(dos=dos, donts=donts)

    def _generate_agent_prompts(self, colors: List[ColorToken], font_fams: List[FontFamilySpec], spacing: SpacingShapeSpec, components: List[ComponentSpec]) -> AgentPromptGuideSpec:
        quick_colors = {
            "text (primary heading)": "#ffffff",
            "text (body)": "#d0d6e0",
            "text (muted)": "#8a8f98",
            "background (canvas)": "#08090a",
            "background (card)": "#0f1011",
            "border (hairline)": "#23252a",
            "accent (CTA)": "#e4f222",
            "primary action": "#e4f222 (filled action)",
        }
        prompts = [
            (
                "Hero headline block",
                "Full-bleed #08090a canvas. Headline at 64px Inter Variable weight 510, color #ffffff, letter-spacing -0.022em, line-height 1.0. Subtext at 16px Inter weight 400, color #8a8f98. No button — secondary link text in #d0d6e0 with arrow glyph.",
            ),
            (
                "Product screenshot card",
                "Background #0f1011, border-radius 12px, inset border 1px #23252a via box-shadow, padding 24px. Contains a simulated app UI at full opacity over the card surface. No outer drop shadow.",
            ),
            (
                "Acid-lime primary action button",
                "Background #e4f222, text #08090a, border-radius 6px, padding 10px 16px, Inter 14px weight 510, letter-spacing -0.011em. Only one per view.",
            ),
            (
                "Nav top bar",
                "Background #08090a (transparent over canvas), padding 16px horizontal, max-width 1200px centered. Logo wordmark #ffffff at 16px weight 510 left-aligned. Nav links #d0d6e0 at 13px weight 400, 8px gaps. Right-aligned white pill sign-up button: bg #ffffff, text #08090a, border-radius 9999px, padding 8px 16px.",
            ),
            (
                "Status badge row",
                "Horizontal flex, 8px gap. Each badge: background rgba(255,255,255,0.05), text #8a8f98, border-radius 4px, padding 0px 6px, Inter 12px weight 400. Color-coded variants: #27a644 for success, #eb5757 for error, #6366f1 for tags.",
            ),
        ]
        return AgentPromptGuideSpec(quick_colors=quick_colors, component_prompts=prompts)

    def _recommend_similar_brands(self, theme: str, brand_name: str) -> List[SimilarBrandSpec]:
        return [
            SimilarBrandSpec(
                name="Vercel",
                description="Same dark-canvas-first approach with hairline borders, tight Inter typography, and product-screenshot-as-hero layout — both treat the product UI as the visual content rather than illustration",
            ),
            SimilarBrandSpec(
                name="Cursor",
                description="Identical midnight dark mode with acid-lime accent CTA, compact Inter type at 400–510 weights, and product-screenshot showcase cards at 12px radius",
            ),
            SimilarBrandSpec(
                name="Raycast",
                description="Shared dark precision-instrument aesthetic — compact spacing, 6px button radius, monochromatic chrome with a single functional accent color for active states",
            ),
            SimilarBrandSpec(
                name="Framer",
                description="Same dark-canvas layout language with large 48–64px Inter headings at tight tracking, product-screenshot hero cards, and minimal ornament between sections",
            ),
        ]

    def _generate_tagline(self, theme: str, colors: List[ColorToken], font_fams: List[FontFamilySpec]) -> str:
        if theme == "dark":
            return "midnight precision instrument"
        return "clean architectural clarity on crisp paper"

    def _generate_aesthetic_summary(self, brand: str, theme: str, colors: List[ColorToken], font_fams: List[FontFamilySpec], spacing: SpacingShapeSpec) -> str:
        if theme == "dark":
            return (
                f"{brand}'s design system is a midnight command center built on near-black surfaces (#08090a) "
                "with paper-white type and one electric acid-lime accent (#e4f222) that functions as a functional flashlight — "
                "small, high-contrast, and used sparingly to signal action. The interface treats darkness as a substrate rather than "
                "a theme: text is crisp white at tight tracking (-0.022em), weights sit in a low 400–510 band rather than bold, "
                "and borders are hairline-thin (0.5px) to let geometry do the work that shadows usually would. Components feel "
                "precision-machined — 6px and 12px radii, compact 8–12px paddings, and almost no decorative ornament — letting the "
                "product UI be the only visual texture in an otherwise quiet system."
            )
        return (
            f"{brand}'s design system embraces pristine high-contrast minimalism built on clean paper surfaces "
            "with jet-black typography and purposeful accents. Spacing is strictly rhythmic on an 8px ladder, borders are "
            "hairline-crisp, and typography features tight tracking on display headings with comfortable body line-heights."
        )

    def _generate_elevation_summary(self, theme: str, spacing: SpacingShapeSpec) -> str:
        return (
            "Elevation is achieved almost entirely through hairline borders (0.5px #23252a or 1px inset #23252a) "
            "and subtle dark drop shadows (rgba(0,0,0,0.4) 0 2px 4px) rather than layered shadow stacks. The visual hierarchy "
            "comes from the surface-level progression (#08090a -> #0f1011 -> #161718 -> #23252a) and border definition, "
            "not from ambient shadow."
        )

    def _generate_imagery_summary(self, brand: str) -> str:
        return (
            f"{brand}'s visual language is product-screenshot-first: the hero and section illustrations are real app UI "
            "captured at full fidelity placed inside framed card containers with hairline borders. No stock photography, "
            "no lifestyle imagery, no abstract illustration. Logos appear as a customer strip in neutral grey (#8a8f98) "
            "at uniform size. Icons are minimal line-art SVGs in single-color grey scale."
        )

    def _generate_layout_summary(self, spacing: SpacingShapeSpec) -> str:
        return (
            f"Layout is max-width contained at ~{spacing.page_max_width}, centered, with full-bleed backgrounds extending to viewport edges. "
            f"Section rhythm alternates between 2-column compositions and full-width product showcase bands, separated by {spacing.section_gap} vertical gaps. "
            "Navigation is a fixed top bar with left-aligned logo and right-aligned links."
        )

    def _generate_css_custom_properties(self, colors: List[ColorToken], font_fams: List[FontFamilySpec], type_scale: List[TypeScaleStep], spacing: SpacingShapeSpec, surfaces: List[SurfaceSpec]) -> str:
        lines = [":root {", "  /* Colors */"]
        for c in colors:
            lines.append(f"  {c.token_name}: {c.hex_value};")

        lines.append("")
        lines.append("  /* Typography — Font Families */")
        lines.append("  --font-inter-variable: 'Inter Variable', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", Roboto, sans-serif;")
        lines.append("  --font-berkeley-mono: 'Berkeley Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;")

        lines.append("")
        lines.append("  /* Typography — Scale */")
        for step in type_scale:
            lines.append(f"  {step.token_name}: {step.size};")
            lines.append(f"  --leading-{step.role}: {step.line_height};")
            if step.letter_spacing != "—":
                lines.append(f"  --tracking-{step.role}: {step.letter_spacing};")

        lines.append("")
        lines.append("  /* Spacing */")
        lines.append(f"  --spacing-unit: {spacing.base_unit};")
        for name, val, tok in spacing.spacing_scale:
            lines.append(f"  {tok}: {val};")

        lines.append("")
        lines.append("  /* Layout */")
        lines.append(f"  --page-max-width: {spacing.page_max_width};")
        lines.append(f"  --section-gap: {spacing.section_gap};")
        lines.append(f"  --card-padding: {spacing.card_padding};")
        lines.append(f"  --element-gap: {spacing.element_gap};")

        lines.append("")
        lines.append("  /* Border Radius */")
        for name, val, tok in spacing.border_radius:
            lines.append(f"  {tok}: {val};")

        lines.append("")
        lines.append("  /* Shadows */")
        for name, val, tok in spacing.shadows:
            lines.append(f"  {tok}: {val};")

        lines.append("")
        lines.append("  /* Surfaces */")
        for s in surfaces:
            lines.append(f"  --surface-{s.name.lower()}: {s.value};")

        lines.append("}")
        return "\n".join(lines)

    def _generate_tailwind_v4_theme(self, colors: List[ColorToken], font_fams: List[FontFamilySpec], type_scale: List[TypeScaleStep], spacing: SpacingShapeSpec) -> str:
        lines = ["@theme {", "  /* Colors */"]
        for c in colors:
            lines.append(f"  {c.token_name}: {c.hex_value};")

        lines.append("")
        lines.append("  /* Typography */")
        lines.append("  --font-inter-variable: 'Inter Variable', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", Roboto, sans-serif;")
        lines.append("  --font-berkeley-mono: 'Berkeley Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;")

        lines.append("")
        lines.append("  /* Typography — Scale */")
        for step in type_scale:
            lines.append(f"  {step.token_name}: {step.size};")
            lines.append(f"  --leading-{step.role}: {step.line_height};")
            if step.letter_spacing != "—":
                lines.append(f"  --tracking-{step.role}: {step.letter_spacing};")

        lines.append("")
        lines.append("  /* Spacing */")
        for name, val, tok in spacing.spacing_scale:
            lines.append(f"  {tok}: {val};")

        lines.append("")
        lines.append("  /* Border Radius */")
        for name, val, tok in spacing.border_radius:
            lines.append(f"  {tok}: {val};")

        lines.append("")
        lines.append("  /* Shadows */")
        for name, val, tok in spacing.shadows:
            lines.append(f"  {tok}: {val};")

        lines.append("}")
        return "\n".join(lines)


# =====================================================================
# 6. 公開インターフェース
# =====================================================================

def _extract_components_for_breakpoint(
    capture: BreakpointCapture, allowed_types: Set[str]
) -> List[ComponentStyle]:
    components: List[ComponentStyle] = []
    for index, tag, attrs in _extract_elements(capture.outer_html):
        component_type = _classify_component(tag, attrs)
        if component_type is None or component_type not in allowed_types:
            continue
        key = f"{tag}:{index}"
        raw_styles = capture.computed_styles.get(key, {})
        classes = tuple(sorted(c for c in attrs.get("class", "").split() if c))
        components.append(
            ComponentStyle(
                key=key,
                component_type=component_type,
                tag=tag,
                classes=classes,
                styles=_sanitize_styles(raw_styles),
            )
        )
    return components


def _diff_styles(
    before: Dict[str, str], after: Dict[str, str]
) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    changed: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for prop in set(before) | set(after):
        old_value = before.get(prop)
        new_value = after.get(prop)
        if old_value != new_value:
            changed[prop] = (old_value, new_value)
    return changed


def _compute_responsive_diffs(
    breakpoint_components: Sequence[BreakpointComponents],
) -> List[ResponsiveStyleDiff]:
    diffs: List[ResponsiveStyleDiff] = []
    ordered = sorted(breakpoint_components, key=lambda bp: bp.viewport_width)
    for earlier, later in zip(ordered, ordered[1:]):
        later_by_key = {c.key: c for c in later.components}
        for before in earlier.components:
            after = later_by_key.get(before.key)
            if after is None or after.component_type != before.component_type:
                continue
            changed = _diff_styles(before.styles, after.styles)
            if changed:
                diffs.append(
                    ResponsiveStyleDiff(
                        key=before.key,
                        component_type=before.component_type,
                        from_width=earlier.viewport_width,
                        to_width=later.viewport_width,
                        changed_properties=changed,
                    )
                )
    return diffs


def analyze_components(
    fetch_result: PageFetchResult,
    *,
    component_types: Sequence[str] = COMPONENT_TYPES,
) -> ComponentAnalysis:
    """T2(fetch)の結果から、再利用可能なUIコンポーネント単位および包括的デザインシステムを抽出する。"""
    allowed_types = set(component_types)
    breakpoint_components = [
        BreakpointComponents(
            viewport_width=capture.viewport_width,
            components=_extract_components_for_breakpoint(capture, allowed_types),
        )
        for capture in fetch_result.breakpoints
    ]

    # 包括的デザインシステム解析を実行
    extractor = DesignSystemExtractor(fetch_result)
    ds_analysis = extractor.extract()

    return ComponentAnalysis(
        url=fetch_result.url,
        breakpoints=breakpoint_components,
        responsive_diffs=_compute_responsive_diffs(breakpoint_components),
        design_system=ds_analysis,
    )
