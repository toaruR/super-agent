"""design-extract パイプラインの Refine 段階（自然言語による再調整）。

- 生成済みのデザインプロンプト（T6: harness.extract.generate.render_prompt()の出力）が
  完成した後、「ボタンの角丸をもっと大きく」のような自然言語の再調整指示を受け取り、
  T4(tokens)のトークンJSON(harness.extract.tokens.build_design_tokens()の出力形式)の
  該当カテゴリ・該当コンポーネントだけを更新し、render_prompt()で再生成した新しい
  プロンプトを返す apply_refinement() を提供する。
- 自然言語→更新スペック(RefinementSpec)への解釈はLLM呼び出し
  (harness.core.invoke.invoke)で行うが、本モジュール自体は「vendor宣言(decl)＋プロンプト
  文字列を受け取り、resultを含むdictを返す」という契約(InvokeFn)しか知らない。
  harness.extract.verify のSimilarityScorer/ImageProviderと同じ設計思想で、
  invoke_fn を注入できるようにし、ユニットテストでは実ベンダー呼び出し
  (サブプロセス起動)なしにフェイクの invoke_fn を渡すだけで検証できる。
- LLMには「どのカテゴリ/コンポーネント種別/プロパティを、どんな値に変えるか」という
  スペック(RefinementSpec)だけを出力させ、実際のトークンJSONへの差分(delta)構築・
  マージは本モジュールのコードで決定的に行う。これにより、LLMの出力ゆれによって
  無関係なトークンが破壊されるリスクを避けられる（マージは既存トークンJSON中で
  スペックに一致したキーだけを書き換え、他は一切変更しない）。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from harness.core.invoke import invoke, load_vendors
from harness.extract.generate import render_prompt
from harness.extract.tokens import TOKEN_CATEGORIES, TYPOGRAPHY

# invoke_fn の契約: decl(vendor宣言) + prompt を受け取り、
# 少なくとも {"result": <structured output>} を含む dict を返す呼び出し可能オブジェクト。
# harness.core.invoke.invoke がこの契約を満たすデフォルト実装。テストではこの型に
# 合致するフェイクを渡すだけで、実ベンダー呼び出し(サブプロセス起動)を避けられる。
InvokeFn = Callable[..., Dict[str, Any]]

# LLMに要求する構造化出力(自然言語→更新スペックの解釈結果)のJSON Schema。
# トークンJSONそのものではなく「どのカテゴリ/コンポーネント種別/プロパティを、
# どんな値にするか」という最小限のスペックに限定することで、LLMの表現ゆれが
# 実際のマージ処理(spec_to_delta/merge_delta、共に純粋関数)に波及しないようにする。
REFINEMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": list(TOKEN_CATEGORIES)},
        "component_type": {"type": ["string", "null"]},
        "property": {"type": ["string", "null"]},
        "value": {"type": "string"},
    },
    "required": ["category", "value"],
}

_REFINE_PROMPT = """あなたはデザイントークンの再調整アシスタントです。
自然言語の再調整指示を読み、変更対象のデザイントークンを特定してください。

ルール:
- category は color/typography/spacing/radius/shadow のいずれか一つだけを選ぶこと。
  指示が複数カテゴリにまたがる場合は、最も主要なものを一つ選ぶこと。
- component_type は button/card/nav/form 等、指示が特定コンポーネントに言及していれば
  その種別を、全コンポーネント種別に適用すべきなら null を返すこと。
- property は border-radius のような具体的なCSSプロパティ名(typographyの場合は
  fontSize 等のサブプロパティ名)を、対象を絞れない場合は null を返すこと。
- value には変更後の具体的な値(例: "16px")を文字列で入れること。

出力は必ず以下の形の JSON のみとすること（説明文や前置きは書かない）:
{{"category": "...", "component_type": null, "property": null, "value": "..."}}

再調整指示: {instruction}

