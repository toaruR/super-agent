"""harness/extract/refine.py の単体テスト。

- parse_instruction_to_delta() が自然言語の再調整指示をトークンJSONへの差分(delta)に
  変換できること。LLM呼び出しはフェイクの invoke_fn を注入するだけで検証でき、
  実ベンダー呼び出し(サブプロセス起動)を必要としないこと。
- merge_delta() が delta を既存トークンJSONにマージする際、該当カテゴリ・
  該当コンポーネントだけを更新し、無関係なカテゴリ/コンポーネント/プロパティには
  一切触れない(値が変わらないだけでなく、同一オブジェクトとして保持される)こと。
- apply_refinement() が上記2ステップに加えて render_prompt()(T6)による再生成まで
  一貫して行い、トークンJSONとプロンプトの整合性(再調整後は必ずトークンJSONから
  導出された新しいプロンプトが返る)を保つこと。
"""
from __future__ import annotations

from typing import Any, Dict

from harness.extract.generate import render_prompt
from harness.extract.refine import (
    RefinementResult,
    apply_refinement,
    merge_delta,
    parse_instruction_to_delta,
    spec_to_delta,
)


def _sample_tokens() -> Dict[str, Any]:
    return {
        "color": {
            "button": {
                "1280:button:1": {
                    "color": {"$type": "color", "$value": "#ffffff"},
                    "background-color": {"$type": "color", "$value": "#0055ff"},
                },
            },
            "card": {},
        },
        "typography": {
            "button": {
                "1280:button:1": {
                    "$type": "typography",
                    "$value": {"fontSize": "14px", "fontWeight": "600"},
                },
            },
            "card": {},
        },
        "spacing": {
            "button": {
                "1280:button:1": {
                    "padding": {"$type": "dimension", "$value": "8px 16px"},
                },
            },
            "card": {
                "1280:div:2": {
                    "padding": {"$type": "dimension", "$value": "16px"},
                },
            },
        },
        "radius": {
            "button": {
                "1280:button:1": {
                    "border-radius": {"$type": "dimension", "$value": "4px"},
                },
            },
            "card": {
                "1280:div:2": {
                    "border-radius": {"$type": "dimension", "$value": "8px"},
                },
            },
        },
        "shadow": {
            "button": {},
            "card": {
                "1280:div:2": {
                    "box-shadow": {"$type": "shadow", "$value": "0 1px 2px rgba(0,0,0,0.1)"},
                },
            },
        },
    }


def _fake_invoke_fn(result: Dict[str, Any]):
    calls = []

    def invoke_fn(decl, prompt, **kwargs):
        calls.append({"decl": decl, "prompt": prompt, "kwargs": kwargs})
        return {"result": result}

    invoke_fn.calls = calls
    return invoke_fn


def test_parses_natural_language_instruction_into_token_delta() -> None:
    tokens = _sample_tokens()
    invoke_fn = _fake_invoke_fn(
        {"category": "radius", "component_type": "button", "property": "border-radius", "value": "16px"}
    )

    delta = parse_instruction_to_delta(
        "ボタンの角丸をもっと大きくして", tokens, invoke_fn=invoke_fn, decl="fake-decl"
    )

    # 実ベンダー呼び出し(サブプロセス起動)は行われず、注入した invoke_fn だけが呼ばれる。
    assert len(invoke_fn.calls) == 1
    assert invoke_fn.calls[0]["decl"] == "fake-decl"
    assert "ボタンの角丸をもっと大きくして" in invoke_fn.calls[0]["prompt"]

    # 指示は radius カテゴリの button だけの border-radius への差分に解釈される。
    assert delta == {
        "radius": {
            "button": {
                "1280:button:1": {
                    "border-radius": {"$type": "dimension", "$value": "16px"},
                },
            },
        },
    }


def test_parse_instruction_returns_empty_delta_when_llm_output_is_unusable() -> None:
    tokens = _sample_tokens()
    # category が未知/欠落 -> _coerce_spec が空dictとして扱い、delta も空になる。
    invoke_fn = _fake_invoke_fn({"category": "not-a-real-category", "value": "16px"})

    delta = parse_instruction_to_delta("よくわからない指示", tokens, invoke_fn=invoke_fn, decl="fake-decl")

    assert delta == {}


