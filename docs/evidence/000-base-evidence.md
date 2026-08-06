# 実測エビデンス（設計の前提）

- 測定日: 2026-08-06
- 測定機: 本機（Windows 10 / git-bash）
- 目的: 「大枠設計」の前提を推測でなく実測で固定する。以下はすべて本機で実行した結果。

---

## E-1. 手元に実在するエージェント

| CLI | 実体 | バージョン |
|---|---|---|
| `claude` | `~/.local/bin/claude` | 2.1.223 |
| `codex` | `/c/nvm4w/nodejs/codex` | codex-cli 0.145.0 |
| `agy`（Antigravity） | `~/AppData/Local/agy/bin/agy` | 1.1.8 |
| `hermes` | venv 内 | （本ハーネス自身） |

不在: `gemini` / `cursor-agent` / `opencode` / `copilot` / `kimi` / `aider` / `goose`。

周辺ツール: `tmux` `git` `jq` `sqlite3` `node` `python` `docker` = 有り / **`inotifywait` = 無し**。

> 含意: 「inotify で常駐エージェントを叩き起こす」型の設計は本機では成立しない。後述の通り、そもそも不要。

---

## E-2. 3ベンダーは同一の4プリミティブを既に備える（実測）

| 能力 | claude | codex | agy |
|---|---|---|---|
| ヘッドレス実行 | `-p` | `codex exec` | `-p` |
| 構造化出力 | `--output-format json --json-schema <inline>` | `--json --output-schema <file>` | `--output-format json --json-schema <str\|path>` |
| セッション再開 | `--session-id` / `--resume` | `codex exec resume <thread_id>` | `--conversation <id>` |
| 権限制御 | `--permission-mode` `--tools` `--allowedTools` | `-s read-only\|workspace-write\|danger-full-access` | `--sandbox` `--dangerously-skip-permissions` |
| MCP | `claude mcp add` | `codex mcp add` | `agy plugin` |

### E-2a. 構造化出力の実測結果

同一スキーマ `{verdict:enum[pass,fail], reason:string}` を3者に投げ、3者とも妥当なJSONを返した。

```
claude → .structured_output = {"verdict":"pass","reason":"probe ok"}
codex  → agent_message.text = {"verdict":"pass","reason":"probe ok"}
agy    → .structured_output = {"reason":"probe ok","verdict":"pass"}
```

> **含意: エージェント間を「散文」でなく「型付きJSON」で繋ぐ設計が、追加開発ゼロで今日成立する。**

### E-2b. プロセスを跨いだ記憶の再開（実測）

各CLIに合言葉を覚えさせ、**別プロセスで**再開して問い直した。3者とも正しく想起した。

| CLI | 合言葉 | 再開後の応答 |
|---|---|---|
| claude | ZANBATO | `ZANBATO` |
| codex | NAGINATA | `NAGINATA` |
| agy | KATANA | `KATANA` |

> 含意: 各ベンダーは**自前のセッション記憶を持つ**。共有メモリ設計は「全部を外部化する」のではなく「外部台帳とベンダー内記憶の役割分担」を決める問題になる。

---

## E-3. 実測で見つかったベンダー非対称性（設計に効く）

### A-1. セッションIDの所有者が違う
- claude: **呼び出し側がUUIDを指定できる**（`--session-id <uuid>`）
- codex: **ツール側が採番**（`thread.started` イベントの `thread_id` を拾う必要あり）
- agy: **ツール側が採番**（`conversation_id`）

> 含意: 統制側でIDを事前採番する設計は破綻する。**ハンドル間接化**（内部の論理IDとベンダーIDの対応表を持つ）が必須。

### A-2. `codex exec resume` は `-s/--sandbox` を受け付けない（実測）
```
$ codex exec resume <id> --json -s read-only "..."
error: unexpected argument '-s' found
```
起動時に使えた権限フラグが、再開時には使えない。`-c sandbox_mode="read-only"` での指定が必要。

> 含意: **権限は「起動時に一度」でなく「毎呼び出しで再宣言」する**設計にしないと、再開時に権限がドリフトする。

### A-3. 構造化出力は「複数回」出うる（実測）
codex のレビュー実行で `agent_message` が2件出た。

| item | 内容 |
|---|---|
| `item_0` | `{"verdict":"fail", "findings":["これから調べます…"], "tested_command":""}` ← **未着手の空返答** |
| `item_7` | `{"verdict":"fail", "findings":["...python が見つからない..."], "tested_command":"python -u .\\fizz.py"}` ← 本物 |

> 含意: JSONL を読むとき **最初の agent_message を採用すると誤動作する。必ず最後を採る。**

### A-4. コスト計器の単位が違う
- claude: `total_cost_usd` を返す（USD直値）
- codex / agy: トークン数のみ（USD無し）

> 含意: 予算制御は**トークンに正規化**し、価格表はハーネス側で持つ。

