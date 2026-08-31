"""harness/extract/analyze.py の単体テスト。

- analyze_components() が T2(fetch)の結果からボタン・カード・ナビゲーション・フォームの
  各コンポーネント種別を抽出し、それ以外の要素はクラッシュせず妥当に除外すること。
- ブレークポイント間のスタイル差分が比較可能な構造化データとして保持されること。
- 抽出結果にテキスト内容・画像URL・ロゴ等のコンテンツ資産が一切含まれないこと（著作権制約）。
"""
from __future__ import annotations

import dataclasses
import json

from harness.extract.analyze import (
    ComponentAnalysis,
    analyze_components,
)
from harness.extract.fetch import BreakpointCapture, PageFetchResult

# button:1, div(card):2, img:3, nav:4, form:5, input:6, div:7, span:8
# （インデックスは html.parser の handle_starttag 呼び出し順=文書順で決まる）
_MIXED_HTML = """
<div>
  <button class="btn btn-primary" type="button">Buy Now</button>
  <div class="card product-card">Card body <img src="secret-logo.png" alt="Logo" class="card-logo" /></div>
</div>
<nav class="site-nav">Home</nav>
<form>
  <input type="text" />
</form>
<div>plain</div>
<span>plain2</span>
"""


def _mixed_computed_styles() -> dict:
    return {
        "button:1": {
            "color": "#ffffff",
            "background-color": "#0055ff",
            "padding": "8px 16px",
            # content プロパティは擬似要素のテキストを保持し得るため抽出結果から除外されるべき。
            "content": '"Buy Now"',
        },
        "div:2": {
            "border-radius": "4px",
            "box-shadow": "0 1px 2px rgba(0,0,0,0.1)",
            # background-image は画像アセットのURLを含み得るため抽出結果から除外されるべき。
            "background-image": 'url("secret-logo.png")',
        },
        "img:3": {
            "width": "24px",
        },
        "nav:4": {
            "display": "flex",
            "gap": "16px",
        },
        "form:5": {
            "display": "grid",
            "gap": "8px",
        },
        "input:6": {
            "border": "1px solid #ccc",
        },
        "div:7": {"color": "#000000"},
        "span:8": {"color": "#000000"},
    }


def _mixed_fetch_result(url: str = "https://example.com/") -> PageFetchResult:
    return PageFetchResult(
        url=url,
        breakpoints=[
            BreakpointCapture(
                viewport_width=1280,
                outer_html=_MIXED_HTML,
                computed_styles=_mixed_computed_styles(),
            )
        ],
    )


def test_extracts_component_types_button_card_nav_form() -> None:
    result = analyze_components(_mixed_fetch_result())

    assert isinstance(result, ComponentAnalysis)
    bp = result.by_width(1280)
    assert bp is not None

    found_types = {c.component_type for c in bp.components}
    assert found_types == {"button", "card", "nav", "form"}
    # プレーンな div/span/img/input(text) は非対象タグ/属性のため抽出結果に含まれない。
    assert len(bp.components) == 4

    button = bp.by_key("button:1")
    assert button is not None
    assert button.component_type == "button"
    assert button.styles["color"] == "#ffffff"
    assert button.styles["padding"] == "8px 16px"

    card = bp.by_key("div:2")
    assert card is not None
    assert card.component_type == "card"
    assert card.styles["border-radius"] == "4px"

    nav = bp.by_key("nav:4")
    assert nav is not None
    assert nav.component_type == "nav"
    assert nav.styles["display"] == "flex"

    form = bp.by_key("form:5")
    assert form is not None
    assert form.component_type == "form"
    assert form.styles["display"] == "grid"

    # img/plain div/span/text-input は妥当に「非コンポーネント」として除外され、クラッシュしない。
    assert bp.by_key("img:3") is None
    assert bp.by_key("input:6") is None
    assert bp.by_key("div:7") is None
    assert bp.by_key("span:8") is None


