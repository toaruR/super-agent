"""harness/extract/storage.py の単体テスト。

- save_snapshot() がサイト単位・抽出日時(timestamp)単位でディレクトリを分離すること。
- トークンJSONと生成プロンプトMarkdownが常に同一スナップショット内にペアで保存されること。
- 既に存在するスナップショットを上書きしないこと（衝突時は例外になり、既存の内容が
  そのまま残ること）。
"""
from __future__ import annotations

import json

import pytest

from harness.extract.storage import (
    COMPONENTS_CSS_FILENAME,
    SKELETON_HTML_FILENAME,
    TOKENS_CSS_FILENAME,
    SnapshotExistsError,
    SnapshotNotFoundError,
    list_snapshot_timestamps,
    load_snapshot,
    save_snapshot,
)

SITE = "https://example.com/"
TOKENS = {
    "color": {
        "brand-primary": {"$type": "color", "$value": "#1a1aff"},
    }
}
PROMPT = "# Design Prompt\n\n- primary color: #1a1aff\n"


def test_creates_versioned_snapshot_directory_per_timestamp(tmp_path) -> None:
    base_dir = tmp_path / "design-extracts"

    dir_a = save_snapshot(base_dir, SITE, TOKENS, PROMPT, timestamp="20260101T000000Z")
    dir_b = save_snapshot(base_dir, SITE, TOKENS, PROMPT, timestamp="20260102T000000Z")

    assert dir_a != dir_b
    assert dir_a.is_dir()
    assert dir_b.is_dir()
    # 同じサイト配下に、タイムスタンプごとに別ディレクトリとして分離されている。
    assert dir_a.parent == dir_b.parent
    site_dir = dir_a.parent
    assert site_dir.name not in ("", ".", "..")
    # サイトURL丸ごとがディレクトリ名になっているわけではない（スキーム/スラッシュを含まない）。
    assert "://" not in site_dir.name
    assert "/" not in site_dir.name

    timestamps = list_snapshot_timestamps(base_dir, SITE)
    assert timestamps == ["20260101T000000Z", "20260102T000000Z"]

    # 抽出結果は既存のledger機構（ledger.jsonl等の単一追記ファイル）とは別のディレクトリ構成、
    # つまりサイト/タイムスタンプ単位のディレクトリツリーとして保存されている。
    assert (base_dir / site_dir.name / "20260101T000000Z").is_dir()
    assert (base_dir / site_dir.name / "20260102T000000Z").is_dir()


def test_creates_separate_directories_per_site(tmp_path) -> None:
    base_dir = tmp_path / "design-extracts"

    dir_a = save_snapshot(base_dir, "https://example.com/", TOKENS, PROMPT, timestamp="20260101T000000Z")
    dir_b = save_snapshot(base_dir, "https://other.example/", TOKENS, PROMPT, timestamp="20260101T000000Z")

    assert dir_a != dir_b
    assert dir_a.parent != dir_b.parent
    assert dir_a.is_dir() and dir_b.is_dir()


def test_saves_tokens_and_prompt_as_paired_files(tmp_path) -> None:
    base_dir = tmp_path / "design-extracts"
    metadata = {"source_url": SITE, "extracted_at": "2026-01-01T00:00:00Z"}
    screenshots = {"top-1280.png": b"\x89PNG\r\n\x1a\nfake-bytes"}

    snapshot_dir = save_snapshot(
        base_dir,
        SITE,
        TOKENS,
        PROMPT,
        screenshots=screenshots,
        metadata=metadata,
        timestamp="20260101T000000Z",
    )

    # トークンJSONとプロンプトMarkdownが両方、同一スナップショットディレクトリ内に存在する。
    tokens_path = snapshot_dir / "tokens.json"
    prompt_path = snapshot_dir / "prompt.md"
    assert tokens_path.is_file()
    assert prompt_path.is_file()
    assert json.loads(tokens_path.read_text(encoding="utf-8")) == TOKENS
    assert prompt_path.read_text(encoding="utf-8") == PROMPT

    loaded = load_snapshot(base_dir, SITE, timestamp="20260101T000000Z")
    assert loaded.tokens == TOKENS
    assert loaded.prompt == PROMPT
    assert loaded.metadata == metadata
    assert loaded.screenshots == screenshots
    assert loaded.path == snapshot_dir

    # timestamp省略時は最新スナップショットを読み込む。
    save_snapshot(base_dir, SITE, {"color": {}}, "# newer\n", timestamp="20260102T000000Z")
    latest = load_snapshot(base_dir, SITE)
    assert latest.timestamp == "20260102T000000Z"
    assert latest.tokens == {"color": {}}