### A-5. 同じ `--json-schema` でも渡し方が逆（後日実測 / `docs/evidence/0606-n3-large-diff.md` §6）

| ベンダー | スキーマの渡し方 |
|---|---|
| claude | **JSON本文をインラインで**渡す。ファイルパスを渡すと `Error: --json-schema is not valid JSON` |
| codex | **ファイルパスを** `--output-schema` に渡す |

```bash
claude -p "$PROMPT" --output-format json --json-schema "$(cat schema.json)"   # 本文
codex exec --json --output-schema schema.json "$PROMPT"                       # パス
```

> 含意: 「同じ機能がある」ことと「同じ呼び方ができる」ことは別。
> §4.1 のアダプタ宣言は、フラグ名だけでなく**引数の意味（本文かパスか）**まで宣言する必要がある。

### A-6. ★「read-only」権限は実行を止めない（後日実測 / `docs/evidence/0606-permission-control.md` §4, §7）

**実測（「実行しなければ答えられない値」＝ランダムファイルの SHA-256 で検証）**:

| 設定 | 実行されたか |
|---|---|
| claude `--disallowedTools "Bash"` | **✅ 実行された**（`PowerShell` ツール経由） |
| claude `--allowedTools "Read,Grep,Glob"` のみ | **✅ 実行された**（許可外の `Bash` が使われた。強制力なし） |
| claude `--permission-mode plan` | ❌ 阻止（有効。ただし Write を使う） |
| codex `--sandbox read-only` | **✅ 実行された**。防ぐのは**書き込みのみ** |
| agy `--mode plan --add-dir <worktree>` | ❌ 阻止（3者中最も厳格） |

> **含意（致命的）**: 判定独立性は「read-only にする」ことでは担保できない。
> ベンダーの権限フラグは**多層防御の一枚**に過ぎず、実行を本当に止められるのは
> agy の `--mode plan` や OS レベルの隔離だけである。
> したがって独立性を担保するのは**§7.2 の裁定器**（レビュアの実行結果を読まない）であり、
> 権限ではない。§8.3・§10(S7) も参照。

---

## E-4. ★最重要: 異ベンダー引き継ぎの実測と「偽の不合格」

claude に実装させ、**codex にレビューさせる**引き継ぎを実測した。

**手順1（claude）**: `fizz.py` を新規作成 → 成功（2ターン, `total_cost_usd = 0.141`）。内容は標準的なFizzBuzzで**論理的に正しい**。

**手順2（codex）**: 同ファイルを読み、**実行して**検証し判定せよ、と指示。結果:

```json
{"verdict":"fail",
 "findings":["The source appears logically correct, but execution could not be verified
              because the configured Python interpreter is missing:
              No Python at '...\\uv\\python\\cpython-3.11.11-windows-x86_64-none\\python.exe'."],
 "tested_command":"python -u .\\fizz.py"}
```

**手順3（検算）**: 同じファイルを筆者のシェルで実行 →
```
['1','2','Fizz','4','Buzz','Fizz','7','8','Fizz','Buzz','11','Fizz','13','14','FizzBuzz']
```
**正常動作**。さらに codex が「無い」と言ったパスは、筆者のシェルからは**存在が確認できた**。

### この1件が意味すること

| 事実 | 判定 |
|---|---|
| 成果物の品質 | **正しい** |
| レビュアの判定 | **fail** |
| 誤判定の原因 | 成果物ではなく**レビュア側の実行環境/サンドボックス** |

> **「エージェントAの成果物をエージェントBがレビューする」という素朴な設計は、成果物の品質ではなくレビュアの環境を測ってしまう。**
> 本設計が最優先で解くべき問題はここ。プラミング（配管）ではない。

---

## E-5. コストの実測値

| 実行 | 入力トークン | 出力 | 備考 |
|---|---|---|---|
| claude 疎通 | 10,500(cache write) | 389 | $0.0688 |
| claude 実装(fizz.py) | — | — | $0.141 / 2ターン |
| codex 疎通 | 13,028 | 21 | — |
| agy 疎通 | 16,459 | 673 | thinking 647 |
| **codex レビュー（16行のファイル1本）** | **138,705**（cache 105,472） | 975 | reasoning 329 |

> 含意: **16行のレビューに13.8万トークン**。「全タスクを全員でレビュー」は経済的に破綻する。
> 文脈予算とリスク階層化（安いタスクにレビュアを付けない）が設計要件になる。

---

## 再現方法

```bash
cd D:/vagrant/harnesses/super-agent/probe
# 構造化出力
claude -p "..." --output-format json --json-schema "$(cat schema.json)" --tools ""
codex exec --json --output-schema schema.json --skip-git-repo-check -s read-only "..."
agy   -p "..." --output-format json --json-schema schema.json
# 引き継ぎ再現は probe/ws/ を参照（fizz.py は claude 生成物のまま残置）
```
