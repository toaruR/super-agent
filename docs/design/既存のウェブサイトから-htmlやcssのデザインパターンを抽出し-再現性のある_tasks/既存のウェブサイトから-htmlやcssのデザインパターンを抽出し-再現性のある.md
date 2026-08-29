# タスク分解（decompose 出力）

要求: 既存のウェブサイトから、htmlやcssのデザインパターンを抽出し、再現性のあるデザインプロンプトとそれを補助する仕組みに還元する仕組みを作りたい

タスク数: 10

## 1. T1

- 目標: harness/extract/robots.py を新設し、(1) 指定サイトのrobots.txtを取得・解釈してuser-agent単位でURLの許可/禁止を判定するRobotsChecker、(2) 候補URL群からトップページ・代表的な一覧ページ・代表的な詳細ページに限定してクロール対象を選定するselect_crawl_targets()を実装する。あわせてharness/config/design_extract.yamlを新設し、breakpoints（レスポンシブ検証用のビューポート幅リスト）、crawl（max_pages等robots遵守設定）、verify（視覚的類似度の許容閾値）の初期値を定義する。他パイプライン段階（fetch/verifyなど）はこの設定ファイルを読み込むだけで、本タスクではその他モジュールは作成しない。
- 依存: （なし）
- 触ってよい範囲: harness/extract/robots.py, harness/config/design_extract.yaml, harness/tests/test_extract_robots.py
- 受入基準 (3):
  - `pytest` harness/tests/test_extract_robots.py -k test_disallowed_urls_filtered_by_robots_txt (expect_exit=0)
  - `pytest` harness/tests/test_extract_robots.py -k test_crawl_targets_limited_to_top_listing_detail_pages (expect_exit=0)
  - `pytest` harness/tests/test_extract_robots.py -k test_design_extract_config_has_breakpoints_crawl_verify_keys (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 20)
  - robots.txt の Disallow/Allow ルールを user-agent 単位で正しく解釈し、単純な文字列マッチに頼っていない (配点: 20)
  - クロール対象URLの選定ロジック（トップ/一覧/詳細の代表ページに限定）が明確な基準を持ち、無制限クロールにならないようテストされている (配点: 20)
  - harness/config/design_extract.yaml の初期値が他モジュールから読み込みやすい単純な構造になっている (配点: 15)

## 2. T2

