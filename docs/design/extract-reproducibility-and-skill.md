# 設計: super-agent extract のUI再現性向上・決定論的アセット自動生成（tokens.css / components.css / skeleton.html）および UI生成兼用Skill（reproduce-ui）の導入

## 1. 概要と目標

### 背景と目的
`super-agent extract run <url>` で抽出した `DESIGN.md` / `prompt.md` だけでは、LLM（Claude, GPT, Gemini等）に「このmdを使って[要素リスト]を持ったabout.htmlをつくって」と指示した際に、モデルごとの解釈ブレ（Model Drift）やCSSハードコードによりデザインシステムの再現性が損なわれる課題がある。
本設計では、**「抽出側（Harness）での決定的なコードアセット（`tokens.css`, `components.css`, `skeleton.html`）自動生成」** と **「エージェント側のUI生成兼用Skill（`reproduce-ui`）」** を組み合わせたハイブリッド方式（二層構造）により、どんなモデルでも安定して高精度なUIを再現できる仕組みを構築する。

---

## 2. アーキテクチャと設計方針

### 2.1 ハイブリッド方式（二層構造）
1. **抽出側（Harness / Extract ロール）**:
   - `super-agent extract run` 実行時に、スナップショットディレクトリ（`design-extracts/<site>/<timestamp>/`）へ以下の3アセットを追加出力する：
     - `tokens.css`: `:root` CSSカスタムプロパティ（色・フォント・余白・角丸・影・サーフェス）
     - `components.css`: 抽出されたボタン（`.btn-primary` 等）、カード（`.card-surface` 等）、ナビ（`.nav-container` 等）、フォーム入力（`.input-text` 等）の具現化CSSクラス集
     - `skeleton.html`: Webフォント読み込み `<link>`、リセットCSS、`<style>` 領域を完備したテンプレートHTML
2. **エージェント側（Skill）**:
   - 汎用兼用Skill（`.agents/skills/reproduce-ui/SKILL.md` および `.claude/skills/reproduce-ui/SKILL.md`）を1つ新設。
   - LLMにゼロからCSSプロパティを考えさせず、スナップショット内の `skeleton.html` と `components.css` を土台として読み込ませ、ユーザー指定の「要素リスト」をセマンティックに配置・クラスバインディングさせる。
   - 自己検証（Self-Audit）ルールによりハードコード色の残存や崩れを排除する。

---

## 3. 実装詳細仕様

### 3.1 Harness 側（Extract パイプライン拡張）

#### `harness/extract/generate.py`
- `render_tokens_css(tokens: Dict[str, Any], design_system: Optional[DesignSystemAnalysis] = None) -> str`
  - W3C Design Tokens または DesignSystemAnalysis から決定的な `:root { ... }` CSSカスタムプロパティ文字列を生成。
- `render_components_css(design_system: Optional[DesignSystemAnalysis] = None) -> str`
  - 主要コンポーネント（`.btn-primary`, `.btn-secondary`, `.card-surface`, `.nav-container`, `.input-field`, `.badge` 等）の具現化CSSルールを生成。
- `render_skeleton_html(metadata: Dict[str, Any], design_system: Optional[DesignSystemAnalysis] = None) -> str`
  - 抽出されたフォント情報（Google Fontsリンク等）とリセットCSSを含む完全なHTMLテンプレート骨格を生成。

#### `harness/extract/storage.py`
- 定数 `TOKENS_CSS_FILENAME = "tokens.css"`, `COMPONENTS_CSS_FILENAME = "components.css"`, `SKELETON_HTML_FILENAME = "skeleton.html"` を定義。
- `save_snapshot()` に `tokens_css`, `components_css`, `skeleton_html` の保存処理を追加（指定があればファイル出力）。

#### `harness/roles/extract.py`
- `run_pipeline()` で `render_tokens_css`, `render_components_css`, `render_skeleton_html` を呼び出し、スナップショットディレクトリに保存。
- `PipelineResult` に `tokens_css_path`, `components_css_path`, `skeleton_html_path` プロパティを追加。

#### 単体テスト (`harness/tests/`)
- `test_extract_generate.py` および `test_role_extract.py` に `tokens.css`, `components.css`, `skeleton.html` の生成・保存テストを追加。

---

### 3.2 Skill 側（`reproduce-ui` スキル）

#### `.agents/skills/reproduce-ui/SKILL.md` & `.claude/skills/reproduce-ui/SKILL.md`
- フロントマター（`name: reproduce-ui`, `description: ...`）
- 実行手順（Step 1: アセットロード ➔ Step 2: 要素リストのマッピング ➔ Step 3: HTML構築 ➔ Step 4: Self-Audit）
- 厳格な制約（Vanilla HTML5/CSS、CSS変数利用徹底、ハードコード色禁止、レスポンシブ対応）
