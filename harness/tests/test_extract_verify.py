"""harness/extract/verify.py の単体テスト。

- compute_similarity() が知覚的類似度スコアを計算できること。scorer/画像取得は
  フェイクを注入するだけでよく、実際のLLM再生成やブラウザレンダリングを必要としないこと。
- verify_against_threshold() が harness/config/design_extract.yaml の
  verify.similarity_threshold と比較して合否判定すること（閾値ちょうどの境界値を含む）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.extract.verify import (
    VerificationResult,
    compute_similarity,
    verify_against_threshold,
)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "design_extract.yaml"


def test_computes_perceptual_similarity_score() -> None:
    # scorer はフェイクで、実際の知覚的ハッシュ/SSIM実装やブラウザ・LLM呼び出しには依存しない。
    calls = []

    def fake_scorer(original, regenerated) -> float:
        calls.append((original, regenerated))
        return 0.87

    score = compute_similarity(
        "screenshots/original.png",
        "screenshots/regenerated.png",
        scorer=fake_scorer,
    )

    assert score == 0.87
    assert calls == [("screenshots/original.png", "screenshots/regenerated.png")]


def test_computes_similarity_via_image_providers_without_real_fetch() -> None:
    # 画像取得も差し替え可能：呼び出し可能な ImageProvider を渡せば、実ファイル/実ネットワーク
    # アクセスなしに（フェイクの返り値だけで）スコア計算のフローを検証できる。
    original_provider_called = []
    regenerated_provider_called = []

    def original_provider() -> bytes:
        original_provider_called.append(True)
        return b"original-bytes"

    def regenerated_provider() -> bytes:
        regenerated_provider_called.append(True)
        return b"regenerated-bytes"

    def fake_scorer(original, regenerated) -> float:
        assert original == b"original-bytes"
        assert regenerated == b"regenerated-bytes"
        return 0.5

    score = compute_similarity(original_provider, regenerated_provider, scorer=fake_scorer)

    assert score == 0.5
    assert original_provider_called == [True]
    assert regenerated_provider_called == [True]


def test_compute_similarity_rejects_out_of_range_score() -> None:
    def bad_scorer(original, regenerated) -> float:
        return 1.5

    with pytest.raises(ValueError):
        compute_similarity("a.png", "b.png", scorer=bad_scorer)


def test_passes_when_similarity_above_configured_threshold() -> None:
    # 実際の design_extract.yaml の verify.similarity_threshold (0.95) を使う。
    result = verify_against_threshold(0.99, config_path=CONFIG_PATH)

    assert isinstance(result, VerificationResult)
    assert result.threshold == 0.95
    assert result.passed is True


def test_fails_when_similarity_below_configured_threshold() -> None:
    result = verify_against_threshold(0.80, config_path=CONFIG_PATH)

    assert result.threshold == 0.95
    assert result.passed is False


def test_boundary_score_equal_to_threshold_passes() -> None:
    # 閾値ちょうどの境界値は「合格」として扱う（score >= threshold）。
    custom_config = {"verify": {"similarity_threshold": 0.9}}

    result = verify_against_threshold(0.9, config=custom_config)

    assert result.score == 0.9
    assert result.threshold == 0.9
    assert result.passed is True


def test_boundary_score_just_below_threshold_fails() -> None:
    custom_config = {"verify": {"similarity_threshold": 0.9}}

    result = verify_against_threshold(0.899999, config=custom_config)

    assert result.passed is False


def test_threshold_is_read_from_config_not_hardcoded() -> None:
    # 同じスコアでも、注入した設定次第で合否が変わる＝閾値がハードコードされていないことの確認。
    lenient_config = {"verify": {"similarity_threshold": 0.5}}
    strict_config = {"verify": {"similarity_threshold": 0.99}}

    lenient_result = verify_against_threshold(0.7, config=lenient_config)
    strict_result = verify_against_threshold(0.7, config=strict_config)

    assert lenient_result.passed is True
    assert strict_result.passed is False