def test_merges_delta_into_existing_tokens_without_touching_unrelated_categories() -> None:
    tokens = _sample_tokens()
    delta = spec_to_delta(
        {"category": "radius", "component_type": "button", "property": "border-radius", "value": "16px"},
        tokens,
    )

    merged = merge_delta(tokens, delta)

    # 該当カテゴリ(radius)・該当コンポーネント(button)の該当プロパティだけが更新される。
    assert merged["radius"]["button"]["1280:button:1"]["border-radius"] == {
        "$type": "dimension",
        "$value": "16px",
    }

    # 同カテゴリ内の無関係なコンポーネント(card)の値は変わらない。
    assert merged["radius"]["card"] == tokens["radius"]["card"]

    # 他のカテゴリ(color/typography/spacing/shadow)は一切変更されない(同一オブジェクトのまま)。
    for category in ("color", "typography", "spacing", "shadow"):
        assert merged[category] is tokens[category]
        assert merged[category] == tokens[category]

    # 元のトークンJSONは非破壊(マージ関数は新しいdictを返す)。
    assert tokens["radius"]["button"]["1280:button:1"]["border-radius"]["$value"] == "4px"


def test_merge_delta_partially_updates_typography_subproperties_only() -> None:
    tokens = _sample_tokens()
    delta = spec_to_delta(
        {"category": "typography", "component_type": "button", "property": "fontSize", "value": "18px"},
        tokens,
    )

    merged = merge_delta(tokens, delta)

    button_typo = merged["typography"]["button"]["1280:button:1"]
    assert button_typo["$value"]["fontSize"] == "18px"
    # 指定していないサブプロパティ(fontWeight)は保持される。
    assert button_typo["$value"]["fontWeight"] == "600"


def test_merge_delta_with_empty_delta_leaves_tokens_unchanged() -> None:
    tokens = _sample_tokens()

    merged = merge_delta(tokens, {})

    assert merged == tokens
    assert merged is not tokens


def test_regenerates_prompt_after_refinement() -> None:
    tokens = _sample_tokens()
    invoke_fn = _fake_invoke_fn(
        {"category": "radius", "component_type": "button", "property": "border-radius", "value": "16px"}
    )

    result = apply_refinement(
        "ボタンの角丸をもっと大きくして", tokens, invoke_fn=invoke_fn, decl="fake-decl"
    )

    assert isinstance(result, RefinementResult)

    # トークンJSON側は該当箇所が更新されている。
    assert result.tokens["radius"]["button"]["1280:button:1"]["border-radius"]["$value"] == "16px"
    assert result.delta == {
        "radius": {
            "button": {
                "1280:button:1": {
                    "border-radius": {"$type": "dimension", "$value": "16px"},
                },
            },
        },
    }

    # プロンプトは更新後のトークンJSONを render_prompt()(T6)にそのまま渡した結果と一致する
    # (トークンJSONとプロンプトの整合性 = プロンプトは常にトークンJSONからの導出物)。
    assert result.prompt == render_prompt(result.tokens)
    assert "16px" in result.prompt

    # 再調整前のプロンプトには存在しなかった新しい値が反映されている(再生成が確実に行われた)。
    original_prompt = render_prompt(tokens)
    assert "`border-radius` (dimension): 16px" not in original_prompt
    assert result.prompt != original_prompt

    # 元のトークンJSONは非破壊。
    assert tokens["radius"]["button"]["1280:button:1"]["border-radius"]["$value"] == "4px"


def test_apply_refinement_forwards_url_to_render_prompt() -> None:
    tokens = _sample_tokens()
    invoke_fn = _fake_invoke_fn(
        {"category": "radius", "component_type": "button", "property": "border-radius", "value": "16px"}
    )

    result = apply_refinement(
        "ボタンの角丸をもっと大きくして",
        tokens,
        invoke_fn=invoke_fn,
        decl="fake-decl",
        url="https://example.com",
    )

    assert "Source: https://example.com" in result.prompt
