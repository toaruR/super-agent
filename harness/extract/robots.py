"""robots.txt 準拠チェックと、クロール対象URLの選定（design-extract パイプラインの Fetch 前段）。

- RobotsChecker: robots.txt を user-agent 単位で解釈し、URL ごとの許可/禁止を判定する。
  文字列の前方一致だけに頼ると Allow による例外（例: Disallow: /a/ だが Allow: /a/public/）を
  取りこぼすため、標準ライブラリの urllib.robotparser（Allow/Disallow の優先順位や
  ワイルドカードを踏まえた解釈をしてくれる）に処理を委譲する。
- select_crawl_targets: 候補URL群を「トップページ」「一覧ページ」「詳細ページ」に分類し、
  各カテゴリから代表的なものだけを上限件数で選び、無制限クロールにならないようにする。
"""
from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import yaml

DEFAULT_USER_AGENT = "DesignExtractBot/1.0"

# 画像・スタイルシート・フォント等の静的アセットはクロール（ページ解析）対象外。
# デザイントークンは computed style から取得するため、アセット自体を個別ページとして
# 選定する必要はない。
_ASSET_SUFFIXES = {
    ".css", ".js", ".mjs", ".json", ".xml", ".txt",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip",
}


def _compile_pattern(pattern: str) -> re.Pattern:
    """robots.txt のパスパターン（"*" ワイルドカード、末尾 "$" アンカー）を正規表現化する。"""
    end_anchor = pattern.endswith("$")
    body = pattern[:-1] if end_anchor else pattern
    regex_body = ".*".join(re.escape(part) for part in body.split("*"))
    if end_anchor:
        regex_body += "$"
    return re.compile("^" + regex_body)


def _parse_groups(robots_txt: str) -> List[Tuple[List[str], List[Tuple[bool, str]]]]:
    """robots.txt を (user-agent 群, [(is_allow, pattern), ...]) のグループ列にパースする。

    連続する User-agent 行は同じルールブロックを共有する（robots.txt の標準的な
    グループ構文）。空文字列の "Disallow:" は「制限なし」を意味するため無視する。
    """
    groups: List[Tuple[List[str], List[Tuple[bool, str]]]] = []
    current_agents: List[str] = []
    current_rules: List[Tuple[bool, str]] = []
    rule_seen_since_agent = False

    for raw_line in robots_txt.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "user-agent":
            if rule_seen_since_agent:
                # 直前のグループにルールが付いた後に新しい User-agent が来た場合は新グループ。
                groups.append((current_agents, current_rules))
                current_agents, current_rules = [], []
                rule_seen_since_agent = False
            current_agents.append(value)
        elif field in ("disallow", "allow"):
            if not current_agents:
                continue
            rule_seen_since_agent = True
            if field == "disallow" and value == "":
                continue
            current_rules.append((field == "allow", value))
        # crawl-delay / sitemap 等、クロール可否判定に関係ない行は無視する。

    if current_agents:
        groups.append((current_agents, current_rules))
    return groups


def _select_rules(
    groups: List[Tuple[List[str], List[Tuple[bool, str]]]], user_agent: str
) -> List[Tuple[bool, str]]:
    ua_lower = user_agent.lower()
    exact_rules: List[Tuple[bool, str]] = []
    for agents, rules in groups:
        if any(a.lower() == ua_lower for a in agents):
            exact_rules.extend(rules)
    if exact_rules:
        return exact_rules

    wildcard_rules: List[Tuple[bool, str]] = []
    for agents, rules in groups:
        if "*" in agents:
            wildcard_rules.extend(rules)
    return wildcard_rules


