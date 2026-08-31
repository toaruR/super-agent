"""harness/roles/extract.py の単体テスト。

- run_pipeline() が fetch→analyze→tokenize→generate→store の順に各段階を実行すること。
- run_pipeline() の戻り値が、保存済みprompt.mdへのパスをplan役割の--design_file相当の
  入力としてそのまま使える形(read_text()可能なテキストファイルパス)で返すこと。
- fetch/analyze/tokenize/generate/store の各段階が、run_pipeline()を経由せず
  個別に呼び出し可能な関数として公開されていること。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pytest

from harness.extract.fetch import RenderResult
from harness.extract.robots import RobotsChecker
from harness.roles import extract as extract_role

ALLOW_ALL_ROBOTS_TXT = "User-agent: *\nAllow: /\n"


class FakeBrowserDriver:
    """BrowserDriver を満たすフェイク実装(実ブラウザを一切起動しない)。"""

    def render(self, url: str, viewport_width: int) -> RenderResult:
        return RenderResult(
            outer_html=(
                "<button class='btn'>x</button><nav class='navbar'>x</nav>"
            ),
            computed_styles={
                "button:0": {"color": "#111111", "border-radius": "4px"},
                "nav:1": {"color": "#222222"},
            },
        )


def _robots_checker() -> RobotsChecker:
    return RobotsChecker(ALLOW_ALL_ROBOTS_TXT, user_agent="*")


def test_pipeline_stages_run_in_order_fetch_analyze_tokenize_generate_store(
    tmp_path, monkeypatch
) -> None:
    order: List[str] = []

    real_fetch = extract_role.fetch_rendered_page
    real_analyze = extract_role.analyze_components
    real_tokenize = extract_role.build_design_tokens
    real_generate = extract_role.render_prompt
    real_store = extract_role.save_snapshot

    def spy_fetch(*args, **kwargs):
        order.append("fetch")
        return real_fetch(*args, **kwargs)

    def spy_analyze(*args, **kwargs):
        order.append("analyze")
        return real_analyze(*args, **kwargs)

    def spy_tokenize(*args, **kwargs):
        order.append("tokenize")
        return real_tokenize(*args, **kwargs)

    def spy_generate(*args, **kwargs):
        order.append("generate")
        return real_generate(*args, **kwargs)

    def spy_store(*args, **kwargs):
        order.append("store")
        return real_store(*args, **kwargs)

    monkeypatch.setattr(extract_role, "fetch_rendered_page", spy_fetch)
    monkeypatch.setattr(extract_role, "analyze_components", spy_analyze)
    monkeypatch.setattr(extract_role, "build_design_tokens", spy_tokenize)
    monkeypatch.setattr(extract_role, "render_prompt", spy_generate)
    monkeypatch.setattr(extract_role, "save_snapshot", spy_store)

    result = extract_role.run_pipeline(
        "https://example.com/",
        FakeBrowserDriver(),
        robots_checker=_robots_checker(),
        base_dir=tmp_path,
        breakpoints=[375, 1280],
    )

    assert order == ["fetch", "analyze", "tokenize", "generate", "store"]
    assert isinstance(result, extract_role.PipelineResult)


def test_returns_design_file_path_usable_by_plan(tmp_path) -> None:
    result = extract_role.run_pipeline(
        "https://example.com/",
        FakeBrowserDriver(),
        robots_checker=_robots_checker(),
        base_dir=tmp_path,
        breakpoints=[375, 1280],
        timestamp="20260101T000000Z",
    )

    design_file = result.design_file
    assert isinstance(design_file, str)

    design_path = Path(design_file)
    assert design_path.is_file()
    assert design_path.name == "prompt.md"

    # harness.roles.planner の replan_from_file は design_path を
    # Path(design_path).read_text(encoding="utf-8", errors="ignore") で読む
    # (harness/roles/planner.py 参照)ため、同じ読み方で内容が取得できることを確認する。
    content = design_path.read_text(encoding="utf-8", errors="ignore")
    assert "# Design Prompt" in content
    assert content == result.prompt


def test_each_stage_can_be_invoked_independently(tmp_path) -> None:
    fetch_result = extract_role.run_fetch(
        "https://example.com/",
        FakeBrowserDriver(),
        robots_checker=_robots_checker(),
        breakpoints=[375, 1280],
    )
    assert fetch_result.url == "https://example.com/"
    assert len(fetch_result.breakpoints) == 2

    analysis = extract_role.run_analyze(fetch_result)
    assert analysis.url == "https://example.com/"
    assert any(bp.by_type("button") for bp in analysis.breakpoints)

    tokens = extract_role.run_tokenize(analysis)
    assert set(tokens.keys()) == {"color", "typography", "spacing", "radius", "shadow"}

    prompt = extract_role.run_generate(tokens, url="https://example.com/")
    assert prompt.startswith("# Design Prompt")

    snapshot_dir = extract_role.run_store(
        tmp_path, "https://example.com/", tokens, prompt, timestamp="20260101T000000Z"
    )
    assert (snapshot_dir / "tokens.json").is_file()
    assert (snapshot_dir / "prompt.md").is_file()

    def fake_scorer(original, regenerated) -> float:
        return 0.99

    verification = extract_role.run_verify(
        b"orig-bytes", b"regen-bytes", scorer=fake_scorer
    )
    assert verification.passed is True

    def fake_invoke_fn(decl, prompt, **kwargs) -> Dict[str, object]:
        return {
            "result": {
                "category": "radius",
                "component_type": "button",
                "property": "border-radius",
                "value": "16px",
            }
        }

    refinement = extract_role.run_refine(
        "ボタンの角丸をもっと大きくして",
        tokens,
        url="https://example.com/",
        invoke_fn=fake_invoke_fn,
        decl=object(),
    )
    assert refinement.tokens != tokens
    assert refinement.prompt.startswith("# Design Prompt")


def test_pipeline_generates_and_stores_css_and_html_assets(tmp_path) -> None:
    result = extract_role.run_pipeline(
        "https://example.com/",
        FakeBrowserDriver(),
        robots_checker=_robots_checker(),
        base_dir=tmp_path,
        breakpoints=[375, 1280],
        timestamp="20260101T000000Z",
    )

    # Verify asset files are generated and stored
    assert result.tokens_css_path.is_file()
    assert result.components_css_path.is_file()
    assert result.skeleton_html_path.is_file()

    assert result.tokens_css_path.name == "tokens.css"
    assert result.components_css_path.name == "components.css"
    assert result.skeleton_html_path.name == "skeleton.html"

    # Verify dataclass properties
    assert ":root" in result.tokens_css
    assert ".btn-primary" in result.components_css
    assert "<!DOCTYPE html>" in result.skeleton_html

    # Verify file contents match dataclass properties
    assert result.tokens_css_path.read_text(encoding="utf-8") == result.tokens_css
    assert result.components_css_path.read_text(encoding="utf-8") == result.components_css
    assert result.skeleton_html_path.read_text(encoding="utf-8") == result.skeleton_html


class TitledBrowserDriver:
    """fetch段階が実ページから取得するメタデータ(title等)を模したフェイクドライバ。"""

    def render(self, url: str, viewport_width: int):
        return RenderResult(
            outer_html="<button class='btn'>x</button><nav class='navbar'>x</nav>",
            computed_styles={
                "button:0": {"color": "#111111", "border-radius": "4px"},
                "nav:1": {"color": "#222222"},
            },
            metadata={"title": "Real Fetched Page Title", "ogTitle": "OG Title"},
        )


def test_pipeline_uses_fetched_metadata_for_skeleton_title_and_snapshot(tmp_path) -> None:
    # run_pipeline() は metadata引数を明示的に渡さなくても、fetch段階で実際に取得した
    # ページの title 等を skeleton.html / metadata.json に反映しなければならない
    # (metadata引数は常にCLIから渡されないため、fetch結果を無視すると常に空になる)。
    result = extract_role.run_pipeline(
        "https://example.com/",
        TitledBrowserDriver(),
        robots_checker=_robots_checker(),
        base_dir=tmp_path,
        breakpoints=[375, 1280],
        timestamp="20260101T000000Z",
    )

    assert result.fetch_result.metadata.get("title") == "Real Fetched Page Title"
    assert "<title>Real Fetched Page Title</title>" in result.skeleton_html

    import json
    metadata_path = result.snapshot_dir / "metadata.json"
    saved_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert saved_metadata.get("title") == "Real Fetched Page Title"


def test_pipeline_explicit_metadata_overrides_fetched_metadata(tmp_path) -> None:
    # 呼び出し元が明示的に metadata を渡した場合は、fetch段階の実測値より優先される。
    result = extract_role.run_pipeline(
        "https://example.com/",
        TitledBrowserDriver(),
        robots_checker=_robots_checker(),
        base_dir=tmp_path,
        breakpoints=[375, 1280],
        timestamp="20260101T000000Z",
        metadata={"title": "Explicit Override Title"},
    )

    assert "<title>Explicit Override Title</title>" in result.skeleton_html


def test_reproduce_ui_skill_files_exist() -> None:
    from pathlib import Path
    agents_skill = Path(".agents/skills/reproduce-ui/SKILL.md")
    claude_skill = Path(".claude/skills/reproduce-ui/SKILL.md")
    assert agents_skill.is_file(), f"missing {agents_skill}"
    assert claude_skill.is_file(), f"missing {claude_skill}"
    agents_content = agents_skill.read_text(encoding="utf-8")
    claude_content = claude_skill.read_text(encoding="utf-8")
    assert "reproduce-ui" in agents_content
    assert "reproduce-ui" in claude_content
    assert "Step 1:" in agents_content
    assert "Step 4:" in agents_content

