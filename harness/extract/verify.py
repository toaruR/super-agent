"""再生成UIと元サイトの視覚的類似度検証（design-extract パイプラインの Verify 段階）。

- compute_similarity: 元サイトのスクリーンショットと、生成デザインプロンプトでLLMに
  再生成させたUIのスクリーンショットとの知覚的類似度スコア(0.0〜1.0)を計算する。
  画像そのもの、または画像を取得する ImageProvider（呼び出し可能オブジェクト）のどちらも
  受け取れるようにし、実際のブラウザレンダリングやLLM呼び出しをこのモジュールに
  持ち込まずに済むようにする。
- verify_against_threshold: compute_similarity の結果を harness/config/design_extract.yaml の
  verify.similarity_threshold と比較し、合否を判定する。

類似度の算出方法（perceptual hash か SSIM か等）や画像の取得方法は、呼び出し側が
SimilarityScorer / ImageProvider を注入することで自由に差し替えられる。これにより、
本モジュール自体は外部依存（Playwright, LLM API 等）を一切持たず、ユニットテストでは
フェイクの scorer/provider を渡すだけで検証できる。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

from harness.extract.robots import load_design_extract_config

# スクリーンショットの実体表現。ファイルパス／生バイト列のいずれでも受け取れるようにし、
# 具体的な取得手段（ファイル保存かインメモリか）を本モジュールに強制しない。
ImageLike = Union[str, Path, bytes]

# 画像取得の差し替え可能インターフェース。例えばPlaywrightでのレンダリング結果や
# LLM再生成結果のキャプチャ処理を、呼び出し可能オブジェクトとして注入する。
ImageProvider = Callable[[], ImageLike]

# 類似度算出の差し替え可能インターフェース。perceptual hash・SSIM等、実装を問わず
# 「2枚の画像を受け取り0.0〜1.0のスコアを返す」契約だけを共有する。
SimilarityScorer = Callable[[ImageLike, ImageLike], float]


def _resolve_image(source: Union[ImageLike, ImageProvider]) -> ImageLike:
    """source がプロバイダ（呼び出し可能）ならそれを呼び出して画像を取得し、そうでなければそのまま返す。"""
    if callable(source):
        return source()
    return source


def compute_similarity(
    original: Union[ImageLike, ImageProvider],
    regenerated: Union[ImageLike, ImageProvider],
    *,
    scorer: SimilarityScorer,
) -> float:
    """元サイトと再生成UIのスクリーンショットから知覚的類似度スコアを計算する。

    original/regenerated は画像そのものでも ImageProvider（引数なしで画像を返す
    呼び出し可能オブジェクト）でもよい。実際の類似度算出ロジックは scorer に委譲するため、
    本関数自体は画像フォーマットやアルゴリズムの詳細を知らない。
    """
    original_image = _resolve_image(original)
    regenerated_image = _resolve_image(regenerated)

    score = scorer(original_image, regenerated_image)
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"similarity score must be within [0.0, 1.0], got {score!r}")
    return score


@dataclass(frozen=True)
class VerificationResult:
    """verify_against_threshold() の判定結果。"""

    score: float
    threshold: float
    passed: bool


def verify_against_threshold(
    score: float,
    *,
    config: Optional[dict] = None,
    config_path: Optional[Path] = None,
) -> VerificationResult:
    """類似度スコアを design_extract.yaml の verify.similarity_threshold と比較して合否判定する。

    閾値はハードコードせず、config（テスト等で直接注入する場合）または config_path
    （省略時はデフォルトの harness/config/design_extract.yaml）から読み込む。
    スコアが閾値と厳密に等しい場合は合格とする（閾値は「この値以上なら許容する」下限として扱う）。
    """
    cfg = config if config is not None else load_design_extract_config(config_path)
    threshold = float(cfg["verify"]["similarity_threshold"])
    return VerificationResult(score=score, threshold=threshold, passed=score >= threshold)