class RobotsChecker:
    """robots.txt を user-agent 単位で解釈し、URL の許可/禁止を判定する。

    urllib.robotparser はルールを宣言順に最初にマッチしたもので判定するため、
    「Disallow: /private/ だが Allow: /private/public/ で例外を許可する」といった
    より具体的なルールが優先されるべきケースを取りこぼす。ここでは Google の
    Robots Exclusion Protocol の解釈に倣い、最長一致（最も具体的なパターン）を
    優先し、同じ長さなら Allow を優先する。
    """

    def __init__(self, robots_txt: str, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.user_agent = user_agent
        groups = _parse_groups(robots_txt)
        rules = _select_rules(groups, user_agent)
        self._rules = [(is_allow, pattern, _compile_pattern(pattern)) for is_allow, pattern in rules]

    def is_allowed(self, url: str) -> bool:
        """指定 URL がこの user-agent に対して robots.txt 上クロール許可されているか。"""
        parsed = urlparse(url)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"

        best: Optional[Tuple[int, bool]] = None
        for is_allow, pattern, regex in self._rules:
            if regex.match(target) is None:
                continue
            candidate = (len(pattern), is_allow)
            if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and is_allow and not best[1]):
                best = candidate
        # ルールが一つも一致しない場合は robots.txt 不在時と同様に許可とみなす。
        return best is None or best[1]

    def filter_allowed(self, urls: Iterable[str]) -> List[str]:
        """許可された URL だけを、元の順序を保ったまま返す。"""
        return [u for u in urls if self.is_allowed(u)]

    @classmethod
    def from_site(
        cls,
        site_url: str,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 10.0,
        fetch: Optional[Callable[[str], str]] = None,
    ) -> "RobotsChecker":
        """サイトの /robots.txt を取得して RobotsChecker を構築する。

        取得に失敗した場合（robots.txt が存在しない等）は、robots.txt 不在時の
        一般的な解釈（全許可）に倣い空文字列として扱う。
        fetch を渡すとテスト等でネットワークアクセスを差し替えられる。
        """
        robots_url = urljoin(site_url, "/robots.txt")
        try:
            if fetch is not None:
                text = fetch(robots_url)
            else:
                with urllib.request.urlopen(robots_url, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return cls(text, user_agent=user_agent)


def _is_asset_path(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in _ASSET_SUFFIXES


def classify_url(url: str) -> str:
    """URL を "top" / "listing" / "detail" / "asset" のいずれかに分類する。

    - top: パスが空 or "/"（トップページ）
    - listing: パスセグメントが1つ（例: /blog, /products の代表的な一覧ページ）
    - detail: パスセグメントが2つ以上（例: /blog/2024/my-post の代表的な詳細ページ）
    - asset: 画像/CSS/JS等の静的ファイル（クロール対象外）
    """
    path = urlparse(url).path
    if _is_asset_path(path):
        return "asset"
    segments = [s for s in path.split("/") if s]
    if not segments:
        return "top"
    if len(segments) == 1:
        return "listing"
    return "detail"


@dataclass(frozen=True)
class CrawlTargetSelection:
    """select_crawl_targets() の結果。カテゴリ別に何を採用したか追跡できるようにする。"""

    top: List[str]
    listing: List[str]
    detail: List[str]

    @property
    def urls(self) -> List[str]:
        return [*self.top, *self.listing, *self.detail]


def select_crawl_targets(
    candidate_urls: Sequence[str],
    robots_checker: Optional[RobotsChecker] = None,
    *,
    max_top_pages: int = 1,
    max_listing_pages: int = 3,
    max_detail_pages: int = 3,
    max_pages: Optional[int] = None,
) -> CrawlTargetSelection:
    """候補URL群から、トップ/代表的な一覧/代表的な詳細ページに限定してクロール対象を選ぶ。

    - robots_checker が与えられれば、まず robots.txt で禁止されている URL を除外する。
    - 候補の順序を維持したまま、カテゴリごとに上限件数まで採用する（無制限クロール防止）。
    - max_pages を指定すると、カテゴリ別上限とは別に全体件数の上限としても働く。
    """
    allowed = list(candidate_urls)
    if robots_checker is not None:
        allowed = robots_checker.filter_allowed(allowed)

    top: List[str] = []
    listing: List[str] = []
    detail: List[str] = []

    for url in allowed:
        category = classify_url(url)
        if category == "asset":
            continue
        if category == "top" and len(top) < max_top_pages:
            top.append(url)
        elif category == "listing" and len(listing) < max_listing_pages:
            listing.append(url)
        elif category == "detail" and len(detail) < max_detail_pages:
            detail.append(url)

    if max_pages is not None:
        while len(top) + len(listing) + len(detail) > max_pages:
            if detail:
                detail.pop()
            elif listing:
                listing.pop()
            elif top:
                top.pop()
            else:
                break

    return CrawlTargetSelection(top=top, listing=listing, detail=detail)


def _default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "design_extract.yaml"


def load_design_extract_config(config_path: Optional[Path] = None) -> dict:
    """harness/config/design_extract.yaml を読み込む（fetch/verify 等の他段階も同じ関数を使う想定）。"""
    path = config_path or _default_config_path()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data