def test_component_classification_does_not_crash_on_unrelated_elements() -> None:
    # コンポーネント判定に使う属性が欠けている/空である要素が混在してもクラッシュしないこと。
    html = """
    <div></div>
    <span class=""></span>
    <a></a>
    <ul><li></li></ul>
    <p role=""></p>
    """
    fetch_result = PageFetchResult(
        url="https://example.com/unrelated",
        breakpoints=[
            BreakpointCapture(viewport_width=1280, outer_html=html, computed_styles={})
        ],
    )

    result = analyze_components(fetch_result)
    bp = result.by_width(1280)
    assert bp is not None
    assert bp.components == []


def test_captures_responsive_style_differences_across_breakpoints() -> None:
    narrow_styles = {
        "button:0": {
            "padding": "4px 8px",
            "font-size": "12px",
            "color": "#ffffff",
        }
    }
    wide_styles = {
        "button:0": {
            "padding": "12px 24px",
            "font-size": "16px",
            "color": "#ffffff",
        }
    }
    html = '<button class="btn">Buy</button>'

    fetch_result = PageFetchResult(
        url="https://example.com/",
        breakpoints=[
            BreakpointCapture(viewport_width=375, outer_html=html, computed_styles=narrow_styles),
            BreakpointCapture(viewport_width=1280, outer_html=html, computed_styles=wide_styles),
        ],
    )

    result = analyze_components(fetch_result)

    # 各ブレークポイントの抽出結果は独立して保持される。
    assert result.by_width(375).by_key("button:0").styles["padding"] == "4px 8px"
    assert result.by_width(1280).by_key("button:0").styles["padding"] == "12px 24px"

    diffs = result.diffs_for_key("button:0")
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.component_type == "button"
    assert diff.from_width == 375
    assert diff.to_width == 1280
    # 変化したプロパティだけが差分として構造化データに残る。
    assert diff.changed_properties == {
        "padding": ("4px 8px", "12px 24px"),
        "font-size": ("12px", "16px"),
    }
    # 変化していないプロパティ(color)は差分に含まれない。
    assert "color" not in diff.changed_properties
    assert diff.has_changes is True


def test_no_diff_reported_when_styles_are_identical_across_breakpoints() -> None:
    styles = {"nav:0": {"display": "flex", "gap": "8px"}}
    html = '<nav class="site-nav"></nav>'

    fetch_result = PageFetchResult(
        url="https://example.com/",
        breakpoints=[
            BreakpointCapture(viewport_width=375, outer_html=html, computed_styles=styles),
            BreakpointCapture(viewport_width=1280, outer_html=html, computed_styles=styles),
        ],
    )

    result = analyze_components(fetch_result)
    assert result.diffs_for_key("nav:0") == []


def test_excludes_text_and_image_content_from_extracted_patterns() -> None:
    result = analyze_components(_mixed_fetch_result())
    bp = result.by_width(1280)

    serialized = json.dumps(dataclasses.asdict(result), ensure_ascii=False)

    # 元ページのテキスト内容(innerText)が一切含まれないこと。
    assert "Buy Now" not in serialized
    assert "Card body" not in serialized
    assert "Home" not in serialized

    # 画像URL・ロゴ等のコンテンツ資産が一切含まれないこと。
    assert "secret-logo.png" not in serialized
    assert "url(" not in serialized

    # 擬似要素のテキストを保持し得る content プロパティ自体も除外されていること。
    button = bp.by_key("button:1")
    assert "content" not in button.styles

    # background-image のように画像URLを含み得るプロパティも除外されていること。
    card = bp.by_key("div:2")
    assert "background-image" not in card.styles

    # img要素自体はロゴ等の画像資産を保持するため、そもそもコンポーネントとして
    # 抽出結果に登場しないこと。
    assert all(c.tag != "img" for c in bp.components)


# =====================================================================
# サイト固有の特徴抽出（docs/plans/design-extract-site-specific-fidelity.md）
# =====================================================================
from harness.extract.analyze import (  # noqa: E402
    DesignSystemExtractor,
    _infer_base_unit,
    _nearest_rank_percentile,
)


