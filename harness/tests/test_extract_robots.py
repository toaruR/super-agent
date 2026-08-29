"""harness/extract/robots.py の単体テスト。

- RobotsChecker が robots.txt を user-agent 単位で解釈し、単純な文字列前方一致では
  取りこぼす Allow による例外も正しく扱えること。
- select_crawl_targets() が候補URL群をトップ/代表的な一覧/代表的な詳細ページに限定し、
  無制限クロールにならないこと。
- design_extract.yaml に breakpoints/crawl/verify のキーが揃っていること。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from harness.extract.robots import (
    RobotsChecker,
    classify_url,
    load_design_extract_config,
    select_crawl_targets,
)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "design_extract.yaml"

ROBOTS_TXT = """
User-agent: DesignExtractBot
Disallow: /private/
Disallow: /admin/
Allow: /private/public-notice.html

User-agent: *
Disallow: /
"""


def test_disallowed_urls_filtered_by_robots_txt() -> None:
    checker = RobotsChecker(ROBOTS_TXT, user_agent="DesignExtractBot")

    assert checker.is_allowed("https://example.com/")
    assert checker.is_allowed("https://example.com/products/widget")
    assert not checker.is_allowed("https://example.com/private/secret.html")
    assert not checker.is_allowed("https://example.com/admin/dashboard")
    # Allow による例外は Disallow より優先される（単純な前方一致では拾えないケース）。
    assert checker.is_allowed("https://example.com/private/public-notice.html")

    urls = [
        "https://example.com/",
        "https://example.com/private/secret.html",
        "https://example.com/products/widget",
        "https://example.com/admin/dashboard",
        "https://example.com/private/public-notice.html",
    ]
    filtered = checker.filter_allowed(urls)
    assert filtered == [
        "https://example.com/",
        "https://example.com/products/widget",
        "https://example.com/private/public-notice.html",
    ]

    # user-agent が異なる（"*" グループにのみマッチする）クローラは全面禁止される。
    other = RobotsChecker(ROBOTS_TXT, user_agent="SomeOtherBot")
    assert not other.is_allowed("https://example.com/products/widget")


def test_crawl_targets_limited_to_top_listing_detail_pages() -> None:
    assert classify_url("https://example.com/") == "top"
    assert classify_url("https://example.com/blog") == "listing"
    assert classify_url("https://example.com/blog/my-first-post") == "detail"
    assert classify_url("https://example.com/assets/app.css") == "asset"

    candidates = [
        "https://example.com/",
        "https://example.com/assets/app.css",
        "https://example.com/blog",
        "https://example.com/products",
        "https://example.com/news",
        "https://example.com/about",  # listing 上限を超える分は選定されない
        "https://example.com/blog/post-1",
        "https://example.com/blog/post-2",
        "https://example.com/products/widget",
        "https://example.com/products/gadget",  # detail 上限を超える分は選定されない
    ]

    selection = select_crawl_targets(
        candidates,
        max_top_pages=1,
        max_listing_pages=3,
        max_detail_pages=3,
    )

    assert selection.top == ["https://example.com/"]
    assert selection.listing == [
        "https://example.com/blog",
        "https://example.com/products",
        "https://example.com/news",
    ]
    assert selection.detail == [
        "https://example.com/blog/post-1",
        "https://example.com/blog/post-2",
        "https://example.com/products/widget",
    ]
    # 無制限クロールにならないこと（候補は10件だが選定は上限内に収まる）。
    assert len(selection.urls) == 7
    assert len(selection.urls) < len(candidates)
    assert "https://example.com/assets/app.css" not in selection.urls
    assert "https://example.com/about" not in selection.urls
    assert "https://example.com/products/gadget" not in selection.urls

    # robots.txt での禁止も反映されること。
    robots_txt = "User-agent: *\nDisallow: /blog/post-2\n"
    checker = RobotsChecker(robots_txt, user_agent="*")
    selection_with_robots = select_crawl_targets(candidates, checker)
    assert "https://example.com/blog/post-2" not in selection_with_robots.urls

    # max_pages によって全体件数も明確に頭打ちになること。
    capped = select_crawl_targets(candidates, max_pages=3)
    assert len(capped.urls) == 3


def test_design_extract_config_has_breakpoints_crawl_verify_keys() -> None:
    assert CONFIG_PATH.exists()

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    assert "breakpoints" in raw
    assert isinstance(raw["breakpoints"], list)
    assert len(raw["breakpoints"]) > 0
    assert all(isinstance(bp, int) for bp in raw["breakpoints"])

    assert "crawl" in raw
    crawl = raw["crawl"]
    assert "max_pages" in crawl
    assert "respect_robots_txt" in crawl
    assert crawl["respect_robots_txt"] is True

    assert "verify" in raw
    verify = raw["verify"]
    assert "similarity_threshold" in verify
    assert 0.0 <= verify["similarity_threshold"] <= 1.0

    # loader ヘルパー経由でも同じ内容が取得できること（他パイプライン段階からの読込を想定）。
    loaded = load_design_extract_config(CONFIG_PATH)
    assert loaded == raw
