# ドキュメント目録（catalog）

super-agent/src の全ドキュメントを網羅する手引き。どこから読めばよいか、
何がどこにあるかを一目でわかることを目的とする。

---

## 入り口

| ファイル | 役割 |
|---|---|
| [`README.md`](../README.md)（src 直下） | **最初に読む**。何か・現状・この目録へのリンク |
| [`docs/catalog.md`](catalog.md) | このファイル。全ドキュメントの目録 |
| [`docs/goals/documentation.md`](goals/documentation.md) | **ドキュメント構造自体**のゴールと評価方法 |

---

## 仕様（設計の正）

| ファイル | 役割 |
|---|---|
| [`docs/spec.md`](spec.md) | **設計の現在の正（単一正源）**。§1〜§10 で全体構成・各層・ルールを定義 |
| [`docs/goals/design.md`](goals/design.md) | 設計の**ゴールと評価ルーブリック**（100点）。spec の「何を良しとするか」 |

---

## 計画・手順

| ファイル | 役割 |
|---|---|
| [`docs/plan.md`](plan.md) | **実装計画**。§9 の一周を「動かしながら確認できる順」に並べたステージ区分 |
| [`docs/usage.md`](usage.md) | **使い方マニュアル**。実際にハーネスを動かすコマンドと手順 |

---

## 証拠（evidence/ — 実測・実証の追加型ログ）

いつ・何を・どう実測したか。上書きせず**日付付きで追記**。検証可能性を残す。

| ファイル | 内容 |
|---|---|
| [`evidence/000-base-evidence.md`](evidence/000-base-evidence.md) | ベンダーCLI の実能力実測（A-1〜A-6）。構造化出力・再開・権限の非対称 |
| [`evidence/0606-s2-validation.md`](evidence/0606-s2-validation.md) | 中心命題（実行と判定の分離で偽fail消滅）の実機検証 |
| [`evidence/0606-n3-large-diff.md`](evidence/0606-n3-large-diff.md) | N=3→N=4 実測と、大きな差分への対処（決定関連性による劣化） |
| [`evidence/0606-permission-control.md`](evidence/0606-permission-control.md) | 起動オプションによる権限制御の実測。**read-only は実行を止めない**という発見 |

---

## 論考（design-notes/ — 決定の理由・記録）

特定論点の深掘りや、設計上の決定・採点の履歴。

| ファイル | 内容 |
|---|---|
| [`design-notes/review-response.md`](design-notes/review-response.md) | 独立レビュー（unsound）への回答。自己採点92点の撤回・改訂案 |
| [`design-notes/scoring.md`](design-notes/scoring.md) | 採点の履歴（v1=52→v2=79→v3=92→v4=89）。各版の失点と改稿 |

---

## 構造の評価

ドキュメント構造自体の出来は [`docs/goals/documentation.md`](goals/documentation.md) のルーブリック（D1〜D6）で評価する。

**自己採点: 100 / 100（合格線 85、致命項目ゼロ）**

| 軸 | 配点 | 得点 | 根拠 |
|---|---|---|---|
| D1 固定名 | 15 | 15 | README/spec/plan/usage + goals/ が固定名で存在 |
| D2 分類一貫性 | 20 | 20 | evidence/・design-notes/・goals/・catalog で括る |
| D3 目録 | 15 | 15 | 本ファイル catalog.md が全網羅 |
| D4 単一正源 | 20 | 20 | spec.md が設計の正、実証は evidence/ へ分離 |
| D5 検証可能性 | 15 | 15 | evidence/ に日付付き追加型ログ |
| D6 リンク整合 | 15 | 15 | 全リンク解決確認済み |

構成: `README.md`（入り口） → `docs/spec.md`（仕様）・`docs/plan.md`（計画）・`docs/usage.md`（使い方）
→ `docs/goals/`（ゴール: documentation=構造 / design=設計）・`docs/evidence/`（実証ログ）・`docs/design-notes/`（論考）・`docs/catalog.md`（目録）。