def _rich_styles(brand: str, btn_pad: str, btn_radius: str, card_pad: str, card_radius: str, gap: str) -> dict:
    styles = {}
    n = 0
    for _ in range(6):
        n += 1
        styles[f"button.btn-primary:{n}"] = {
            "background-color": brand,
            "color": "#ffffff",
            "padding": btn_pad,
            "border-radius": btn_radius,
            "font-size": "16px",
            "font-weight": "600",
            "line-height": "1.4",
        }
    for _ in range(3):
        n += 1
        styles[f"a.btn-outline:{n}"] = {
            "background-color": "transparent",
            "color": brand,
            "border": f"1px solid {brand}",
            "padding": btn_pad,
            "border-radius": btn_radius,
            "font-size": "16px",
            "font-weight": "600",
        }
    for _ in range(3):
        n += 1
        styles[f"button.btn-text:{n}"] = {
            "background-color": "transparent",
            "color": "#666666",
            "padding": btn_pad,
            "font-size": "14px",
            "font-weight": "500",
        }
    for _ in range(10):
        n += 1
        styles[f"div.card:{n}"] = {
            "padding": card_pad,
            "border-radius": card_radius,
            "box-shadow": "0 1px 3px rgba(0,0,0,0.1)",
            "gap": gap,
        }
    for hexv in ("#ffffff", "#f5f5f5", "#eeeeee", "#dddddd", "#999999", "#555555", "#222222", "#111111"):
        n += 1
        styles[f"div.neutral:{n}"] = {"color": hexv}
    return styles


def _rich_fetch_result(url: str, **kwargs) -> PageFetchResult:
    return PageFetchResult(
        url=url,
        breakpoints=[
            BreakpointCapture(viewport_width=1280, outer_html="<div></div>", computed_styles=_rich_styles(**kwargs))
        ],
        metadata={"title": "Test Page Title"},
    )


def test_nearest_rank_percentile_returns_only_observed_values() -> None:
    values = [2.0, 4.0, 6.0, 8.0, 10.0]
    # 分位点は実測値の中からのみ選ばれ、線形補間による「架空の中間値」は生成されない。
    for p in (0.0, 0.25, 0.5, 0.85, 1.0):
        assert _nearest_rank_percentile(values, p) in values
    # 同一入力・同一pに対しては常に同じ値を返す(決定的)。
    assert _nearest_rank_percentile(values, 0.5) == _nearest_rank_percentile(values, 0.5)


def test_infer_base_unit_detects_8px_grid() -> None:
    assert _infer_base_unit([8.0, 16.0, 24.0, 32.0, 40.0]) == 8


def test_infer_base_unit_falls_back_to_4px_with_sparse_data() -> None:
    assert _infer_base_unit([7.0]) == 4


def test_extract_spacing_shapes_reflects_real_measurements() -> None:
    site_a = DesignSystemExtractor(
        _rich_fetch_result(
            "https://site-a.example/",
            brand="#ff5c35",
            btn_pad="12px 24px",
            btn_radius="6px",
            card_pad="24px",
            card_radius="12px",
            gap="16px",
        )
    ).extract()
    site_b = DesignSystemExtractor(
        _rich_fetch_result(
            "https://site-b.example/",
            brand="#0066ff",
            btn_pad="10px 20px",
            btn_radius="24px",
            card_pad="32px",
            card_radius="4px",
            gap="24px",
        )
    ).extract()

    # 実測の角丸・余白が異なれば、抽出される spacing_shapes も異なる(単一の固定テンプレートに
    # 収束しない)。
    assert site_a.spacing_shapes.border_radius != site_b.spacing_shapes.border_radius
    assert site_a.spacing_shapes.card_padding != site_b.spacing_shapes.card_padding

    # site_b は 24px 単位のカード角丸(4px)を持つため、cards スロットにその値が反映される。
    cards_radius = dict((n, v) for n, v, _ in site_b.spacing_shapes.border_radius)["cards"]
    assert cards_radius in ("4px", "6px", "24px")  # 実測分位点由来の値であること


