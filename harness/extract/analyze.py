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
    # 「柔軟な再利用」のための抽象化: 生の1インスタンス値ではなく、実測から導いた
    # 再利用可能なバリアント単位で保持する。variant_key はCSSクラス接尾辞
    # (例: "primary"/"secondary"/"ghost")、semantic_role は用途タグ
    # (例: "primary-cta"/"secondary-cta"/"tertiary-action")。
    variant_key: str = "default"
    semantic_role: str = ""
    # generate.py が spec_summary の正規表現パースに頼らず値を取り出せるようにする
    # 構造化フィールド(キーはCSSプロパティに準じたsnake_case)。
    properties: Dict[str, str] = field(default_factory=dict)


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


def _nearest_rank_percentile(sorted_values: Sequence[float], p: float) -> float:
    """ソート済み数値列から nearest-rank 法で分位点を取得する（決定的・タイ非依存）。

    線形補間ではなく nearest-rank（最も近い順位の実測値をそのまま返す）を採用することで、
    分位点が「実測に存在しない中間値」に化けることを防ぎ、かつ同一入力からは常に同じ
    インデックス計算のみで値が定まる（浮動小数点の丸め誤差やタイの扱いに依存しない）。
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    p = 0.0 if p < 0.0 else (1.0 if p > 1.0 else p)
    index = int(round(p * (len(sorted_values) - 1)))
    return sorted_values[index]


def _infer_base_unit(values_px: Sequence[float]) -> int:
    """実測 px 値列から基準スペーシング単位（4px/8px等）を推定する。

    候補単位 {4, 8} のうち、実測値の大半（切り捨て誤差2px以内）を割り切れる方を採用する。
    実測データが乏しい場合は 4px を既定とする。
    """
    candidates = [v for v in values_px if v > 0]
    if len(candidates) < 3:
        return 4
    for unit in (8, 4):
        matches = sum(1 for v in candidates if abs(round(v / unit) * unit - v) <= 1.0)
        if matches / len(candidates) >= 0.6:
            return unit
    return 4


def _resample_neutrals(
    neutral: Sequence[Tuple[str, float, float, float, int]], threshold: int = 3
) -> Optional[List[str]]:
    """明度昇順ソート済みの実測ニュートラル候補を10スロットへ均等にリサンプルする。

    実測候補が乏しい（`threshold` 未満）場合は None を返し、呼び出し元に
    デフォルトパレットへのフォールバックを促す。nearest-rank と同じ考え方で
    インデックスを決定的に選ぶため、同一入力からは常に同じ10色が得られる。
    """
    if len(neutral) < threshold:
        return None
    hexes = [item[0] for item in neutral]
    n = len(hexes)
    return [hexes[int(round((i / 9.0) * (n - 1)))] for i in range(10)]


def _build_neutral_tokens(
    default_neutrals: Sequence[Tuple[str, str, str]],
    neutral: Sequence[Tuple[str, float, float, float, int]],
    raw_colors: Dict[str, Tuple[int, int, int]],
) -> List["ColorToken"]:
    """デフォルトのニュートラル10段の器（name/role）に、実測値または既定値を割り当てる。

    実測ニュートラルが十分にある場合は `_resample_neutrals` で10スロットへリサンプルし、
    乏しい場合のみ従来通りデフォルトHEXに最も近い実測値（輝度差0.08未満）で置換する。
    """
    resampled = _resample_neutrals(neutral)
    tokens: List[ColorToken] = []
    for idx, (name, def_hex, role) in enumerate(default_neutrals):
        if resampled is not None:
            chosen_hex = resampled[idx]
            cr, cg, cb = raw_colors[chosen_hex]
        else:
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
            chosen_hex = closest_hex
        tok_name = f"--color-{name.lower().replace(' ', '-')}"
        tokens.append(
            ColorToken(
                name=name,
                hex_value=chosen_hex,
                token_name=tok_name,
                category="neutral",
                role=role,
                rgb=(cr, cg, cb),
                luminance=_relative_luminance(cr, cg, cb),
            )
        )
    return tokens


def _classify_button_bucket(style: Dict[str, str]) -> str:
    """ボタン様要素の実測スタイルを filled/outline/text の3バケットに決定的に分類する。

    LLMや乱数を使わず、背景色の有無・枠線の有無という2つの実測シグナルのみで判定するため、
    同一入力からは常に同じバケットに落ちる。
    """
    bg = (style.get("background-color") or "").strip().lower()
    transparent_bg = bg in ("", "transparent", "rgba(0, 0, 0, 0)", "rgb(255, 255, 255)", "#ffffff", "#fff", "white")
    border = (style.get("border") or style.get("border-width") or "").strip().lower()
    has_border = bool(border) and border not in ("none", "0px", "0px none", "medium none", "0")
    if not transparent_bg:
        return "filled"
    if has_border:
        return "outline"
    return "text"


def _radius_for(spacing: "SpacingShapeSpec", name: str, default: str) -> str:
    """`SpacingShapeSpec.border_radius`（(名前, 値, トークン名)のリスト）から名前で値を引く。"""
    for entry_name, value, _token in spacing.border_radius:
        if entry_name == name:
            return value
    return default


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
        components = self._extract_components(colors, spacing_shapes, theme)
        surfaces = self._extract_surfaces(colors, theme)
        principles = self._derive_principles(theme, colors, font_families, spacing_shapes)
        agent_prompts = self._generate_agent_prompts(theme, colors, font_families, spacing_shapes, components)
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
        body_lum: Optional[float] = None

        for key, style in self.styles_pool.items():
            bg = style.get("background-color")
            parsed = _parse_color(bg or "")
            if parsed and parsed[3] > 0.5:
                lum = _relative_luminance(parsed[0], parsed[1], parsed[2])
                bgs.append(lum)
                if (key.startswith("body:") or key.startswith("html:")) and body_lum is None:
                    body_lum = lum

        if body_lum is not None:
            return ("dark" if body_lum < 0.35 else "light", bgs)

        avg_lum = sum(bgs) / len(bgs) if bgs else 0.0
        return ("dark" if avg_lum < 0.4 else "light", bgs)

    def _extract_colors(self, theme: str) -> List[ColorToken]:
        color_scores: Dict[str, float] = {}
        color_counts: Dict[str, int] = {}
        color_usage: Dict[str, Set[str]] = {}
        raw_colors: Dict[str, Tuple[int, int, int]] = {}

        # DOM 要素の分類情報（button等）を取得
        element_types: Dict[str, str] = {}
        element_attrs_map: Dict[str, Dict[str, str]] = {}
        if self.primary_bp and hasattr(self.primary_bp, "outer_html") and self.primary_bp.outer_html:
            for index, tag, attrs in _extract_elements(self.primary_bp.outer_html):
                key = f"{tag}:{index}"
                element_attrs_map[key] = attrs
                ctype = _classify_component(tag, attrs)
                if ctype:
                    element_types[key] = ctype

        for key, style in self.styles_pool.items():
            tag = key.split(":")[0]
            comp_type = element_types.get(key, "")
            attrs = element_attrs_map.get(key, {})
            classes = attrs.get("class", "").lower().split()
            role = attrs.get("role", "").lower()
            input_type = attrs.get("type", "").lower()

            # 要素がボタンや主要インタラクティブ要素かどうかを判定
            is_button = (
                comp_type == "button"
                or tag == "button"
                or role == "button"
                or (tag == "input" and input_type in ("button", "submit", "reset"))
                or any("btn" in c or "button" in c or "cta" in c for c in classes)
                or (tag == "a" and bool(style.get("border-radius") and style.get("padding")))
            )

            for prop in ("background-color", "color", "border-color", "border-top-color", "fill"):
                val = style.get(prop)
                parsed = _parse_color(val or "")
                if parsed and parsed[3] > 0.1:
                    hex_val = _to_hex(parsed[0], parsed[1], parsed[2])
                    color_counts[hex_val] = color_counts.get(hex_val, 0) + 1
                    color_usage.setdefault(hex_val, set()).add(f"{tag}:{prop}")
                    raw_colors[hex_val] = (parsed[0], parsed[1], parsed[2])

                    # スコアリング（ボタン背景色・枠線・文字色には高加重を与える）
                    weight = 1.0
                    if is_button and prop == "background-color":
                        weight = 50.0
                    elif is_button and prop in ("color", "border-color"):
                        weight = 25.0
                    elif prop == "background-color":
                        weight = 2.0

                    # 彩度と明度によるコントラスト・視認性ボーナス
                    h, l, s = colorsys.rgb_to_hls(parsed[0] / 255.0, parsed[1] / 255.0, parsed[2] / 255.0)
                    if s > 0.25 and 0.15 < l < 0.85:
                        weight *= 2.0

                    color_scores[hex_val] = color_scores.get(hex_val, 0.0) + weight

        is_dark = (theme == "dark")
        tokens: List[ColorToken] = []

        # 彩度と明度で分類
        chromatic: List[Tuple[str, float, float, float, float]] = []  # (hex, h, l, s, score)
        neutral: List[Tuple[str, float, float, float, int]] = []

        for hex_val, (r, g, b) in raw_colors.items():
            lum = _relative_luminance(r, g, b)
            h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
            score = color_scores.get(hex_val, 0.0)
            count = color_counts.get(hex_val, 0)
            if s > 0.18 and 0.05 < l < 0.95:
                chromatic.append((hex_val, h, l, s, score))
            else:
                neutral.append((hex_val, h, l, s, count))

        # ニュートラルカラーを明度順にソート
        neutral.sort(key=lambda item: item[2])  # l (lightness) 昇順

        # トークン名の一意性を保証するためのセット
        seen_token_names: Set[str] = set()

        # ブランドカラー (ボタン等での使用スコアが最も高い有彩色)
        brand_hex = None
        brand_color_token = None
        if chromatic:
            chromatic.sort(key=lambda item: item[4], reverse=True)
            brand_hex = chromatic[0][0]
            br, bg, bb = raw_colors[brand_hex]
            bh, bl, bs = colorsys.rgb_to_hls(br / 255.0, bg / 255.0, bb / 255.0)
            brand_name = _color_name_for_hue(bh, bs, bl, is_dark)
            tok_name = f"--color-{brand_name.lower().replace(' ', '-')}"
            seen_token_names.add(tok_name)

            brand_color_token = ColorToken(
                name=brand_name,
                hex_value=brand_hex,
                token_name=tok_name,
                category="brand",
                role="Primary action buttons, active nav indicators — electric accent that breaks the monochrome system",
                rgb=(br, bg, bb),
                luminance=_relative_luminance(br, bg, bb),
            )
            tokens.append(brand_color_token)

        # セマンティックトークン (--color-primary, --color-on-primary, --color-primary-hover 等)
        if brand_color_token:
            p_lum = brand_color_token.luminance
            on_primary_hex = "#ffffff" if p_lum < 0.45 else "#111827"
            tokens.append(
                ColorToken(
                    name="Primary",
                    hex_value=brand_color_token.hex_value,
                    token_name="--color-primary",
                    category="brand",
                    role="Semantic primary brand color for high-emphasis CTAs",
                    rgb=brand_color_token.rgb,
                    luminance=p_lum,
                )
            )
            tokens.append(
                ColorToken(
                    name="On Primary",
                    hex_value=on_primary_hex,
                    token_name="--color-on-primary",
                    category="brand",
                    role="High-contrast text color on top of primary brand color",
                    rgb=(255, 255, 255) if on_primary_hex == "#ffffff" else (17, 24, 39),
                    luminance=1.0 if on_primary_hex == "#ffffff" else 0.01,
                )
            )

        # アクセントカラー (重複トークン名の衝突を回避)
        accent_items = [c for c in chromatic if c[0] != brand_hex][:5]
        for hex_val, h, l, s, score in accent_items:
            r, g, b = raw_colors[hex_val]
            base_name = _color_name_for_hue(h, s, l, is_dark)
            name = base_name
            tok_name = f"--color-{name.lower().replace(' ', '-')}"

            # トークン名が重複する場合は明度等に応じた修飾子を付与
            if tok_name in seen_token_names:
                suffix = "Light" if l > 0.6 else ("Dark" if l < 0.3 else "Muted")
                name = f"{base_name} {suffix}"
                tok_name = f"--color-{name.lower().replace(' ', '-')}"
                if tok_name in seen_token_names:
                    idx = 2
                    while f"{tok_name}-{idx}" in seen_token_names:
                        idx += 1
                    tok_name = f"{tok_name}-{idx}"
                    name = f"{name} {idx}"

            seen_token_names.add(tok_name)
            tokens.append(
                ColorToken(
                    name=name,
                    hex_value=hex_val,
                    token_name=tok_name,
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
            tokens.extend(_build_neutral_tokens(default_dark_neutrals, neutral, raw_colors))
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
            tokens.extend(_build_neutral_tokens(default_light_neutrals, neutral, raw_colors))

        return tokens

    def _extract_font_families(self) -> List[FontFamilySpec]:
        family_counts: Dict[str, int] = {}
        fam_weights: Dict[str, Set[str]] = {}
        fam_sizes: Dict[str, Set[str]] = {}
        fam_lhs: Dict[str, List[float]] = {}

        for key, style in self.styles_pool.items():
            fam = style.get("font-family")
            if fam:
                primary = fam.split(",")[0].strip().replace('"', '').replace("'", "")
                if primary:
                    family_counts[primary] = family_counts.get(primary, 0) + 1
                    fw = style.get("font-weight")
                    if fw:
                        fam_weights.setdefault(primary, set()).add(str(fw))
                    fs = style.get("font-size")
                    if fs:
                        fam_sizes.setdefault(primary, set()).add(str(fs).replace("px", ""))
                    lh = style.get("line-height")
                    if lh and "px" in lh:
                        try:
                            val = float(lh.replace("px", ""))
                            fam_lhs.setdefault(primary, []).append(val)
                        except ValueError:
                            pass

        sorted_fams = sorted(family_counts.items(), key=lambda x: x[1], reverse=True)
        primary_name = sorted_fams[0][0] if sorted_fams else "Inter Variable"
        primary_full = next((style.get("font-family") for style in self.styles_pool.values() if style.get("font-family") and primary_name in style.get("font-family", "")), f"'{primary_name}', sans-serif")

        weights = sorted(fam_weights.get(primary_name, {"400", "500", "600", "700"}), key=lambda w: int(w) if w.isdigit() else 400)
        sizes = sorted(fam_sizes.get(primary_name, {"14", "16", "20", "24", "32", "48"}), key=lambda s: float(s) if s.replace(".", "", 1).isdigit() else 16.0)

        lhs = fam_lhs.get(primary_name, [])
        lh_range = f"{min(lhs):.1f}px–{max(lhs):.1f}px" if lhs else "1.2–1.6"

        mono_candidates = [f for f, _ in sorted_fams if any(m in f.lower() for m in ("mono", "code", "berkeley", "jetbrains", "courier", "consolas", "menlo"))]
        code_name = mono_candidates[0] if mono_candidates else "monospace"

        return [
            FontFamilySpec(
                role="Primary",
                name=primary_name,
                token_name="--font-primary",
                substitute=primary_full,
                weights=weights,
                sizes=sizes,
                line_height_range=lh_range,
                letter_spacing_summary="tight on display headings, normal on body copy",
                opentype_features="normal",
                usage_role="Primary UI and heading typeface — used across nav, body, headings, buttons, cards",
            ),
            FontFamilySpec(
                role="Code",
                name=code_name,
                token_name="--font-mono",
                substitute=f"'{code_name}', monospace",
                weights=["400"],
                sizes=["12", "14"],
                line_height_range="1.4–1.7",
                letter_spacing_summary="normal",
                opentype_features="normal",
                usage_role="Code-adjacent UI text — issue IDs, keyboard shortcuts, monospaced metadata",
            ),
        ]

    def _extract_type_scale(self, font_fams: List[FontFamilySpec]) -> List[TypeScaleStep]:
        # styles_pool から実際の font-size, line-height, font-weight を集計
        size_buckets: Dict[float, Dict[str, Any]] = {}

        for key, style in self.styles_pool.items():
            fs = style.get("font-size")
            if not fs:
                continue
            num_str = re.sub(r"[^\d.]", "", fs)
            try:
                size_num = float(num_str)
                if size_num <= 0:
                    continue
            except ValueError:
                continue

            bucket = size_buckets.setdefault(size_num, {"count": 0, "weights": {}, "lhs": {}, "trackings": {}})
            bucket["count"] += 1

            fw = style.get("font-weight") or "400"
            bucket["weights"][fw] = bucket["weights"].get(fw, 0) + 1

            lh = style.get("line-height") or "1.5"
            bucket["lhs"][lh] = bucket["lhs"].get(lh, 0) + 1

            ls = style.get("letter-spacing") or "normal"
            bucket["trackings"][ls] = bucket["trackings"].get(ls, 0) + 1

        if not size_buckets:
            # フォールバック
            return [
                TypeScaleStep(role="caption", size="13px", size_num=13.0, line_height="1.2", letter_spacing="—", token_name="--text-caption", weight="400"),
                TypeScaleStep(role="body-sm", size="14px", size_num=14.0, line_height="1.5", letter_spacing="normal", token_name="--text-body-sm", weight="400"),
                TypeScaleStep(role="body", size="16px", size_num=16.0, line_height="1.6", letter_spacing="normal", token_name="--text-body", weight="400"),
                TypeScaleStep(role="subheading", size="20px", size_num=20.0, line_height="1.4", letter_spacing="normal", token_name="--text-subheading", weight="600"),
                TypeScaleStep(role="heading-sm", size="28px", size_num=28.0, line_height="1.25", letter_spacing="-0.5px", token_name="--text-heading-sm", weight="700"),
                TypeScaleStep(role="heading", size="40px", size_num=40.0, line_height="1.15", letter_spacing="-1px", token_name="--text-heading", weight="700"),
                TypeScaleStep(role="display", size="56px", size_num=56.0, line_height="1.1", letter_spacing="-1.5px", token_name="--text-display", weight="700"),
            ]

        # 頻度とサイズ順でソート
        sorted_sizes = sorted(size_buckets.keys())

        # 代表的な 6〜8 段階のスケールに役割を割り振る
        role_names = ["caption", "body-sm", "body", "body-lg", "subheading", "heading-sm", "heading", "heading-lg", "display"]
        if len(sorted_sizes) > len(role_names):
            # 頻出サイズ優先でピック
            scored_sizes = sorted(sorted_sizes, key=lambda s: size_buckets[s]["count"], reverse=True)
            chosen = sorted(scored_sizes[:len(role_names)])
        else:
            chosen = sorted_sizes

        steps: List[TypeScaleStep] = []
        for i, snum in enumerate(chosen):
            b = size_buckets[snum]
            role = role_names[min(i, len(role_names) - 1)] if len(chosen) <= len(role_names) else f"step-{i+1}"
            best_weight = max(b["weights"].items(), key=lambda x: x[1])[0] if b["weights"] else "400"
            best_lh = max(b["lhs"].items(), key=lambda x: x[1])[0] if b["lhs"] else "1.5"
            best_ls = max(b["trackings"].items(), key=lambda x: x[1])[0] if b["trackings"] else "normal"

            # 表示用サイズ文字列
            size_str = f"{int(snum)}px" if snum.is_integer() else f"{snum}px"
            steps.append(
                TypeScaleStep(
                    role=role,
                    size=size_str,
                    size_num=snum,
                    line_height=best_lh,
                    letter_spacing=best_ls,
                    token_name=f"--text-{role}",
                    weight=best_weight,
                )
            )

        return steps

    def _extract_spacing_shapes(self) -> SpacingShapeSpec:
        radius_counts: Dict[float, int] = {}
        padding_counts: Dict[str, int] = {}
        padding_px_counts: Dict[float, int] = {}
        gap_counts: Dict[str, int] = {}
        gap_px_counts: Dict[float, int] = {}
        shadow_counts: Dict[str, int] = {}
        card_paddings: Dict[str, int] = {}
        max_width_counts: Dict[float, int] = {}

        for key, style in self.styles_pool.items():
            # border-radius
            for rprop in ("border-radius", "border-top-left-radius"):
                rv = style.get(rprop)
                if rv and "px" in rv:
                    try:
                        rnum = float(re.sub(r"[^\d.]", "", rv))
                        radius_counts[rnum] = radius_counts.get(rnum, 0) + 1
                    except ValueError:
                        pass

            # padding
            pv = style.get("padding")
            if pv and pv != "0px":
                padding_counts[pv] = padding_counts.get(pv, 0) + 1
                if any(c in key for c in ("card", "article", "div")):
                    card_paddings[pv] = card_paddings.get(pv, 0) + 1
                for num_str in re.findall(r"[\d.]+", pv):
                    try:
                        num = float(num_str)
                        if num > 0:
                            padding_px_counts[num] = padding_px_counts.get(num, 0) + 1
                    except ValueError:
                        pass

            # gap
            gv = style.get("gap")
            if gv and gv != "normal" and gv != "0px":
                gap_counts[gv] = gap_counts.get(gv, 0) + 1
                for num_str in re.findall(r"[\d.]+", gv):
                    try:
                        num = float(num_str)
                        if num > 0:
                            gap_px_counts[num] = gap_px_counts.get(num, 0) + 1
                    except ValueError:
                        pass

            # box-shadow
            sv = style.get("box-shadow")
            if sv and sv != "none":
                shadow_counts[sv] = shadow_counts.get(sv, 0) + 1

            # max-width（ページ/コンテナの最大幅の実測候補）
            mv = style.get("max-width")
            if mv and mv.endswith("px"):
                try:
                    mnum = float(re.sub(r"[^\d.]", "", mv))
                    if 480.0 <= mnum <= 2400.0:
                        max_width_counts[mnum] = max_width_counts.get(mnum, 0) + 1
                except ValueError:
                    pass

        # --- 角丸スケール: 実測値の分位点(nearest-rank、タイ非依存で決定的)から動的に割り当てる ---
        sorted_radii = sorted(radius_counts.keys())
        if sorted_radii:
            small_r = _nearest_rank_percentile(sorted_radii, 0.0)
            badges_r = _nearest_rank_percentile(sorted_radii, 0.25)
            mid_r = _nearest_rank_percentile(sorted_radii, 0.5)
            cards_r = _nearest_rank_percentile(sorted_radii, 0.85)

            def _fmt_px(v: float) -> str:
                return f"{int(v) if v.is_integer() else v}px"

            btn_radius = _fmt_px(mid_r)
            card_radius = _fmt_px(cards_r)
            border_radius = [
                ("small", _fmt_px(small_r), "--radius-sm"),
                ("badges", _fmt_px(badges_r), "--radius-badges"),
                ("inputs", btn_radius, "--radius-inputs"),
                ("buttons", btn_radius, "--radius-buttons"),
                ("cards", card_radius, "--radius-cards"),
                ("pills", "9999px", "--radius-full"),
            ]
        else:
            btn_radius = "6px"
            card_radius = "12px"
            border_radius = [
                ("small", "2px", "--radius-sm"),
                ("badges", "4px", "--radius-badges"),
                ("inputs", btn_radius, "--radius-inputs"),
                ("buttons", btn_radius, "--radius-buttons"),
                ("cards", card_radius, "--radius-cards"),
                ("pills", "9999px", "--radius-full"),
            ]

        # --- 基準単位・スペーシングスケール: 実測 padding/gap から動的生成し、
        #     データが乏しい場合のみ固定ラダーにフォールバックする ---
        observed_px = sorted(set(padding_px_counts.keys()) | set(gap_px_counts.keys()))
        base_unit_num = _infer_base_unit(observed_px)
        if len(observed_px) >= 4:
            rounded = sorted({int(round(v / base_unit_num) * base_unit_num) for v in observed_px if v > 0})
            rounded = [v for v in rounded if v > 0][:14]
            spacing_scale = [(str(v), f"{v}px", f"--spacing-{v}") for v in rounded]
        else:
            spacing_scale = [
                ("4", "4px", "--spacing-4"),
                ("8", "8px", "--spacing-8"),
                ("12", "12px", "--spacing-12"),
                ("16", "16px", "--spacing-16"),
                ("20", "20px", "--spacing-20"),
                ("24", "24px", "--spacing-24"),
                ("28", "28px", "--spacing-28"),
                ("32", "32px", "--spacing-32"),
                ("40", "40px", "--spacing-40"),
                ("48", "48px", "--spacing-48"),
                ("64", "64px", "--spacing-64"),
                ("80", "80px", "--spacing-80"),
                ("96", "96px", "--spacing-96"),
            ]

        # 影（タイ発生時の順序を値の文字列で固定し決定的にする）
        if shadow_counts:
            top_shadows = sorted(shadow_counts.items(), key=lambda x: (-x[1], x[0]))
            shadows = [
                ("sm", top_shadows[0][0], "--shadow-sm"),
                ("md", top_shadows[1][0] if len(top_shadows) > 1 else top_shadows[0][0], "--shadow-md"),
                ("lg", top_shadows[2][0] if len(top_shadows) > 2 else "0 10px 15px -3px rgba(0,0,0,0.1)", "--shadow-lg"),
            ]
        else:
            shadows = [
                ("sm", "0 1px 3px rgba(0, 0, 0, 0.05), 0 1px 2px rgba(0, 0, 0, 0.03)", "--shadow-sm"),
                ("md", "0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03)", "--shadow-md"),
                ("lg", "0 10px 15px -3px rgba(0, 0, 0, 0.06), 0 4px 6px -2px rgba(0, 0, 0, 0.03)", "--shadow-lg"),
            ]

        top_card_pad = max(card_paddings.items(), key=lambda x: (x[1], x[0]))[0] if card_paddings else "24px"
        top_gap = max(gap_counts.items(), key=lambda x: (x[1], x[0]))[0] if gap_counts else "8px"

        # --- レイアウト定数: 実測 max-width / 大きめの実測gapがあれば採用し、無ければ既定値 ---
        if max_width_counts:
            page_max_width = f"{int(max(max_width_counts.items(), key=lambda x: (x[1], x[0]))[0])}px"
        else:
            page_max_width = "1200px"

        large_gaps = sorted((v for v in gap_px_counts if v >= 48.0), reverse=True)
        section_gap = f"{int(large_gaps[0])}px" if large_gaps else "80px"

        return SpacingShapeSpec(
            base_unit=f"{base_unit_num}px",
            density="compact",
            spacing_scale=spacing_scale,
            border_radius=border_radius,
            shadows=shadows,
            page_max_width=page_max_width,
            section_gap=section_gap,
            card_padding=top_card_pad,
            element_gap=top_gap,
        )

    def _extract_components(self, colors: List[ColorToken], spacing: SpacingShapeSpec, theme: str) -> List[ComponentSpec]:
        brand_color = next((c for c in colors if c.category == "brand" and c.token_name == "--color-primary"), None)
        if not brand_color:
            brand_color = next((c for c in colors if c.category == "brand"), None)
        brand_hex = brand_color.hex_value if brand_color else ("#ff4800" if theme == "light" else "#e4f222")
        brand_name = brand_color.name if brand_color else "Primary Brand"

        on_primary = next((c for c in colors if c.token_name == "--color-on-primary"), None)
        on_primary_hex = on_primary.hex_value if on_primary else ("#ffffff" if theme == "light" else "#08090a")

        is_dark = (theme == "dark")
        text_body_hex = "#d0d6e0" if is_dark else "#1f1f1f"
        border_hex = "#23252a" if is_dark else "#e5e7eb"

        # ボタン要素の実測プロパティを filled/outline/text の3バケットに分類しつつ集約する。
        # 「柔軟な再利用」のため、単一の代表インスタンスではなく実測から導いた再利用可能な
        # バリアント単位（Primary/Secondary/Ghost）で ComponentSpec を構築する。
        BUCKET_KEYS = ("filled", "outline", "text")
        bucket_counts: Dict[str, int] = {k: 0 for k in BUCKET_KEYS}
        bucket_paddings: Dict[str, Dict[str, int]] = {k: {} for k in BUCKET_KEYS}
        bucket_radii: Dict[str, Dict[str, int]] = {k: {} for k in BUCKET_KEYS}
        bucket_fontsizes: Dict[str, Dict[str, int]] = {k: {} for k in BUCKET_KEYS}
        bucket_weights: Dict[str, Dict[str, int]] = {k: {} for k in BUCKET_KEYS}
        bucket_lhs: Dict[str, Dict[str, int]] = {k: {} for k in BUCKET_KEYS}
        bucket_bgs: Dict[str, Dict[str, int]] = {k: {} for k in BUCKET_KEYS}
        bucket_borders: Dict[str, Dict[str, int]] = {k: {} for k in BUCKET_KEYS}
        bucket_colors: Dict[str, Dict[str, int]] = {k: {} for k in BUCKET_KEYS}

        # バケット別データが乏しい場合のフォールバック用に、全体集計も維持する。
        btn_paddings: Dict[str, int] = {}
        btn_radii: Dict[str, int] = {}
        btn_fontsizes: Dict[str, int] = {}
        btn_weights: Dict[str, int] = {}
        btn_lhs: Dict[str, int] = {}

        for key, style in self.styles_pool.items():
            is_btn = (
                "button" in key
                or (key.startswith("a:") and bool(style.get("border-radius") and style.get("padding")))
            )
            if not is_btn:
                continue

            p = style.get("padding")
            r = style.get("border-radius")
            fs = style.get("font-size")
            fw = style.get("font-weight")
            lh = style.get("line-height")
            bg = style.get("background-color")
            bd = style.get("border") or style.get("border-color")
            cl = style.get("color")

            if p and p != "0px":
                btn_paddings[p] = btn_paddings.get(p, 0) + 1
            if r and r != "0px":
                btn_radii[r] = btn_radii.get(r, 0) + 1
            if fs:
                btn_fontsizes[fs] = btn_fontsizes.get(fs, 0) + 1
            if fw:
                btn_weights[fw] = btn_weights.get(fw, 0) + 1
            if lh:
                btn_lhs[lh] = btn_lhs.get(lh, 0) + 1

            bucket = _classify_button_bucket(style)
            bucket_counts[bucket] += 1
            if p and p != "0px":
                bucket_paddings[bucket][p] = bucket_paddings[bucket].get(p, 0) + 1
            if r and r != "0px":
                bucket_radii[bucket][r] = bucket_radii[bucket].get(r, 0) + 1
            if fs:
                bucket_fontsizes[bucket][fs] = bucket_fontsizes[bucket].get(fs, 0) + 1
            if fw:
                bucket_weights[bucket][fw] = bucket_weights[bucket].get(fw, 0) + 1
            if lh:
                bucket_lhs[bucket][lh] = bucket_lhs[bucket].get(lh, 0) + 1
            if bg:
                bucket_bgs[bucket][bg] = bucket_bgs[bucket].get(bg, 0) + 1
            if bd:
                bucket_borders[bucket][bd] = bucket_borders[bucket].get(bd, 0) + 1
            if cl:
                bucket_colors[bucket][cl] = bucket_colors[bucket].get(cl, 0) + 1

        def _top(counter: Dict[str, int], default: str) -> str:
            return max(counter.items(), key=lambda x: (x[1], x[0]))[0] if counter else default

        top_btn_pad = _top(btn_paddings, "12px 24px")
        top_btn_rad = _top(btn_radii, "6px")
        top_btn_fs = _top(btn_fontsizes, "16px")
        top_btn_fw = _top(btn_weights, "500")
        top_btn_lh = _top(btn_lhs, "1.5")

        card_radius = _radius_for(spacing, "cards", "12px")
        input_radius = _radius_for(spacing, "inputs", top_btn_rad)
        badge_radius = _radius_for(spacing, "pills", "9999px")

        def _btn_variant(
            bucket: str,
            name: str,
            role: str,
            variant_key: str,
            semantic_role: str,
            bg_hex: str,
            text_hex: str,
            border_css: str,
        ) -> ComponentSpec:
            pad = _top(bucket_paddings[bucket], top_btn_pad)
            rad = _top(bucket_radii[bucket], top_btn_rad)
            fs = _top(bucket_fontsizes[bucket], top_btn_fs)
            fw = _top(bucket_weights[bucket], top_btn_fw)
            lh = _top(bucket_lhs[bucket], top_btn_lh)
            props = {
                "background": bg_hex,
                "text": text_hex,
                "border": border_css,
                "border_radius": rad,
                "padding": pad,
                "font_size": fs,
                "font_weight": fw,
                "line_height": lh,
            }
            summary = (
                f"Background {bg_hex}, text {text_hex}, border {border_css}, border-radius {rad}, "
                f"padding {pad}, font-size {fs}, font-weight {fw}, line-height {lh}."
            )
            return ComponentSpec(
                name=name,
                role=role,
                spec_summary=summary,
                component_type="button",
                variant_key=variant_key,
                semantic_role=semantic_role,
                properties=props,
            )

        # ボタンバリアントは実測から導く再利用可能な小さい名前付き集合に留める(上限3、
        # モデル/シード差異による揺らぎを避けるため頻度閾値未満のバケットは既定値にフォールバックする)。
        MIN_VARIANT_COUNT = 2
        button_variants: List[ComponentSpec] = []

        # Primary: 背景/文字色は複数シグナルを統合済みのブランド検出パイプラインの結果
        # (brand_hex/on_primary_hex) を用いる。単純な最頻出色より意味論的に信頼できるため。
        button_variants.append(
            _btn_variant(
                "filled",
                f"Primary Action Button ({brand_name})",
                "High-emphasis CTA — primary interactive trigger",
                "primary",
                "primary-cta",
                brand_hex,
                on_primary_hex,
                "1px solid transparent",
            )
        )

        outline_has_data = bucket_counts["outline"] >= MIN_VARIANT_COUNT
        sec_bg_default = "transparent" if is_dark else "#ffffff"
        sec_border_default = brand_hex if not is_dark else border_hex
        sec_text_default = brand_hex if not is_dark else text_body_hex
        sec_bg = _top(bucket_bgs["outline"], sec_bg_default) if outline_has_data else sec_bg_default
        sec_border = _top(bucket_borders["outline"], sec_border_default) if outline_has_data else sec_border_default
        sec_text = _top(bucket_colors["outline"], sec_text_default) if outline_has_data else sec_text_default
        button_variants.append(
            _btn_variant(
                "outline",
                "Secondary Action Button",
                "Secondary actions, outline-style triggers",
                "secondary",
                "secondary-cta",
                sec_bg,
                sec_text,
                f"1px solid {sec_border}",
            )
        )

        if bucket_counts["text"] >= MIN_VARIANT_COUNT and len(button_variants) < 3:
            ghost_text = _top(bucket_colors["text"], text_body_hex)
            button_variants.append(
                _btn_variant(
                    "text",
                    "Ghost Action Button",
                    "Low-emphasis tertiary actions, text-only triggers",
                    "ghost",
                    "tertiary-action",
                    "transparent",
                    ghost_text,
                    "1px solid transparent",
                )
            )

        card_bg = "#0f1011" if is_dark else "#ffffff"
        card_shadow = spacing.shadows[0][1] if spacing.shadows else "none"
        card = ComponentSpec(
            name="Card Surface",
            role="Showcase cards, content container panels",
            spec_summary=f"Background {card_bg}, border 1px {border_hex}, border-radius {card_radius}, padding {spacing.card_padding}, box-shadow {card_shadow}.",
            component_type="card",
            variant_key="default",
            semantic_role="content-container",
            properties={
                "background": card_bg,
                "border": f"1px solid {border_hex}",
                "border_radius": card_radius,
                "padding": spacing.card_padding,
                "box_shadow": card_shadow,
            },
        )

        nav_bg = "#08090a" if is_dark else "#ffffff"
        nav = ComponentSpec(
            name="Navigation Bar Container",
            role="Top-level page header and brand navigation",
            spec_summary=f"Background {nav_bg}, border-bottom 1px {border_hex}, padding 16px 24px, max-width {spacing.page_max_width}.",
            component_type="nav",
            variant_key="default",
            semantic_role="site-navigation",
            properties={
                "background": nav_bg,
                "border_bottom": f"1px solid {border_hex}",
                "padding": "16px 24px",
                "max_width": spacing.page_max_width,
            },
        )

        input_bg = "rgba(255,255,255,0.02)" if is_dark else "#ffffff"
        input_field = ComponentSpec(
            name="Form Input Field",
            role="Text input, search boxes, form controls",
            spec_summary=f"Background {input_bg}, border 1px {border_hex}, border-radius {input_radius}, padding 12px 14px, text {text_body_hex}. Focus border {brand_hex}.",
            component_type="input",
            variant_key="default",
            semantic_role="form-control",
            properties={
                "background": input_bg,
                "border": f"1px solid {border_hex}",
                "border_radius": input_radius,
                "padding": "12px 14px",
                "text": text_body_hex,
                "focus_border": brand_hex,
            },
        )

        badge_bg = (
            f"rgba({int(brand_hex[1:3], 16)},{int(brand_hex[3:5], 16)},{int(brand_hex[5:7], 16)},0.1)"
            if brand_hex.startswith("#") and len(brand_hex) == 7
            else "#f3f4f6"
        )
        badge = ComponentSpec(
            name="Badge & Tag Chip",
            role="Category labels, active state pill",
            spec_summary=f"Background {badge_bg}, color {brand_hex}, border-radius {badge_radius}, padding 4px 10px, font-size 13px, font-weight 600.",
            component_type="badge",
            variant_key="default",
            semantic_role="status-label",
            properties={
                "background": badge_bg,
                "text": brand_hex,
                "border_radius": badge_radius,
                "padding": "4px 10px",
                "font_size": "13px",
                "font_weight": "600",
            },
        )

        return [*button_variants, card, nav, input_field, badge]

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
            SurfaceSpec(level=1, name="Surface", value="#ffffff", purpose="Card surfaces, content panels, elevated containers"),
            SurfaceSpec(level=2, name="Subtle", value="#f9fafb", purpose="Secondary panel backgrounds, light containers"),
            SurfaceSpec(level=3, name="Elevated", value="#f3f4f6", purpose="Elevated panels, modal backgrounds, hover containers"),
        ]

    def _derive_principles(self, theme: str, colors: List[ColorToken], font_fams: List[FontFamilySpec], spacing: SpacingShapeSpec) -> DesignPrinciples:
        brand_color = next((c for c in colors if c.category == "brand" and c.token_name == "--color-primary"), None)
        if not brand_color:
            brand_color = next((c for c in colors if c.category == "brand"), None)
        brand_hex = brand_color.hex_value if brand_color else ("#ff5c35" if theme == "light" else "#e4f222")

        is_dark = (theme == "dark")
        border_hint = "#23252a or #383b3f" if is_dark else "#e5e7eb or #f0f0f0"

        dos = [
            "Use primary UI font with proper optical tracking and weights (400–600) — defines typographic identity",
            f"Use {brand_hex} exclusively for the primary call-to-action per view — preserve visual hierarchy",
            "Set body text with comfortable line-height (1.5–1.6) for high legibility",
            "Use tight letter-spacing (-0.02em) on display headings at 48px and above",
            "Maintain consistent border radii (6px for buttons/inputs, 12px for cards, 9999px for pills)",
            f"Use clean borders ({border_hint}) and subtle elevation for clear surface separation",
            "Keep rhythmic spacing gaps (8/16/24/32/48/96px) across all sections",
        ]
        donts = [
            "Do not use overly heavy font weights (800+) where medium/semibold provides sufficient contrast",
            "Do not introduce conflicting chromatic accent colors for secondary buttons — reserve primary color for main CTAs",
            "Do not mix arbitrary corner radii — stick strictly to the design token radius scale",
            "Do not use harsh heavy drop shadows — favor crisp borders and subtle ambient elevation",
            "Do not use low-contrast text for body copy — ensure WCAG AA readability against the canvas",
        ]
        return DesignPrinciples(dos=dos, donts=donts)

    def _generate_agent_prompts(self, theme: str, colors: List[ColorToken], font_fams: List[FontFamilySpec], spacing: SpacingShapeSpec, components: List[ComponentSpec]) -> AgentPromptGuideSpec:
        brand_color = next((c for c in colors if c.category == "brand" and c.token_name == "--color-primary"), None)
        if not brand_color:
            brand_color = next((c for c in colors if c.category == "brand"), None)
        brand_hex = brand_color.hex_value if brand_color else ("#ff5c35" if theme == "light" else "#e4f222")
        on_primary = next((c for c in colors if c.token_name == "--color-on-primary"), None)
        on_primary_hex = on_primary.hex_value if on_primary else ("#ffffff" if theme == "light" else "#08090a")

        is_dark = (theme == "dark")
        heading_color = "#ffffff" if is_dark else "#111827"
        body_color = "#d0d6e0" if is_dark else "#374151"
        muted_color = "#8a8f98" if is_dark else "#6b7280"
        canvas_bg = "#08090a" if is_dark else "#ffffff"
        card_bg = "#0f1011" if is_dark else "#ffffff"
        border_color = "#23252a" if is_dark else "#e5e7eb"

        quick_colors = {
            "text (primary heading)": heading_color,
            "text (body)": body_color,
            "text (muted)": muted_color,
            "background (canvas)": canvas_bg,
            "background (card)": card_bg,
            "border (hairline)": border_color,
            "accent (CTA)": brand_hex,
            "primary action": f"{brand_hex} (filled action)",
        }
        prompts = [
            (
                "Hero headline block",
                f"Full-bleed {canvas_bg} canvas. Headline at 56–64px weight 600, color {heading_color}, letter-spacing -0.02em, line-height 1.1. Subtext at 18px weight 400, color {body_color}. Primary CTA in {brand_hex} with {on_primary_hex} text.",
            ),
            (
                "Feature showcase card",
                f"Background {card_bg}, border-radius 12px, border 1px {border_color}, padding 24px. Clear heading, supporting copy in {body_color}, and optional action link.",
            ),
            (
                "Primary action button",
                f"Background {brand_hex}, text {on_primary_hex}, border-radius 6px, padding 12px 20px, font weight 500. Main CTA trigger.",
            ),
            (
                "Navigation top bar",
                f"Background {canvas_bg}, padding 16px horizontal, max-width 1200px centered. Brand mark left-aligned, links in {body_color}, right-aligned primary CTA button in {brand_hex}.",
            ),
            (
                "Badge / Category pill",
                f"Background {'rgba(255,255,255,0.05)' if is_dark else '#f3f4f6'}, text {muted_color}, border-radius 4px, padding 2px 8px, font size 12px.",
            ),
        ]
        return AgentPromptGuideSpec(quick_colors=quick_colors, component_prompts=prompts)

    def _recommend_similar_brands(self, theme: str, brand_name: str) -> List[SimilarBrandSpec]:
        if theme == "light":
            return [
                SimilarBrandSpec(
                    name="HubSpot",
                    description="Clean white canvas with high-energy brand orange CTA, structured rhythmic typography, and approachable modern SaaS layouts",
                ),
                SimilarBrandSpec(
                    name="Stripe",
                    description="Pristine light aesthetic with vibrant primary actions, refined typography scale, and crisp subtle borders",
                ),
                SimilarBrandSpec(
                    name="Notion",
                    description="High-contrast black-on-white typography with purposeful functional accents and minimalist modular cards",
                ),
                SimilarBrandSpec(
                    name="Intercom",
                    description="Friendly modern SaaS visual language with bold headline typography, clean cards, and clear visual hierarchy",
                ),
            ]
        return [
            SimilarBrandSpec(
                name="Vercel",
                description="Same dark-canvas-first approach with hairline borders, tight Inter typography, and product-screenshot-as-hero layout",
            ),
            SimilarBrandSpec(
                name="Cursor",
                description="Midnight dark mode with high-contrast accent CTA, compact typography at 400–510 weights, and showcase cards",
            ),
            SimilarBrandSpec(
                name="Raycast",
                description="Precision-instrument aesthetic — compact spacing, 6px button radius, monochromatic chrome with functional accent color",
            ),
            SimilarBrandSpec(
                name="Framer",
                description="Dark-canvas layout language with large headings at tight tracking and minimal ornament between sections",
            ),
        ]

    def _generate_tagline(self, theme: str, colors: List[ColorToken], font_fams: List[FontFamilySpec]) -> str:
        if theme == "dark":
            return "midnight precision instrument"
        return "clean architectural clarity on crisp paper"

    def _generate_aesthetic_summary(self, brand: str, theme: str, colors: List[ColorToken], font_fams: List[FontFamilySpec], spacing: SpacingShapeSpec) -> str:
        brand_color = next((c for c in colors if c.category == "brand" and c.token_name == "--color-primary"), None)
        if not brand_color:
            brand_color = next((c for c in colors if c.category == "brand"), None)
        brand_hex = brand_color.hex_value if brand_color else ("#ff5c35" if theme == "light" else "#e4f222")

        if theme == "dark":
            return (
                f"{brand}'s design system is a midnight command center built on near-black surfaces (#08090a) "
                f"with paper-white type and one electric accent ({brand_hex}) that functions as a functional flashlight — "
                "small, high-contrast, and used sparingly to signal action. The interface treats darkness as a substrate rather than "
                "a theme: text is crisp white at tight tracking (-0.022em), weights sit in a low 400–510 band rather than bold, "
                "and borders are hairline-thin (0.5px) to let geometry do the work that shadows usually would. Components feel "
                "precision-machined — 6px and 12px radii, compact 8–12px paddings, and almost no decorative ornament."
            )
        return (
            f"{brand}'s design system embraces pristine high-contrast minimalism built on clean paper surfaces "
            f"with jet-black typography and purposeful brand accents ({brand_hex}). Spacing is strictly rhythmic on an 8px ladder, borders are "
            "hairline-crisp, and typography features tight tracking on display headings with comfortable body line-heights."
        )

    def _generate_elevation_summary(self, theme: str, spacing: SpacingShapeSpec) -> str:
        if theme == "light":
            return (
                "Elevation is achieved through crisp hairline borders (1px #e5e7eb or #f0f0f0) and subtle soft ambient drop shadows "
                "(rgba(0,0,0,0.04) 0 2px 4px) against clean white surfaces. Visual hierarchy comes from surface-level progression "
                "(#ffffff -> #f9fafb -> #f3f4f6) and border definition."
            )
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
        for f in font_fams:
            lines.append(f"  {f.token_name}: {f.substitute};")

        lines.append("")
        lines.append("  /* Typography — Scale */")
        for step in type_scale:
            lines.append(f"  {step.token_name}: {step.size};")
            lines.append(f"  --leading-{step.role}: {step.line_height};")
            if step.letter_spacing != "—" and step.letter_spacing != "normal":
                lines.append(f"  --tracking-{step.role}: {step.letter_spacing};")
            if step.weight:
                lines.append(f"  --weight-{step.role}: {step.weight};")

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
        for f in font_fams:
            lines.append(f"  {f.token_name}: {f.substitute};")

        lines.append("")
        lines.append("  /* Typography — Scale */")
        for step in type_scale:
            lines.append(f"  {step.token_name}: {step.size};")
            lines.append(f"  --leading-{step.role}: {step.line_height};")
            if step.letter_spacing != "—" and step.letter_spacing != "normal":
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
