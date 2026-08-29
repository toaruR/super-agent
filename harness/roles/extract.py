"""design-extract パイプライン(T2〜T8)を束ねる新規ロール。

- harness.extract.{fetch,analyze,tokens,generate,storage,verify,refine} は各段階の
  ロジックを持つ独立モジュール(T2〜T8で実装済み)。本モジュールはそれらをimportして
  呼び出すだけで、各段階の処理そのものは一切実装しない(疎結合を保つため)。
- run_fetch/run_analyze/run_tokenize/run_generate/run_store/run_verify/run_refine は
  対応する段階を単独で呼び出せる薄いラッパー。run_pipeline() はこのうち
  fetch→analyze→tokenize→generate→store を順に実行し、PipelineResult を返す。
- PipelineResult.design_file (= 保存された prompt.md の絶対パス文字列) は、
  harness.roles.planner の --design_file 引数と同じ「プレーンテキストとして
  read_text() されるMarkdownファイルパス」という契約を満たすため、そのまま
  plan役割への入力候補として渡せる。
- 本モジュールは harness.roles.planner / decomposer / drive を一切importしない
  (それらのロジック・シグネチャには触れない)。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Union

from harness.extract.analyze import ComponentAnalysis, analyze_components
from harness.extract.fetch import BrowserDriver, PageFetchResult, fetch_rendered_page
from harness.extract.generate import render_prompt
from harness.extract.refine import RefinementResult, apply_refinement
from harness.extract.robots import RobotsChecker
from harness.extract.storage import PROMPT_FILENAME, TOKENS_FILENAME, save_snapshot
from harness.extract.tokens import build_design_tokens
from harness.extract.verify import (
    ImageLike,
    ImageProvider,
    SimilarityScorer,
    VerificationResult,
    compute_similarity,
    verify_against_threshold,
)

__all__ = [
    "PipelineResult",
    "run_fetch",
    "run_analyze",
    "run_tokenize",
    "run_generate",
    "run_store",
    "run_verify",
    "run_refine",
    "run_pipeline",
]


@dataclass(frozen=True)
class PipelineResult:
    """run_pipeline() の戻り値。各段階の中間結果と、最終的な保存先を束ねる。"""

    url: str
    fetch_result: PageFetchResult
    analysis: ComponentAnalysis
    tokens: Dict[str, Any]
    prompt: str
    snapshot_dir: Path
    prompt_path: Path
    tokens_path: Path

    @property
    def design_file(self) -> str:
        """harness.roles.planner の --design_file にそのまま渡せるパス文字列。"""
        return str(self.prompt_path)


def run_fetch(
    url: str,
    driver: BrowserDriver,
    *,
    robots_checker: RobotsChecker,
    breakpoints: Optional[Sequence[int]] = None,
    config: Optional[dict] = None,
) -> PageFetchResult:
    """T2(fetch): レンダリング後DOM/computed styleを取得する。単独呼び出し可能。"""
    return fetch_rendered_page(
        url, driver, robots_checker=robots_checker, breakpoints=breakpoints, config=config
    )


def run_analyze(
    fetch_result: PageFetchResult,
    *,
    component_types: Optional[Sequence[str]] = None,
) -> ComponentAnalysis:
    """T3(analyze): コンポーネント単位でスタイルパターンを抽出する。単独呼び出し可能。"""
    if component_types is None:
        return analyze_components(fetch_result)
    return analyze_components(fetch_result, component_types=component_types)


def run_tokenize(analysis: ComponentAnalysis) -> Dict[str, Any]:
    """T4(tokenize): W3C Design Tokens形式のJSONに変換する。単独呼び出し可能。"""
    return build_design_tokens(analysis)


def run_generate(tokens: Dict[str, Any], *, url: Optional[str] = None) -> str:
    """T6(generate): トークンJSONからMarkdownデザインプロンプトを生成する。単独呼び出し可能。"""
    return render_prompt(tokens, url=url)


def run_store(
    base_dir: Union[str, Path],
    site: str,
    tokens: Dict[str, Any],
    prompt: str,
    *,
    screenshots: Optional[Dict[str, bytes]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> Path:
    """T7(store): トークンJSONと生成プロンプトをスナップショットとして保存する。単独呼び出し可能。"""
    return save_snapshot(
        base_dir,
        site,
        tokens,
        prompt,
        screenshots=screenshots,
        metadata=metadata,
        timestamp=timestamp,
    )


def run_verify(
    original: Union[ImageLike, ImageProvider],
    regenerated: Union[ImageLike, ImageProvider],
    *,
    scorer: SimilarityScorer,
    config: Optional[dict] = None,
    config_path: Optional[Path] = None,
) -> VerificationResult:
    """T8(verify): 元サイトと再生成UIの視覚的類似度を検証する。単独呼び出し可能。"""
    score = compute_similarity(original, regenerated, scorer=scorer)
    return verify_against_threshold(score, config=config, config_path=config_path)


def run_refine(
    instruction: str,
    tokens: Dict[str, Any],
    *,
    url: Optional[str] = None,
    **kwargs: Any,
) -> RefinementResult:
    """自然言語の再調整指示をトークンJSONへ反映し、プロンプトを再生成する。単独呼び出し可能。

    invoke_fn 等の追加引数は apply_refinement() (T8/refine段階) にそのまま委譲するため、
    実ベンダー呼び出しなしにフェイクの invoke_fn を注入してテストできる。
    """
    return apply_refinement(instruction, tokens, url=url, **kwargs)


def run_pipeline(
    url: str,
    driver: BrowserDriver,
    *,
    robots_checker: RobotsChecker,
    base_dir: Union[str, Path],
    site: Optional[str] = None,
    breakpoints: Optional[Sequence[int]] = None,
    component_types: Optional[Sequence[str]] = None,
    config: Optional[dict] = None,
    screenshots: Optional[Dict[str, bytes]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> PipelineResult:
    """fetch→analyze→tokenize→generate→store を順に一括実行する。

    verify/refine は生成物(再生成UIのスクリーンショットや自然言語の再調整指示)を
    必要とする独立した後工程であり、一括抽出の対象には含めない。呼び出し側は
    run_verify()/run_refine() を個別に呼ぶ。

    戻り値の PipelineResult.design_file (= 保存済み prompt.md の絶対パス) は、
    harness.roles.planner へ design_file 入力候補としてそのまま渡せる。
    """
    fetch_result = run_fetch(
        url, driver, robots_checker=robots_checker, breakpoints=breakpoints, config=config
    )
    analysis = run_analyze(fetch_result, component_types=component_types)
    tokens = run_tokenize(analysis)
    prompt = run_generate(tokens, url=url)
    snapshot_dir = run_store(
        base_dir,
        site or url,
        tokens,
        prompt,
        screenshots=screenshots,
        metadata=metadata,
        timestamp=timestamp,
    )

    return PipelineResult(
        url=url,
        fetch_result=fetch_result,
        analysis=analysis,
        tokens=tokens,
        prompt=prompt,
        snapshot_dir=snapshot_dir,
        prompt_path=snapshot_dir / PROMPT_FILENAME,
        tokens_path=snapshot_dir / TOKENS_FILENAME,
    )
