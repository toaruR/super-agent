"""design-extract パイプラインの Analyze 段階（再利用可能なUIコンポーネント単位のスタイル抽出）。

- analyze_components(): harness.extract.fetch.PageFetchResult（breakpointごとの
  outerHTML相当のDOM/computed style）を入力に、ボタン・カード・ナビゲーション・
  フォームといった再利用可能なUIコンポーネント単位でスタイルパターンを抽出する。
  ブレークポイント間で同一要素（tag:index）に対応するスタイル差分も構造化して保持する。
- 著作権制約: 抽出結果には innerText 等のテキスト内容、画像URL、ロゴ等のコンテンツ資産を
  一切含めない。要素の分類・照合には class/role/type といった構造的な属性のみを用い、
  href/src/alt/title やテキストノードはそもそもモデルに取り込まない。computed style の
  値のうち url(...) を含むもの（background-image等）や content プロパティ（擬似要素の
  テキストを保持し得る）は抽出結果から除外する。

DOM の解析には外部依存を避けるため標準ライブラリの html.parser を用いる。要素の走査順は
HTMLParser の handle_starttag 呼び出し順（文書順）で、これは harness.extract.fetch の
PlaywrightBrowserDriver が computed_styles のキーを "タグ名:document順インデックス" で
生成する際の querySelectorAll('*').forEach の走査順と一致するため、同じ規約でスタイルを
突き合わせられる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Sequence, Set, Tuple

from harness.extract.fetch import BreakpointCapture, PageFetchResult

# 抽出対象とするUIコンポーネント種別。
COMPONENT_TYPES: Tuple[str, ...] = ("button", "card", "nav", "form")

# 画像・メディア等、コンテンツ資産を保持しうるタグはそもそもコンポーネントとして
# 分類しない（class名にたまたま "card" 等が含まれていても除外する）。
_NON_COMPONENT_TAGS: Set[str] = {
    "img", "svg", "picture", "video", "audio", "source", "canvas",
    "script", "style", "noscript", "meta", "link",
}

# nav 判定に用いるクラス名ヒント。
_NAV_CLASS_HINTS: Set[str] = {"nav", "navbar", "navigation", "menu", "menubar"}

# computed style のうち、テキスト内容を保持しうるため常に除外するプロパティ。
_CONTENT_LEAK_STYLE_PROPERTIES: Set[str] = {"content"}


class _ElementCollector(HTMLParser):
    """outerHTML相当の文字列から、文書順の (index, tag, attrs) 列を収集する。

    テキストノード（handle_data）は一切保持しないため、innerText がこのクラスの
    出力に混入することはない。
    """

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
    """タグ名・構造的属性(class/role/type)だけからコンポーネント種別を判定する。

    テキスト内容やhref/src等の資産系属性は一切参照しない。判定できない場合は
    None を返し、呼び出し側で妥当に除外させる（例外は送出しない）。
    """
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
        tag == "button"
        or role == "button"
        or (tag == "input" and input_type in ("button", "submit", "reset"))
        or any("btn" in c or "button" in c for c in classes)
    ):
        return "button"
    if any("card" in c for c in classes):
        return "card"
    return None


def _sanitize_styles(styles: Dict[str, str]) -> Dict[str, str]:
    """computed style から、テキスト内容/画像URLを保持しうるプロパティを取り除く。"""
    sanitized: Dict[str, str] = {}
    for prop, value in styles.items():
        if prop.lower() in _CONTENT_LEAK_STYLE_PROPERTIES:
            continue
        if "url(" in (value or "").lower():
            continue
        sanitized[prop] = value
    return sanitized


@dataclass(frozen=True)
class ComponentStyle:
    """1コンポーネント×1ブレークポイントにおける、著作権制約を満たすよう浄化済みのスタイル情報。"""

    key: str  # "tag:index"。ブレークポイント間で同一要素を対応付けるためのキー。
    component_type: str
    tag: str
    classes: Tuple[str, ...] = field(default_factory=tuple)
    styles: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BreakpointComponents:
    """1ブレークポイントにおける、抽出済みコンポーネントの集合。"""

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
    """隣接するブレークポイント間で、同一コンポーネントのスタイルがどう変化したか。"""

    key: str
    component_type: str
    from_width: int
    to_width: int
    changed_properties: Dict[str, Tuple[Optional[str], Optional[str]]] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_properties)


@dataclass(frozen=True)
class ComponentAnalysis:
    """analyze_components() の戻り値。ブレークポイントごとの抽出結果と、その差分をまとめる。"""

    url: str
    breakpoints: List[BreakpointComponents] = field(default_factory=list)
    responsive_diffs: List[ResponsiveStyleDiff] = field(default_factory=list)

    def by_width(self, viewport_width: int) -> Optional[BreakpointComponents]:
        for bp in self.breakpoints:
            if bp.viewport_width == viewport_width:
                return bp
        return None

    def diffs_for_key(self, key: str) -> List[ResponsiveStyleDiff]:
        return [d for d in self.responsive_diffs if d.key == key]


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
    """幅の昇順に隣接するブレークポイント同士で、同一キー(tag:index)かつ同一種別の
    コンポーネントについてスタイル差分を計算する。片方にしか存在しない要素は比較しようが
    ないためスキップする（クラッシュさせない）。
    """
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
    """T2(fetch)の結果から、再利用可能なUIコンポーネント単位でスタイルパターンを抽出する。

    - component_types に含まれる種別（既定: button/card/nav/form）だけを抽出対象とする。
      該当しない要素（プレーンなdiv/span/画像/テキストノード等）は例外を送出せず
      単に結果から除外する。
    - 各ブレークポイントの抽出結果は互いに独立して保持しつつ、同一要素(tag:index)を
      またいだスタイル差分（レスポンシブ差分）も responsive_diffs として構造化して返す。
    - 戻り値にはテキスト内容・画像URL・ロゴ等のコンテンツ資産を一切含めない
      （_classify_component/_sanitize_styles/_ElementCollector 参照）。
    """
    allowed_types = set(component_types)
    breakpoint_components = [
        BreakpointComponents(
            viewport_width=capture.viewport_width,
            components=_extract_components_for_breakpoint(capture, allowed_types),
        )
        for capture in fetch_result.breakpoints
    ]

    return ComponentAnalysis(
        url=fetch_result.url,
        breakpoints=breakpoint_components,
        responsive_diffs=_compute_responsive_diffs(breakpoint_components),
    )
