# タスク分解（decompose 出力）

要求: super-agent extract のUI再現性向上・決定論的アセット自動生成（tokens.css / components.css / skeleton.html）および UI生成兼用Skill（reproduce-ui）の導入

タスク数: 4

## 1. T1

- 目標: harness/extract/generate.py に、(1) W3C Design Tokens または DesignSystemAnalysis から決定的な :root CSSカスタムプロパティを生成する render_tokens_css()、(2) 抽出されたボタン・カード・ナビ・フォーム等の具現化CSSクラスルールを生成する render_components_css()、(3) Webフォント読み込みリンクやリセットCSS、スタイル枠組みを完備したHTMLテンプレート骨格を生成する render_skeleton_html() を追加実装する。あわせて harness/tests/test_extract_generate.py にそれぞれの単体テストを追加する。
- 依存: （なし）
- 触ってよい範囲: harness/extract/generate.py, harness/tests/test_extract_generate.py
- 受入基準 (3):
  - `pytest` harness/tests/test_extract_generate.py -k test_render_tokens_css (expect_exit=0)
  - `pytest` harness/tests/test_extract_generate.py -k test_render_components_css (expect_exit=0)
  - `pytest` harness/tests/test_extract_generate.py -k test_render_skeleton_html (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 20)
  - render_tokens_css() が決定論的な :root { ... } CSSカスタムプロパティ文字列を出力する (配点: 20)
  - render_components_css() が .btn-primary, .card-surface 等の具現化クラスを出力する (配点: 20)
  - render_skeleton_html() がフォントリンク・リセットCSS・スタイル枠組みを含む完全なHTMLを出力する (配点: 15)

## 2. T2

- 目標: harness/extract/storage.py に、定数 TOKENS_CSS_FILENAME ("tokens.css"), COMPONENTS_CSS_FILENAME ("components.css"), SKELETON_HTML_FILENAME ("skeleton.html") を追加定義し、save_snapshot() でこれらのCSS/HTMLコードアセットが渡された際にスナップショットディレクトリ内に保存する処理を追加する。あわせて harness/tests/test_extract_storage.py に保存機能の単体テストを追加する。
- 依存: T1
- 触ってよい範囲: harness/extract/storage.py, harness/tests/test_extract_storage.py
- 受入基準 (2):
  - `pytest` harness/tests/test_extract_storage.py -k test_saves_css_and_skeleton_html_assets (expect_exit=0)
  - `pytest` harness/tests/test_extract_storage.py -k test_storage_constants_defined (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 20)
  - TOKENS_CSS_FILENAME, COMPONENTS_CSS_FILENAME, SKELETON_HTML_FILENAME が定義されている (配点: 25)
  - save_snapshot() が tokens.css, components.css, skeleton.html を指定時に正しくスナップショットへ保存する (配点: 30)

## 3. T3

- 目標: harness/roles/extract.py の run_pipeline() を拡張し、render_tokens_css(), render_components_css(), render_skeleton_html() を呼び出してスナップショットに保存する処理を組み込む。また PipelineResult データクラスに tokens_css, components_css, skeleton_html フィールドおよび対応するパスプロパティを追加する。あわせて harness/tests/test_role_extract.py にパイプライン統合テストを追加する。
- 依存: T2
- 触ってよい範囲: harness/roles/extract.py, harness/tests/test_role_extract.py
- 受入基準 (2):
  - `pytest` harness/tests/test_role_extract.py -k test_pipeline_generates_and_stores_css_and_skeleton_html (expect_exit=0)
  - `pytest` harness/tests/test_role_extract.py -k test_pipeline_result_has_css_and_skeleton_properties (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 20)
  - run_pipeline() が実行時に tokens.css, components.css, skeleton.html を生成・保存する (配点: 30)
  - PipelineResult の後方互換性が維持されている (配点: 25)

## 4. T4

- 目標: UI生成兼用スキル（reproduce-ui）を .agents/skills/reproduce-ui/SKILL.md および .claude/skills/reproduce-ui/SKILL.md に新設する。スナップショット内の skeleton.html, tokens.css, components.css, DESIGN.md を読み込み、指定された要素リストをセマンティックに配置・クラスバインディングする手順、および自己検証ルール（Self-Audit）を明記する。
- 依存: T3
- 触ってよい範囲: .agents/skills/reproduce-ui/SKILL.md, .claude/skills/reproduce-ui/SKILL.md, harness/tests/test_role_extract.py
- 受入基準 (1):
  - `pytest` harness/tests/test_role_extract.py -k test_reproduce_ui_skill_files_exist (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 20)
  - .agents/skills/reproduce-ui/SKILL.md と .claude/skills/reproduce-ui/SKILL.md が同一内容で作成されている (配点: 30)
  - SKILL.md に明確なYAMLフロントマター、実行手順（Step 1〜4）、自己検証チェックリストが含まれている (配点: 25)
