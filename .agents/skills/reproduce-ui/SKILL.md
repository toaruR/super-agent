---
name: reproduce-ui
description: UI reproduction skill using snapshot assets (skeleton.html, tokens.css, components.css, DESIGN.md) to deterministically generate HTML/CSS pages matching the extracted design system without model drift or hardcoded colors.
---

# UI Reproduce Skill (`reproduce-ui`)

本スキルは、`super-agent extract` パイプラインによって抽出されたスナップショットアセット（`skeleton.html`, `tokens.css`, `components.css`, `DESIGN.md`）を活用し、モデルごとの解釈ブレ（Model Drift）やCSSハードコードを完全に防ぎつつ、指定された要素リストを含むUI（HTML/CSS）を決定論的に高精度再現・生成するための統合ガイドです。

---

## 1. 概要と適用要件

- **入力**: スナップショットディレクトリ（`design-extracts/<site>/<timestamp>/`）およびユーザー指定の「要素リスト」
- **出力**: セマンティックでアクセシブルな Vanilla HTML5 / CSS Web ページ（例: `about.html`, `index.html` 等）
- **前提条件**: Vanilla HTML5/CSS 構成とし、ハードコード色を排除し、抽出された CSS カスタムプロパティ（`:root`）と具現化コンポーネントクラス（`components.css`）を徹底利用すること。

---

## 2. 実行手順 (Step 1 〜 Step 4)

### Step 1: アセットロード (Asset Loading)
スナップショットディレクトリ（`design-extracts/<site>/<timestamp>/`）から以下の 4 つのアセットを読み込み、デザインシステムの定義と骨格を把握する。

1. `skeleton.html`: Google Fonts `<link>`、CSSリセット、`<style>` ブロック、`<link rel="stylesheet">` の基本骨格を読み込む。
2. `tokens.css`: `:root` に定義された CSS カスタムプロパティ（色 `--color-*`, フォント `--font-*`, 余白 `--spacing-*`, 角丸 `--radius-*`, 影 `--shadow-*`, サーフェス `--surface-*`）を確認する。
3. `components.css`: 抽出済みの具現化コンポーネントクラス（`.btn-primary`, `.btn-secondary`, `.card-surface`, `.nav-container`, `.input-field`, `.badge` 等）を確認する。
4. `DESIGN.md`: トークンロール、カラーパレット、タイポグラフィ、Do's & Don'ts、アクセシビリティ・レスポンシブデザインガイドラインを確認する。

### Step 2: 要素リストのマッピング (Element List Mapping)
ユーザーが要求する「要素リスト」（例: ヘッダーナビゲーション、ヒーローセクション、カードグリッド、お問い合わせフォーム、フッター等）を整理し、抽出された `components.css` のクラスおよび `tokens.css` の変数との対応関係をマッピングする。

- ボタン要素 ➔ `.btn-primary` / `.btn-secondary`
- カード/コンテナ要素 ➔ `.card-surface`
- ナビゲーション ➔ `.nav-container`
- テキスト/検索入力 ➔ `.input-field`
- バッジ/タグ ➔ `.badge`
- 既存コンポーネントクラスで不足する要素 ➔ `tokens.css` 内の CSS 変数 (`var(--...)`) のみを用いて追加CSSルールを作成する。

### Step 3: HTML構築 & クラスバインディング (HTML Construction & Class Binding)
`skeleton.html` をベーステンプレートとして使用し、HTML 構造を構築する。

1. `<head>` 内で `tokens.css` と `components.css` を適切に参照・読み込む。
2. セマンティック HTML5 タグ（`<header>`, `<nav>`, `<main>`, `<section>`, `<article>`, `<footer>` 等）を使用して要素リストを配置する。
3. マッピングした CSS コンポーネントクラス（`.btn-primary`, `.card-surface` 等）を各要素にバインディングする。
4. 新規レイアウト調整が必要な場合は、インライン色指定やハードコード値を避け、`style="gap: var(--spacing-unit, 8px);"` や追加スタイルシート内で `:root` 変数を組み合わせて記述する。

### Step 4: 自己検証 (Self-Audit)
生成された HTML/CSS が受入基準を満たしているか、以下の「自己検証チェックリスト」に照らし合わせて厳密に検証する。

---

## 3. 自己検証チェックリスト (Self-Audit Checklist)

生成されたコードが出力される前に、以下の項目がすべてパスしていることを確認してください。

- [ ] **1. Vanilla HTML5/CSS 遵守**: フレームワーク（React/Vue/Tailwind等）に依存せず、純粋な HTML5 と CSS で構築されているか？
- [ ] **2. スナップショットアセットの読み込み**: `skeleton.html`, `tokens.css`, `components.css`, `DESIGN.md` の定義が正しく反映されているか？
- [ ] **3. ハードコード色の禁止**: `#ffffff`, `#000000`, `rgb(...)` などのハードコードされた色・スタイルが直書きされておらず、すべて `tokens.css` の `var(--color-*)` や `var(--surface-*)` を使用しているか？
- [ ] **4. コンポーネントクラスの優先利用**: `components.css` に存在するクラス（`.btn-primary`, `.card-surface`, `.nav-container` 等）が優先してバインディングされているか？
- [ ] **5. セマンティックHTML構造**: `<div>` の濫用を避け、`<header>`, `<main>`, `<section>`, `<nav>`, `<footer>` などの適切な HTML5 タグが使用されているか？
- [ ] **6. レスポンシブ対応**: ビューポートサイズの変化に対応した柔軟なレイアウト（Flexbox / CSS Grid / 相対単位）になっているか？
- [ ] **7. 意図した要素リストの網羅**: ユーザーが指定したすべての要件・要素リストが漏れなく配置されているか？
- [ ] **8. variantカタログの再利用**: 新規コンポーネントを追加する際、`components.css` に定義済みの variant（`skeleton.html` の `<!-- Variant: ... -->` コメントや `.btn-primary`/`.btn-secondary`/`.btn-ghost` 等、存在するもの）のいずれかを選択しているか、それらの `var()` の組み合わせのみで構成しているか？ `tokens.css`/`components.css` に存在しない値（px, hex, 独自の色相等）を新規に創作していないか？
- [ ] **9. 意味役割に基づく再利用**: 既存ページを編集する際、変更対象と同じ意味役割（semantic role。`skeleton.html` の `<!-- Variant: 名前 (role) -->` コメントや `DESIGN.md` のコンポーネント説明に記載）を持つ既存variantを優先して再利用し、無関係な新規スタイルを持ち込んでいないか？