def test_does_not_overwrite_previous_snapshot(tmp_path) -> None:
    base_dir = tmp_path / "design-extracts"
    timestamp = "20260101T000000Z"

    original_tokens = dict(TOKENS)
    save_snapshot(base_dir, SITE, original_tokens, PROMPT, timestamp=timestamp)

    conflicting_tokens = {"color": {"brand-primary": {"$type": "color", "$value": "#ff0000"}}}
    with pytest.raises(SnapshotExistsError):
        save_snapshot(base_dir, SITE, conflicting_tokens, "# overwritten\n", timestamp=timestamp)

    # 既存スナップショットの内容は変更されていない。
    reloaded = load_snapshot(base_dir, SITE, timestamp=timestamp)
    assert reloaded.tokens == original_tokens
    assert reloaded.prompt == PROMPT

    # 衝突時に半端な一時ディレクトリがスナップショットとして残っていない。
    assert list_snapshot_timestamps(base_dir, SITE) == [timestamp]


def test_load_snapshot_missing_raises(tmp_path) -> None:
    base_dir = tmp_path / "design-extracts"

    with pytest.raises(SnapshotNotFoundError):
        load_snapshot(base_dir, SITE, timestamp="20260101T000000Z")

    save_snapshot(base_dir, SITE, TOKENS, PROMPT, timestamp="20260101T000000Z")
    with pytest.raises(SnapshotNotFoundError):
        load_snapshot(base_dir, "https://never-extracted.example/")


def test_storage_constants_defined() -> None:
    assert TOKENS_CSS_FILENAME == "tokens.css"
    assert COMPONENTS_CSS_FILENAME == "components.css"
    assert SKELETON_HTML_FILENAME == "skeleton.html"


def test_saves_css_and_skeleton_html_assets(tmp_path) -> None:
    base_dir = tmp_path / "design-extracts"
    timestamp = "20260101T000000Z"
    tokens_css_content = ":root { --color-primary: #1a1aff; }"
    components_css_content = ".btn-primary { background: var(--color-primary); }"
    skeleton_html_content = "<!DOCTYPE html><html><head><title>Skeleton</title></head><body></body></html>"

    snapshot_dir = save_snapshot(
        base_dir,
        SITE,
        TOKENS,
        PROMPT,
        timestamp=timestamp,
        tokens_css=tokens_css_content,
        components_css=components_css_content,
        skeleton_html=skeleton_html_content,
    )

    tokens_css_path = snapshot_dir / TOKENS_CSS_FILENAME
    components_css_path = snapshot_dir / COMPONENTS_CSS_FILENAME
    skeleton_html_path = snapshot_dir / SKELETON_HTML_FILENAME

    assert tokens_css_path.is_file()
    assert components_css_path.is_file()
    assert skeleton_html_path.is_file()

    assert tokens_css_path.read_text(encoding="utf-8") == tokens_css_content
    assert components_css_path.read_text(encoding="utf-8") == components_css_content
    assert skeleton_html_path.read_text(encoding="utf-8") == skeleton_html_content

    loaded = load_snapshot(base_dir, SITE, timestamp=timestamp)
    assert loaded.tokens_css == tokens_css_content
    assert loaded.components_css == components_css_content
    assert loaded.skeleton_html == skeleton_html_content