- 目標: harness/extract/fetch.py を新設し、ヘッドレスブラウザ（Playwright等）でレンダリング後のDOM（outerHTML相当）とcomputed styleを、harness/config/design_extract.yamlのbreakpointsで指定された複数ビューポート幅ごとに取得するfetch_rendered_page()を実装する。ブラウザ操作は差し替え可能なドライバ/インターフェース越しに呼び出し、実ブラウザ起動なしにユニットテスト可能な設計にする。取得前にT1のrobots.pyでURLの許可判定を行い、禁止URLはfetchしない。
- 依存: T1
- 触ってよい範囲: harness/extract/fetch.py, harness/tests/test_extract_fetch.py
- 受入基準 (3):
  - `pytest` harness/tests/test_extract_fetch.py -k test_fetches_dom_and_computed_style_per_breakpoint (expect_exit=0)
  - `pytest` harness/tests/test_extract_fetch.py -k test_skips_urls_disallowed_by_robots (expect_exit=0)
  - `pytest` harness/tests/test_extract_fetch.py -k test_browser_driver_is_injectable_and_mockable (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外（harness/extract/robots.py 含む）に一切触れていない (配点: 20)
  - ブラウザ操作が差し替え可能なインターフェース越しに呼ばれており、実ブラウザ起動なしにユニットテスト可能な設計になっている (配点: 20)
  - 複数ブレークポイントでのDOM/computed style取得がbreakpointごとに独立した結果として構造化されている (配点: 20)
  - robots.txtで許可されないURLに対してfetchが実行されないことが明示的に保証されている (配点: 15)

## 3. T3

- 目標: harness/extract/analyze.py を新設し、T2のfetch結果（breakpointごとのDOM/computed style）から再利用可能なUIコンポーネント単位（ボタン・カード・ナビゲーション・フォーム等）でスタイルパターンを抽出するanalyze_components()を実装する。ブレークポイント間のスタイル差分（レスポンシブ差分）も構造化データとして保持する。著作権制約として、抽出結果にはテキスト内容（innerText等）・画像URL・ロゴ等のコンテンツ資産を一切含めず、配色・間隔・タイポグラフィ等の抽象的なスタイル規則のみを残す。
- 依存: T2
- 触ってよい範囲: harness/extract/analyze.py, harness/tests/test_extract_analyze.py
- 受入基準 (3):
  - `pytest` harness/tests/test_extract_analyze.py -k test_extracts_component_types_button_card_nav_form (expect_exit=0)
  - `pytest` harness/tests/test_extract_analyze.py -k test_captures_responsive_style_differences_across_breakpoints (expect_exit=0)
  - `pytest` harness/tests/test_extract_analyze.py -k test_excludes_text_and_image_content_from_extracted_patterns (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 15)
  - ボタン・カード・ナビゲーション・フォーム以外の要素が混入してもクラッシュせず妥当に分類・除外される (配点: 15)
  - 抽出結果にテキスト内容・画像URL・ロゴ等のコンテンツ資産が一切含まれないことがテストで直接検証されている（著作権制約） (配点: 25)
  - ブレークポイント間のスタイル差分が比較可能な構造化データとして保持されている (配点: 20)

## 4. T4

- 目標: harness/extract/tokens.py を新設し、T3のコンポーネント抽出結果をW3C Design Tokens Community Group形式（各トークンが$type/$valueを持つ）のJSONに変換するbuild_design_tokens()を実装する。color/typography/spacing/radius/shadowの各カテゴリを欠落なくマッピングし、コンポーネント単位のグルーピングを保持する。万一入力にテキスト等のコンテンツ情報が含まれていてもトークンJSONには書き出さない防御的実装にする。
- 依存: T3
- 触ってよい範囲: harness/extract/tokens.py, harness/tests/test_extract_tokens.py
- 受入基準 (3):
  - `pytest` harness/tests/test_extract_tokens.py -k test_output_conforms_to_w3c_token_categories (expect_exit=0)
  - `pytest` harness/tests/test_extract_tokens.py -k test_token_values_use_dollar_type_and_dollar_value_keys (expect_exit=0)
  - `pytest` harness/tests/test_extract_tokens.py -k test_component_grouping_preserved_in_token_json (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 15)
  - 出力JSONの各トークンが$type/$valueを持つW3C Design Tokens Community Group形式に準拠している (配点: 25)
  - color/typography/spacing/radius/shadowの各カテゴリが欠落なくマッピングされている (配点: 20)
  - 入力にコンテンツ情報が混入していてもトークンJSONに書き出さない防御的実装になっている (配点: 15)

## 5. T5

- 目標: harness/extract/storage.py を新設し、抽出結果（トークンJSON・生成プロンプトMarkdown・スクリーンショット・メタデータ）をサイト単位・抽出日時単位のスナップショットとして design-extracts/<site>/<timestamp>/ に保存するsave_snapshot()、および保存済みスナップショットを読み込むload_snapshot()を実装する。既存のledger機構とは独立したディレクトリ構成とし、既存スナップショットを上書きしない。トークンJSONと生成プロンプトは常にペアで同一スナップショット内に保存する。
- 依存: T1
- 触ってよい範囲: harness/extract/storage.py, harness/tests/test_extract_storage.py
- 受入基準 (3):
  - `pytest` harness/tests/test_extract_storage.py -k test_creates_versioned_snapshot_directory_per_timestamp (expect_exit=0)
  - `pytest` harness/tests/test_extract_storage.py -k test_saves_tokens_and_prompt_as_paired_files (expect_exit=0)
  - `pytest` harness/tests/test_extract_storage.py -k test_does_not_overwrite_previous_snapshot (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 20)
  - サイトURL単位・タイムスタンプ単位でディレクトリが分離され、既存スナップショットを上書きしない設計になっている (配点: 25)
  - トークンJSONと生成プロンプトが常にペアで同一スナップショット内に保存される (配点: 20)
  - 既存のledger機構（harness/core/ledger.py）とは独立したディレクトリ構成になっている (配点: 10)

## 6. T6

- 目標: harness/extract/generate.py を新設し、T4のトークンJSONを中間表現としてテンプレートエンジン（自然言語生成LLMではなく機械的な文字列組み立て）でMarkdown形式のデザインプロンプトを生成するrender_prompt()を実装する。出力はClaude/Codex/Figmaのいずれでも解釈しやすいよう、トークンカテゴリ・コンポーネント単位でセクション分けする。同一トークンJSON入力に対しては常に同一のMarkdown出力を返す決定的な実装とする。
- 依存: T4
- 触ってよい範囲: harness/extract/generate.py, harness/tests/test_extract_generate.py
- 受入基準 (3):
  - `pytest` harness/tests/test_extract_generate.py -k test_renders_markdown_from_token_categories (expect_exit=0)
  - `pytest` harness/tests/test_extract_generate.py -k test_output_is_deterministic_for_same_tokens (expect_exit=0)
  - `pytest` harness/tests/test_extract_generate.py -k test_includes_component_level_sections (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 20)
  - 自然言語生成LLMではなくテンプレートエンジンによる機械的生成であり、同一トークン入力に対し出力が決定的である (配点: 25)
  - Claude/Codex/Figmaのいずれの利用先でも解釈しやすい構成（見出し・コンポーネント単位のセクション分け等）になっている (配点: 15)
  - トークンJSONに存在しないカテゴリ/コンポーネントについて出力が破綻しない (配点: 15)

## 7. T7

- 目標: harness/extract/verify.py を新設し、生成プロンプトを用いて再生成されたUIのスクリーンショットと元サイトのスクリーンショットの視覚的類似度（知覚的類似度スコア）を計算するcompute_similarity()、およびharness/config/design_extract.yamlのverify.similarity_thresholdと比較して合否判定するverify_against_threshold()を実装する。類似度計算・画像取得は差し替え可能なインターフェース越しに呼び出し、実際のLLM再生成やブラウザレンダリングなしにユニットテスト可能な設計とする。
- 依存: T1
- 触ってよい範囲: harness/extract/verify.py, harness/tests/test_extract_verify.py
- 受入基準 (3):
  - `pytest` harness/tests/test_extract_verify.py -k test_computes_perceptual_similarity_score (expect_exit=0)
  - `pytest` harness/tests/test_extract_verify.py -k test_passes_when_similarity_above_configured_threshold (expect_exit=0)
  - `pytest` harness/tests/test_extract_verify.py -k test_fails_when_similarity_below_configured_threshold (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 20)
  - 類似度スコア計算ロジックが差し替え可能で、実際のLLM呼び出しや画像レンダリングなしにユニットテスト可能である (配点: 20)
  - 閾値がハードコードではなくharness/config/design_extract.yamlの設定値から読み込まれる (配点: 20)
  - 閾値ちょうど境界の値の扱いが明確にテストされている (配点: 15)

## 8. T8

- 目標: harness/extract/refine.py を新設し、生成済みのデザインプロンプト完成後に自然言語の再調整指示（例:『ボタンの角丸をもっと大きく』）を受け取り、T4のトークンJSONの該当カテゴリだけを更新し、T6のrender_prompt()で再生成した新しいプロンプトを返すapply_refinement()を実装する。自然言語解釈のLLM呼び出しはharness.core.invoke経由で行い、テストではモック可能な形に分離する。
- 依存: T4, T6
- 触ってよい範囲: harness/extract/refine.py, harness/tests/test_extract_refine.py
- 受入基準 (3):
  - `pytest` harness/tests/test_extract_refine.py -k test_parses_natural_language_instruction_into_token_delta (expect_exit=0)
  - `pytest` harness/tests/test_extract_refine.py -k test_merges_delta_into_existing_tokens_without_touching_unrelated_categories (expect_exit=0)
  - `pytest` harness/tests/test_extract_refine.py -k test_regenerates_prompt_after_refinement (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外に一切触れていない (配点: 20)
  - LLM呼び出し（harness.core.invoke等）がテストではモック可能な形で分離されており、実ベンダー呼び出しなしにユニットテスト可能である (配点: 20)
  - 自然言語指示の解釈結果がトークンJSONの該当カテゴリだけを更新し、無関係なトークンを破壊しない (配点: 20)
  - 再調整後にトークンJSONとプロンプトの整合性が保たれる（再生成が確実に行われる） (配点: 15)

## 9. T9

- 目標: harness/roles/extract.py を新設し、T2〜T8の各パイプライン段階（fetch/analyze/tokenize/generate/store/verify/refine）を疎結合な独立関数として束ね、一括実行するrun_pipeline()と各段階を個別に呼び出せるインターフェースを提供する。生成された design-extracts/<site>/<timestamp>/prompt.md のパスを返し、既存のplan役割（harness/roles/planner.py 等）へdesign_file入力候補としてそのまま渡せる形にする。既存のplan/drive/decomposeのロジックやシグネチャは一切変更しない。
- 依存: T2, T3, T4, T5, T6, T7, T8
- 触ってよい範囲: harness/roles/extract.py, harness/tests/test_role_extract.py
- 受入基準 (3):
  - `pytest` harness/tests/test_role_extract.py -k test_pipeline_stages_run_in_order_fetch_analyze_tokenize_generate_store (expect_exit=0)
  - `pytest` harness/tests/test_role_extract.py -k test_returns_design_file_path_usable_by_plan (expect_exit=0)
  - `pytest` harness/tests/test_role_extract.py -k test_each_stage_can_be_invoked_independently (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外（各パイプラインモジュール本体等）に一切触れていない (配点: 20)
  - fetch→analyze→tokenize→generate→storeの各段階が個別に呼び出し可能な関数単位で公開されている (配点: 20)
  - plan役割へのdesign_file入力候補として使えるプロンプトファイルパスを返す設計になっている (配点: 20)
  - 既存のplan/drive/decomposeのロジックやシグネチャを変更していない（疎結合が保たれている） (配点: 15)

## 10. T10

- 目標: harness/cli.py に新規サブコマンド extract（および自然言語再調整用の extract refine）を追加し、T9のharness.roles.extract.run_pipeline()/apply_refinement()相当を呼び出す薄いラッパーとして実装する。既存のarchitect/plan/review/drive等のサブコマンドの挙動・オプションには一切影響を与えない。
- 依存: T9
- 触ってよい範囲: harness/cli.py, harness/tests/test_cli.py
- 受入基準 (3):
  - `pytest` harness/tests/test_cli.py -k test_extract_command_registered (expect_exit=0)
  - `pytest` harness/tests/test_cli.py -k test_extract_command_invokes_role_pipeline (expect_exit=0)
  - `pytest` harness/tests/test_cli.py -k test_extract_refine_subcommand_invokes_refine_function (expect_exit=0)
- 採点基準 (rubric, 合格ライン: 80点):
  - acceptance のテストファイル・アサーションを一切変更していない (配点: 25)
  - touch_allow の範囲外（harness/roles/extract.py等）に一切触れていない (配点: 20)
  - extract サブコマンドが既存のarchitect/plan/drive等のサブコマンドの挙動・オプションを変更・破壊していない (配点: 25)
  - extract サブコマンドがharness.roles.extractの薄いラッパーになっており、パイプラインロジックをcli.py側に再実装していない (配点: 20)
  - refine用のサブコマンド/オプションが自然言語指示を引数として受け取れる形になっている (配点: 10)

