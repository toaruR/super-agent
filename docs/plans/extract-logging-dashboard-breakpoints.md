# `super-agent extract run` 改良の実装計画

`super-agent extract run` コマンドにおいて、処理状況の可視化と適応的な抽出を可能にするため、以下の3つの機能を実装します。

1. **ログ出力**: CLI実行中に標準エラー出力（stderr）へリアルタイムの進捗ログを出力（標準出力のJSON出力契約を維持）。
2. **途中経過のDashboard出力**: Ledger / `progress` side-channel と連携し、`dashboard.html` / `dashboard.md` 上に抽出タスクのステータス（`extracting` → `extracted` / `failed`）および詳細進捗をリアルタイム反映。
3. **CSSからブレークポイントを自動抽出**: レンダリング時に `@media` クエリおよび CSS スタイルシートからブレークポイント（`px`, `rem`, `em`, `pt`）を自動抽出・正規化し、最適なビューポート幅でサンプリング。

---

## User Review Required

> [!NOTE]
> - CLI の標準出力 (`stdout`) は既存の JSON 契約（`{"ok": true, "url": ..., "design_file": ..., ...}`）を維持し、進捗ログはすべて標準エラー出力 (`stderr`) に `[extract] ...` 形式で出力します。これにより既存のスクリプトやテストとの互換性を完全に保ちます。
> - `--breakpoints` 引数が明示的に指定された場合はユーザー指定値が最優先され、未指定の場合にのみ CSS からの自動抽出（検出不可時は `design_extract.yaml` のデフォルト）が適用されます。

---

## Proposed Changes

### Component 1: CSS Breakpoint 自動抽出 & Fetch ロジックの拡張

#### [MODIFY] [fetch.py](file:///d:/vagrant/harnesses/super-agent/src/harness/extract/fetch.py)
- `extract_breakpoints_from_css(css_text: str) -> list[int]` 関数を新設:
  - `@media` クエリ内の `min-width`, `max-width`, `width >=`, `width <=` などの条件を正規表現で解析。
  - `px`, `rem`, `em`, `pt` の単位変換（1rem/em = 16px, 1pt = 4/3 px）を行い、整数 px に丸めて [320, 3840] の範囲でソート・重複排除。
- `PlaywrightBrowserDriver.render()` の JavaScript 評価処理を拡張:
  - `document.styleSheets` の `CSSMediaRule`、インライン `<style>` タグ、CSS カスタムプロパティを走査してブレークポイント数値を抽出。
  - `RenderResult` に `css_breakpoints: List[int]` フィールドを追加。
- `fetch_rendered_page()` の拡張:
  - `breakpoints` が未指定（`None`）の場合、初回レンダリングで得られた `css_breakpoints` を元にサンプリング用ビューポート幅リスト（例: モバイル 375px + 抽出された各ブレークポイント + デスクトップ 1280px）を自動構築。
  - CSS からブレークポイントが抽出できなかった場合は `design_extract.yaml` のデフォルト値（`[375, 768, 1280, 1920]`）へ安全にフォールバック。
  - `log_fn` / `progress_cb` コールバックを受け取り、ビューポートごとのレンダリング進捗を通知。

---

### Component 2: Extract ロールでの進捗通知・ログ連携

#### [MODIFY] [extract.py](file:///d:/vagrant/harnesses/super-agent/src/harness/roles/extract.py)
- `run_pipeline()` に `progress_cb: Optional[Callable[[str, str], None]] = None` および `log_fn: Optional[Callable[[str], None]] = None` 引数を追加。
- パイプラインの各段階で進行状況を通知・ログ出力:
  1. `[fetch]` URL確認・robots.txt 判定・CSSブレークポイント自動抽出・ビューポートレンダリング
  2. `[analyze]` コンポーネントおよびスタイル解析
  3. `[tokenize]` W3C Design Tokens 形式への変換
  4. `[generate]` prompt.md および DESIGN.md の生成
  5. `[store]` スナップショットのディスク保存

---

### Component 3: Dashboard & Ledger 連携

#### [MODIFY] [dashboard.py](file:///d:/vagrant/harnesses/super-agent/src/harness/roles/dashboard.py)
- `STATUS_MAP` に `"extract.ok": "extracted"`, `"extracting": "extracting"`, `"extract.error": "failed"`, `"extract.start": "extracting"` を登録。
- `STATUS_RANK` に `"extracted": 4`, `"extracting": 0.5` を追加。
- `_STATUS_BADGE` に `"extracted": ("Extracted", "badge-green")`, `"extracting": ("Extracting", "badge-purple")` を追加。
- `_BAR_BUCKETS` の `Completed` に `"extracted"`、`In Progress` に `"extracting"` を追加。
- `_NON_TERMINAL_STATUSES` に `"extracting"` を追加（stale 検出対応）。

---

### Component 4: CLI コマンド (`super-agent extract run`) の統合

#### [MODIFY] [cli.py](file:///d:/vagrant/harnesses/super-agent/src/harness/cli.py)
- `cmd_extract_run()` を改良:
  - `ensure_ledger()` と `seq.start()` で Ledger セッションを開始。
  - 決定的タスクID `task_id = f"extract-{slugify(site or url)}-{stable_tag(url)}"` を生成。
  - `seq.propose(task_id, "task.created", goal=f"extract design tokens from {url}", role="extract", status="extracting", url=url)` を発行。
  - `auto_update_dashboard(seq=seq)` を即時呼び出し。
  - `progress_cb(status, detail)` で `write_progress(task_id, seq.path, status=status, detail=detail, vendor="extract")` および stderr への `[extract] ...` ログ出力を実行。
  - 成功時に `seq.propose(task_id, "extract.ok", status="extracted", ...)` を発行し、`auto_update_dashboard()` を更新。
  - エラー時に `seq.propose(task_id, "extract.error", status="failed", error=str(e), ...)` を発行し、`auto_update_dashboard()` を更新。
  - `seq.stop()` を確実に実行。

---

## Verification Plan

### Automated Tests
- 単体テストの実行:
  ```pwsh
  pytest harness/tests/test_extract_fetch.py harness/tests/test_dashboard.py harness/tests/test_cli.py
  ```
- 新規テストケースの追加:
  - `test_extract_breakpoints_from_css()`: `@media` クエリ（min/max-width, rem/em/px/pt, レベル4レンジ構文）からの数値抽出と単位変換の検証。
  - `test_fetch_rendered_page_auto_extracts_breakpoints()`: CSS から抽出されたブレークポイントによる自動ビューポート決定の検証。
  - `test_dashboard_renders_extracting_and_extracted_status()`: ダッシュボードでの `extracting` / `extracted` 表示・バッジ・プログレスバーの検証。
  - `test_extract_run_emits_ledger_events_and_updates_progress()`: `cmd_extract_run` 実行時の Ledger イベント発行、progress 更新、ログ出力の検証。

### Manual Verification
- モックまたは実URLに対する `super-agent extract run <url>` の実行確認:
  - 標準エラー出力に進捗ログが順次出力されること。
  - `dashboard.html` / `dashboard.md` に `Extracting` → `Extracted` が正しく反映されること。
  - 標準出力に正常な JSON が出力されること。