def test_extract_spacing_shapes_falls_back_when_data_is_sparse() -> None:
    fetch_result = PageFetchResult(
        url="https://sparse.example/",
        breakpoints=[BreakpointCapture(viewport_width=1280, outer_html="<div></div>", computed_styles={})],
    )
    ds = DesignSystemExtractor(fetch_result).extract()
    # 実測データが皆無の場合は既存の固定既定値にフォールバックする。
    assert ds.spacing_shapes.page_max_width == "1200px"
    assert ds.spacing_shapes.section_gap == "80px"
    assert ds.spacing_shapes.base_unit == "4px"


def test_extract_colors_resamples_neutrals_when_data_is_rich() -> None:
    ds = DesignSystemExtractor(
        _rich_fetch_result(
            "https://site-a.example/",
            brand="#ff5c35",
            btn_pad="12px 24px",
            btn_radius="6px",
            card_pad="24px",
            card_radius="12px",
            gap="16px",
        )
    ).extract()
    neutrals = [c for c in ds.colors if c.category == "neutral"]
    assert len(neutrals) == 10
    # 実測ニュートラルからリサンプルされた値は、synthetic fixtureで与えた実測hexのいずれかである。
    observed = {"#ffffff", "#f5f5f5", "#eeeeee", "#dddddd", "#999999", "#555555", "#222222", "#111111"}
    assert any(c.hex_value in observed for c in neutrals)


def test_extract_components_button_variant_clustering() -> None:
    ds = DesignSystemExtractor(
        _rich_fetch_result(
            "https://site-a.example/",
            brand="#ff5c35",
            btn_pad="12px 24px",
            btn_radius="6px",
            card_pad="24px",
            card_radius="12px",
            gap="16px",
        )
    ).extract()
    buttons = [c for c in ds.components if c.component_type == "button"]
    # filled(6件)/outline(3件)/text(3件)が全て閾値(2件)を超えるため、
    # Primary/Secondary/Ghostの3バリアントに収束する(上限3)。
    assert len(buttons) == 3
    assert {b.variant_key for b in buttons} == {"primary", "secondary", "ghost"}
    primary = next(b for b in buttons if b.variant_key == "primary")
    assert primary.properties["background"] == "#ff5c35"
    assert primary.semantic_role == "primary-cta"


def test_extract_components_omits_ghost_variant_when_data_is_scarce() -> None:
    styles = {}
    for i in range(6):
        styles[f"button.btn:{i}"] = {
            "background-color": "#00cc66",
            "color": "#ffffff",
            "padding": "14px 28px",
            "border-radius": "4px",
            "font-size": "18px",
            "font-weight": "700",
        }
    fetch_result = PageFetchResult(
        url="https://site-c.example/",
        breakpoints=[BreakpointCapture(viewport_width=1280, outer_html="<div></div>", computed_styles=styles)],
    )
    ds = DesignSystemExtractor(fetch_result).extract()
    buttons = [c for c in ds.components if c.component_type == "button"]
    # outline/textの実測データが閾値未満のため、Ghostは生成されずPrimary/Secondaryのみになる。
    assert {b.variant_key for b in buttons} == {"primary", "secondary"}


def test_extract_components_card_surface_uses_correct_radius_not_padding() -> None:
    ds = DesignSystemExtractor(
        _rich_fetch_result(
            "https://site-a.example/",
            brand="#ff5c35",
            btn_pad="12px 24px",
            btn_radius="6px",
            card_pad="32px",
            card_radius="4px",
            gap="16px",
        )
    ).extract()
    card = next(c for c in ds.components if c.component_type == "card")
    # 修正前は border-radius に card_padding(32px)が誤って使われるバグがあった。
    assert card.properties["border_radius"] != card.properties["padding"]
    assert card.properties["padding"] == ds.spacing_shapes.card_padding


def test_design_system_extraction_is_deterministic() -> None:
    fetch_result = _rich_fetch_result(
        "https://site-a.example/",
        brand="#ff5c35",
        btn_pad="12px 24px",
        btn_radius="6px",
        card_pad="24px",
        card_radius="12px",
        gap="16px",
    )
    first = DesignSystemExtractor(fetch_result).extract()
    second = DesignSystemExtractor(fetch_result).extract()
    assert dataclasses.asdict(first) == dataclasses.asdict(second)
