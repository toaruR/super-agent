"""design-extract パイプラインの永続化層（Store 段階）。

- save_snapshot(): 抽出結果一式（トークンJSON・生成プロンプトMarkdown・スクリーンショット・
  メタデータ）を `<base_dir>/<site>/<timestamp>/` 配下にスナップショットとして保存する。
  トークンJSONと生成プロンプトは常に同一スナップショット内にペアで書き出す。
- load_snapshot(): 保存済みスナップショットを読み込む。timestamp を省略すると最新
  （タイムスタンプの辞書順最大値）のスナップショットを読み込む。

既存の harness.core.ledger（設計/タスクの実行履歴を追記する append-only ログ）とは
完全に独立しており、このモジュールは ledger ファイルを一切読み書きしない。対象サイトは
更新され得るため、抽出結果はサイト単位・抽出日時単位のディレクトリに分離して保存し、
同じ (site, timestamp) の組でも既存スナップショットは上書きしない（衝突時は例外を送出する）。
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

TOKENS_FILENAME = "tokens.json"
PROMPT_FILENAME = "prompt.md"
DESIGN_FILENAME = "DESIGN.md"
METADATA_FILENAME = "metadata.json"
SCREENSHOTS_DIRNAME = "screenshots"
TOKENS_CSS_FILENAME = "tokens.css"
COMPONENTS_CSS_FILENAME = "components.css"
SKELETON_HTML_FILENAME = "skeleton.html"

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class SnapshotExistsError(FileExistsError):
    """save_snapshot() が既存スナップショットを上書きしそうになったときに送出される。"""


class SnapshotNotFoundError(FileNotFoundError):
    """load_snapshot() が指定された (site, timestamp) のスナップショットを見つけられなかったときに送出される。"""


@dataclass(frozen=True)
class Snapshot:
    """load_snapshot() の戻り値。1回分の抽出結果一式を束ねる。"""

    path: Path
    site: str
    timestamp: str
    tokens: Dict[str, Any]
    prompt: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    screenshots: Dict[str, bytes] = field(default_factory=dict)
    tokens_css: Optional[str] = None
    components_css: Optional[str] = None
    skeleton_html: Optional[str] = None

    @property
    def design_file(self) -> str:
        return str(self.path / DESIGN_FILENAME if (self.path / DESIGN_FILENAME).exists() else self.path / PROMPT_FILENAME)


def _slugify_site(site: str) -> str:
    """site（URLまたはホスト名文字列）をディレクトリ名として安全な文字列に変換する。"""
    parsed = urlparse(site if "//" in site else f"//{site}")
    host = parsed.netloc or site
    slug = re.sub(r"[^a-zA-Z0-9.-]+", "-", host.lower()).strip("-")
    return slug or "site"


def _new_timestamp() -> str:
    return time.strftime(_TIMESTAMP_FORMAT, time.gmtime())


def _snapshot_dir(base_dir: Union[str, Path], site: str, timestamp: str) -> Path:
    return Path(base_dir) / _slugify_site(site) / timestamp


def save_snapshot(
    base_dir: Union[str, Path],
    site: str,
    tokens: Dict[str, Any],
    prompt: str,
    *,
    screenshots: Optional[Dict[str, bytes]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
    tokens_css: Optional[str] = None,
    components_css: Optional[str] = None,
    skeleton_html: Optional[str] = None,
) -> Path:
    """抽出結果を `<base_dir>/<site>/<timestamp>/` にスナップショットとして保存する。

    - tokens (デザイントークンJSON) と prompt (デザインプロンプトMarkdown) は常に
      同一スナップショットディレクトリ内にペアで書き出される。
    - timestamp を省略すると現在時刻(UTC)から自動生成する。
    - 対象ディレクトリが既に存在する場合は何も書き換えず SnapshotExistsError を送出する
      （既存スナップショットの上書き防止）。
    - 書き込みは一時ディレクトリに対して行い、完了後に最終パスへ rename することで、
      途中失敗時に不完全なスナップショットが本来のパスへ残ることを防ぐ。
    """
    ts = timestamp or _new_timestamp()
    site_dir = Path(base_dir) / _slugify_site(site)
    target_dir = site_dir / ts

    if target_dir.exists():
        raise SnapshotExistsError(f"snapshot already exists, refusing to overwrite: {target_dir}")

    site_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".tmp-{ts}-", dir=site_dir))
    try:
        (tmp_dir / TOKENS_FILENAME).write_text(
            json.dumps(tokens, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        (tmp_dir / PROMPT_FILENAME).write_text(prompt, encoding="utf-8")
        (tmp_dir / DESIGN_FILENAME).write_text(prompt, encoding="utf-8")
        (tmp_dir / METADATA_FILENAME).write_text(
            json.dumps(metadata or {}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        if screenshots:
            shots_dir = tmp_dir / SCREENSHOTS_DIRNAME
            shots_dir.mkdir()
            for name, data in screenshots.items():
                (shots_dir / name).write_bytes(data)

        if tokens_css is not None:
            (tmp_dir / TOKENS_CSS_FILENAME).write_text(tokens_css, encoding="utf-8")
        if components_css is not None:
            (tmp_dir / COMPONENTS_CSS_FILENAME).write_text(components_css, encoding="utf-8")
        if skeleton_html is not None:
            (tmp_dir / SKELETON_HTML_FILENAME).write_text(skeleton_html, encoding="utf-8")

        if target_dir.exists():
            # save_snapshot() の呼び出し中に別プロセス/スレッドが同じ (site, timestamp)
            # を作成した場合の競合。上書きせず例外にする。
            raise SnapshotExistsError(f"snapshot already exists, refusing to overwrite: {target_dir}")
        tmp_dir.rename(target_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return target_dir


def list_snapshot_timestamps(base_dir: Union[str, Path], site: str) -> List[str]:
    """指定サイトの保存済みスナップショットのタイムスタンプ一覧を古い順に返す。"""
    site_dir = Path(base_dir) / _slugify_site(site)
    if not site_dir.is_dir():
        return []
    return sorted(
        p.name for p in site_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".tmp-")
    )


def load_snapshot(
    base_dir: Union[str, Path],
    site: str,
    timestamp: Optional[str] = None,
) -> Snapshot:
    """保存済みスナップショットを読み込む。

    timestamp を省略すると、そのサイトの最新スナップショット（タイムスタンプの辞書順
    最大値。_new_timestamp() の形式は辞書順ソートが時系列順と一致する）を読み込む。
    """
    if timestamp is None:
        timestamps = list_snapshot_timestamps(base_dir, site)
        if not timestamps:
            raise SnapshotNotFoundError(f"no snapshots found for site: {site!r} under {base_dir!r}")
        timestamp = timestamps[-1]

    snapshot_dir = _snapshot_dir(base_dir, site, timestamp)
    if not snapshot_dir.is_dir():
        raise SnapshotNotFoundError(f"snapshot not found: {snapshot_dir}")

    tokens = json.loads((snapshot_dir / TOKENS_FILENAME).read_text(encoding="utf-8"))
    prompt = (snapshot_dir / PROMPT_FILENAME).read_text(encoding="utf-8")

    metadata_path = snapshot_dir / METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}

    screenshots: Dict[str, bytes] = {}
    shots_dir = snapshot_dir / SCREENSHOTS_DIRNAME
    if shots_dir.is_dir():
        for shot_path in sorted(shots_dir.iterdir()):
            if shot_path.is_file():
                screenshots[shot_path.name] = shot_path.read_bytes()

    tokens_css_path = snapshot_dir / TOKENS_CSS_FILENAME
    tokens_css = tokens_css_path.read_text(encoding="utf-8") if tokens_css_path.exists() else None

    components_css_path = snapshot_dir / COMPONENTS_CSS_FILENAME
    components_css = components_css_path.read_text(encoding="utf-8") if components_css_path.exists() else None

    skeleton_html_path = snapshot_dir / SKELETON_HTML_FILENAME
    skeleton_html = skeleton_html_path.read_text(encoding="utf-8") if skeleton_html_path.exists() else None

    return Snapshot(
        path=snapshot_dir,
        site=site,
        timestamp=timestamp,
        tokens=tokens,
        prompt=prompt,
        metadata=metadata,
        screenshots=screenshots,
        tokens_css=tokens_css,
        components_css=components_css,
        skeleton_html=skeleton_html,
    )