現在のトークンJSON(参考。既知のカテゴリ/コンポーネント種別のみ抜粋):
{tokens_summary}
"""


def _tokens_summary(tokens: Dict[str, Any]) -> str:
    """LLMに渡すトークンJSONの要約。カテゴリ→コンポーネント種別のキー一覧のみを
    渡し、実際の値(色コード等)はプロンプトを簡潔にするため省略する。"""
    summary: Dict[str, Any] = {}
    for category, group in (tokens or {}).items():
        if isinstance(group, dict):
            summary[category] = sorted(group.keys())
    return json.dumps(summary, ensure_ascii=False)


def _build_prompt(instruction: str, tokens: Dict[str, Any]) -> str:
    return _REFINE_PROMPT.format(instruction=instruction, tokens_summary=_tokens_summary(tokens))


def _coerce_spec(result: object) -> Dict[str, Any]:
    """invoke()のresultは構造化JSON(dict)を期待するが、ベンダーが素の文字列で
    返す場合もあるため、architect._coerce_parsed と同様にJSON復元を試みる。
    復元不能・categoryが未知/欠落な場合は空dictを返し、呼び出し側で「変更なし」
    として扱えるようにする(防御的実装)。"""
    parsed: Any = result
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (json.JSONDecodeError, ValueError):
            return {}
    if not isinstance(parsed, dict):
        return {}
    category = parsed.get("category")
    value = parsed.get("value")
    if category not in TOKEN_CATEGORIES or value is None:
        return {}
    return {
        "category": category,
        "component_type": parsed.get("component_type") or None,
        "property": parsed.get("property") or None,
        "value": value,
    }


def _default_config_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config"


def _resolve_decl(vendor: Optional[str], config_dir: Optional[Path]) -> Any:
    vendors = load_vendors(config_dir or _default_config_dir())
    vendor_name = vendor or "claude"
    return vendors[vendor_name]


def _is_leaf_token(node: Any) -> bool:
    return isinstance(node, dict) and set(node.keys()) == {"$type", "$value"}


def spec_to_delta(spec: Dict[str, Any], tokens: Dict[str, Any]) -> Dict[str, Any]:
    """RefinementSpec(category/component_type/property/value)を、既存のトークンJSON
    (tokens)と突き合わせて、実際に更新すべきエントリだけを含む差分(delta)に変換する。

    - delta は tokens と同じネスト形状(category -> component_type -> composite_key ->
      ...)を持つが、spec に一致したエントリだけを含む(それ以外のカテゴリ/コンポーネント/
      composite_key/プロパティは delta に一切現れない)。
    - spec が既存トークンJSONのどの部分とも一致しない(存在しないカテゴリを指す等)場合は
      空dictを返す。呼び出し側(merge_delta)にとって空deltaは「変更なし」を意味する。
    """
    if not spec:
        return {}
    category = spec.get("category")
    if category not in (tokens or {}):
        return {}
    category_group = tokens[category]
    if not isinstance(category_group, dict):
        return {}

    wanted_component = spec.get("component_type")
    wanted_property = spec.get("property")
    new_value = spec.get("value")

    delta_category: Dict[str, Any] = {}
    for component_type, composites in category_group.items():
        if wanted_component and component_type != wanted_component:
            continue
        if not isinstance(composites, dict):
            continue

        delta_composites: Dict[str, Any] = {}
        for composite_key, entry in composites.items():
            if _is_leaf_token(entry) and category == TYPOGRAPHY:
                sub_values = entry.get("$value")
                if not isinstance(sub_values, dict):
                    continue
                if wanted_property and wanted_property not in sub_values:
                    continue
                sub_keys = [wanted_property] if wanted_property else list(sub_values.keys())
                new_sub_values = {k: new_value for k in sub_keys}
                delta_composites[composite_key] = {"$type": entry["$type"], "$value": new_sub_values}
            elif isinstance(entry, dict):
                delta_props: Dict[str, Any] = {}
                for prop, token in entry.items():
                    if wanted_property and prop != wanted_property:
                        continue
                    if not _is_leaf_token(token):
                        continue
                    delta_props[prop] = {"$type": token["$type"], "$value": new_value}
                if delta_props:
                    delta_composites[composite_key] = delta_props

        if delta_composites:
            delta_category[component_type] = delta_composites

    if not delta_category:
        return {}
    return {category: delta_category}


def _deep_merge(base: Any, delta: Any) -> Any:
    """base に delta を再帰的に重ね合わせた新しいオブジェクトを返す(base/deltaは非破壊)。

    両方が dict のときだけ再帰し、それ以外(文字列等のスカラー値)は delta の値で
    置き換える。$value がサブプロパティの辞書であるtypographyトークンも、この再帰に
    よって「指定されたサブプロパティだけ上書きし、他は保持」という部分マージになる。
    delta に現れないキー(無関係なカテゴリ/コンポーネント/トークン)は base 側の値を
    そのまま保持するため、無関係なトークンが書き換わることはない。
    """
    if isinstance(base, dict) and isinstance(delta, dict):
        merged = dict(base)
        for key, value in delta.items():
            merged[key] = _deep_merge(merged.get(key), value) if key in merged else copy.deepcopy(value)
        return merged
    return copy.deepcopy(delta)


def merge_delta(tokens: Dict[str, Any], delta: Dict[str, Any]) -> Dict[str, Any]:
    """delta(spec_to_delta()の出力)を既存のトークンJSON(tokens)にマージした新しい
    トークンJSONを返す。tokens/delta のどちらも変更しない(常に新しいdictを返す)。

    delta に含まれないカテゴリ・コンポーネント種別・composite_key・プロパティは
    tokens の値がそのまま保持される(触れない)。delta が空なら tokens と等価な
    新しいオブジェクトを返す。
    """
    return _deep_merge(tokens or {}, delta or {})


def parse_instruction_to_delta(
    instruction: str,
    tokens: Dict[str, Any],
    *,
    invoke_fn: InvokeFn = invoke,
    decl: Any = None,
    vendor: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    config_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """自然言語の再調整指示(instruction)を、既存のトークンJSON(tokens)に対する差分
    (delta)に変換する。

    自然言語の解釈自体は invoke_fn(既定は harness.core.invoke.invoke)によるLLM呼び出しに
    委譲し、LLMには REFINEMENT_SCHEMA に従ったスペック(category/component_type/
    property/value)だけを出力させる。実際にどのトークンをどう書き換えるか(delta構築)は
    spec_to_delta() が既存トークンJSONと突き合わせて決定的に行うため、LLMの出力ゆれが
    無関係なトークンを破壊することはない。

    invoke_fn を差し替えれば実ベンダー呼び出し(サブプロセス起動)なしにユニットテスト
    できる(decl はフェイクの invoke_fn からは参照されないため、テストでは任意のダミー
    オブジェクトを渡してよい)。
    """
    resolved_decl = decl if decl is not None else _resolve_decl(vendor, config_dir)
    prompt = _build_prompt(instruction, tokens)
    res = invoke_fn(resolved_decl, prompt, schema=REFINEMENT_SCHEMA, model=model, effort=effort, role="refine")
    result = res.get("result") if isinstance(res, dict) else None
    spec = _coerce_spec(result)
    return spec_to_delta(spec, tokens)


@dataclass(frozen=True)
class RefinementResult:
    """apply_refinement() の結果。更新後のトークンJSON・その差分・再生成されたプロンプトを
    常にペアで保持する(設計方針: プロンプト本文とトークンJSONは常にペアで保存する)。"""

    tokens: Dict[str, Any]
    delta: Dict[str, Any]
    prompt: str


def apply_refinement(
    instruction: str,
    tokens: Dict[str, Any],
    *,
    url: Optional[str] = None,
    invoke_fn: InvokeFn = invoke,
    decl: Any = None,
    vendor: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    config_dir: Optional[Path] = None,
) -> RefinementResult:
    """生成済みのデザインプロンプト完成後に自然言語の再調整指示を適用する。

    1. instruction を parse_instruction_to_delta() でトークンJSONへの差分(delta)に解釈する
       (LLM呼び出しは invoke_fn 経由。既定は harness.core.invoke.invoke)。
    2. delta を merge_delta() で既存トークンJSON(tokens)にマージし、該当カテゴリ・
       該当コンポーネントだけを更新した新しいトークンJSONを得る(無関係なトークンは
       一切変更しない)。
    3. 更新後のトークンJSONを harness.extract.generate.render_prompt()(T6)にそのまま
       渡し、再生成した新しいデザインプロンプトを得る。これにより、トークンJSONと
       プロンプトの整合性(プロンプトは常にトークンJSONからの機械的な導出物である)が
       再調整後も保たれる。

    戻り値の RefinementResult は更新後のトークンJSON・delta・新しいプロンプトを常に
    セットで返す(呼び出し側が両者を個別に永続化しても不整合が起きないようにするため)。
    """
    delta = parse_instruction_to_delta(
        instruction,
        tokens,
        invoke_fn=invoke_fn,
        decl=decl,
        vendor=vendor,
        model=model,
        effort=effort,
        config_dir=config_dir,
    )
    new_tokens = merge_delta(tokens, delta)
    new_prompt = render_prompt(new_tokens, url=url)
    return RefinementResult(tokens=new_tokens, delta=delta, prompt=new_prompt)
