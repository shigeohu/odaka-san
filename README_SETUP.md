# MAXONE Claude Code スターターパック

## 構成

- `CLAUDE.md`：プロジェクト全体の常時ルール
- `config/experiment.yaml`：path、閾値、電圧刻み、停止条件
- `config/metrics_schema.yaml`：metrics workbook（.xlsx）の解釈、レコーディング選択、平均発火頻度の取得先
- `config/stimulation_protocols.yaml`：電極、波形、パルス条件、安全上限
- `.claude/settings.json`：Claude Code のプロジェクト設定
- `.claude/hooks/block_live_maxone.py`：Claude Code からの armed 実行を遮断する hook
- `metrics_data_20260409_150425.xlsx`：MaxLab Live の metrics エクスポート実物（参照ファイル）

## Skill.md を付けていない理由

今回はプロジェクト全体で常に必要な規則と、実験ごとに変わる数値設定が中心である。そのため、常時ルールは `CLAUDE.md`、数値・path・workbook 構造は YAML、強制的な実機遮断は settings と hook に分離した。

Skill は、後に「新しい metrics 形式を取り込む」「dry-run 解析レポートを作る」などの再利用可能な手順を追加するときに有用だが、現段階では必須ではない。

## 確定した制御則

1. cycle 0 は 0 V のベースライン記録で、刺激シーケンスを送信しない。
2. MaxLab Live が metrics workbook を指定 path に出力する。
3. workbook 完成を確認後、**当該サイクルのレコーディングを1件だけ特定**し、平均発火頻度を読む。
4. 平均発火頻度が基準値未満なら、刺激強度を1段階だけ上げる。
5. 基準値以上なら正常終了する。
6. 応答は数分でよいため、Python による metrics ファイル監視方式を使用する。

## 入力ファイル形式（重要）

入力は **CSV ではなく Excel workbook** である。詳細は `CLAUDE.md` §2 を参照。

- ファイル名：`metrics_data_<YYYYMMDD>_<HHMMSS>.xlsx`
- ファイル名の時刻は**エクスポート実行時刻**であり、記録時刻ではない
- シートは5枚：`Meta Data` / `Analysis Parameters` / `Activity - Well Level` / `Network - Well Level` / `Network - Burst Level`
- `Instance` が全シートの結合キー。1レコーディング = 2 Instance（Activity Analysis と ISI-N Burst Detector）
- 平均発火頻度は `Activity - Well Level` の `Mean Firing Rate [Hz]`。**既に well レベルで集約済みのスカラー**で、行集約は不要
- 欠損マーカーは文字列 `"N/A"`（空セルではない）。数値列に混入する

### 1つの workbook に複数レコーディングが入る

参照ファイルは7枚の別チップ（P005163, P005157, P005124, P005232, P005211, P005169, P005190）を各約300秒記録したバッチエクスポートである。同一チップの時系列ではない。

実験運用時も同様に複数レコーディングが入りうるため、**「どのレコーディングがこのサイクルのものか」の特定は安全ゲートである**。該当0件も複数件も FAULT とし、「1行しかないからそれ」「最初の行」「最新の行」といったフォールバックは禁止する。

### 特に注意すべき実データ上の罠

1. **単一電極ケース**：`P005211` は `Mean Firing Rate 0.19 Hz` / `Active Area 0.10 %` で、std・CV・percentile が全て `"N/A"`。有効電極が1本しかない。値は正常な float なので単純な数値チェックは通過するが、well の代表値としては無意味。`Active Area` と有効電極数の下限 QC が必須。
2. **`Network - Burst Level` の無名 index 列**：A列にヘッダなしの `0..N-1` 連番がある。列位置で読むと1列ずれる。ヘッダ名で読むこと。
3. **`Network - Burst Level` のメタ列分散**：各 Instance ブロックの2行目にだけ `Well Group Name` / `Color` / `Control` / `Assay Tag` が入り、`Date` / `Wellplate ID` / `Well Number` / `Assay Run ID` はその2行目以外に入る。メタは必ず `Meta Data` シートから引くこと。
4. **バーストゼロでも行が出る**：`P005163` はバースト0件だがプレースホルダ2行が出力される。行数はバースト数ではない。
5. **記録時間は300秒ちょうどではない**：`Duration per Configuration [s]` は 300.018〜300.084 と変動する。固定値を埋め込まない。
6. **Instance の偶奇に依存しない**：参照ファイルでは Activity が奇数・burst が偶数だが、これは観測された偶然であり保証ではない。`Analysis Parameters.Analysis Type` で判定すること。

## 実機前に設定する具体値

方針は確定しているが、以下の数値・名称は未提示のため `null` としている。Claude Code に推測させない。

### `config/experiment.yaml`

- `paths.metrics_watch_directory`
- `adaptive_policy.reference_threshold_hz`
- `adaptive_policy.amplitude_step_mV`
- `adaptive_policy.maximum_amplitude_mV`
- `adaptive_policy.maximum_cycles`
- `adaptive_policy.maximum_total_experiment_minutes`

### `config/metrics_schema.yaml`

- `recording_selection.strategy` と対応するキー（`folder_path` または `wellplate_id` + `well_number` + `assay_run_id`）
- `expected_acquisition.duration_seconds_min` / `duration_seconds_max`
- `quality_control.minimum_active_area_percent`
- `quality_control.minimum_active_electrodes`

シート名・列名・`expected_acquisition` の取得系設定（20000 Hz / Gain 512 / LSB 6.294 µV / HPF 1 Hz）と `expected_analysis_parameters`（0.1 Hz / 20 µV / 200 ms / N=80）は参照ファイルの実測値を投入済み。実験条件が異なる場合は更新すること。

### `config/stimulation_protocols.yaml`

- MaxLab Live / API バージョン
- 刺激電極と記録電極
- 位相時間、パルス数、パルス間隔
- 独立した最大刺激強度
- 累積刺激上限
- 緊急停止手順

## ベンダーマニュアルで確定した定義

MaxLab Live Manual v25.1 により以下が確定した（CLAUDE.md §4.1）。

- **`Active Area [%]`** = 活性電極数 ÷ **記録電極総数**。活性電極の定義は「発火率が `Firing Rate Threshold [Hz]` を超え、かつ90パーセンタイル spike 振幅が `Amplitude Threshold [µV]` を超える」。
- **分母は記録ごと**（そのレコーディングで routing した電極数、1コンフィグあたり**最大1020**）。workbook に出力されないため、assay 側の設定から与える必要がある。（`1024` はマニュアル中では `LSB` の 10bit ADC 分解能であり、電極数ではない。）
- **Firing Rate の要約統計は「活性電極のみ」を対象**とする。全記録電極の平均ではない。
- **`Instance`** =「Well Plate ID + Well Number + Assay Run ID + Analysis Trial」の一意な組み合わせごとに割り当てられる。偶奇は定義に含まれない。
- **`Number of Configurations`** = 1記録ファイル内のコンフィグ数（例：7X Sparse ActivityScan なら7）。Network assay は `1`。
- **`Well Number`** は MaxOne では常に1。
- **`DIV [days]`** は Well Editor の plating date から自動計算される。`"N/A"` は未入力を意味し、異常ではない。

### 刺激の文書化された上限（CLAUDE.md §4.3）

| パラメータ | 範囲 | 既定 |
|---|---|---|
| Stimulation Amplitude | `0`–`1000` mV（assay: `3`–`1000`） | `0` / `200` |
| Stimulation Pulse Phase | `100`–`1000` µs, step `50` | `200` |
| Number of Pulses per Burst | `0`–`10000` | `1` |
| Interpulse Interval | `0`–`10000` ms | `300` |
| Number of Bursts | `0`–`99` | `1` |
| Interburst Interval | `0.01`–`600` s | `1.00` |
| 同時刺激チャネル数 | `32` | — |

> 「MaxOne+ の PEDOT 電極では推奨最大刺激強度は **600 mV**。超えると電極の完全性と実験再現性を損なう可能性がある。」

パルスは電圧・biphasic で**正相が先**。設定は電圧極性の語（`positive_then_negative`）で表現している。anodic/cathodic への対応付けは `voltage_amplifier_inverts_polarity` に依存し未検証のため、あえて符号化していない。

これらは `config/stimulation_protocols.yaml` の `documented_device_limits` に取り込み、設定値がこれを超える場合は設定エラーとして拒否する。

## 未確認事項（推測しない）

- **ベンダー Python API**。今回のマニュアルは GUI マニュアルであり、`maxlab` モジュール、`mxwserver`、DAC コード、volts→bits 較正の記載がない。**`adapters/real.py` を塞いでいるのはこれ**。
- **電荷注入容量と安全振幅の根拠**。マニュアルは MaxWell の別文書 *Electrical Stimulation Guide*（`mxw.bio/MxW_Doc_Electrical_Stimulation_Guide`）に委ねている。認証付きファイル共有のため未取得。
- **`Spikes per Burst per Electrode`**。マニュアルは「記録電極総数で正規化」と定義するが、参照ファイルでは除数が約 166.8 / 247.8 / 190.2 となり、同じレコーディングの `Active Area [%]` が示す約1020と一致しない。**文書の定義がデータを再現しない**。判定経路では使用しない。
- 適応ループで1サイクルごとに新規 workbook が出るのか、既存 workbook に追記されるのか。現状はどちらも許容する設計にしてある。

## 実装

`maxone_loop` パッケージとして実装済み。実機アダプタ以外は完成している。

```bash
pip install -e ".[dev]"

python -m pytest
python -m maxone_loop.cli validate-config --config config/experiment.yaml
python -m maxone_loop.cli inspect --config config/experiment.yaml metrics_data_20260409_150425.xlsx
python -m maxone_loop.cli replay --config config/experiment.yaml --input tests/fixtures
python -m maxone_loop.cli run --config config/experiment.yaml --mode dry_run
```

`validate-config` と `inspect` はそのまま動く。`replay` と `run` は上記の `null` を埋めるまで意図的に非ゼロ終了する（`recording_selection.strategy` と `paths.metrics_watch_directory` が未設定のため）。推測しないことが設計上の正しい挙動である。

### モジュール構成

| モジュール | 役割 |
|---|---|
| `config/models.py` | 型付き設定。未知キーはエラー |
| `config/loader.py` | 3ファイル読み込み、`*.local.yaml` 上書き、ハッシュ |
| `metrics/workbook.py` | read-only workbook reader（`"N/A"`、ヘッダ名解決、無名index列） |
| `metrics/selection.py` | レコーディングを1件だけ特定 |
| `metrics/extract.py` | スカラー1個の取得と来歴記録 |
| `metrics/qc.py` | 品質ゲート（単一電極ケースを含む） |
| `policy.py` | 純粋な判定関数（I/O・時計・ハードウェアなし） |
| `state.py` | 状態機械と再起動時の解決 |
| `ledger.py` | SQLite 来歴、レコーディング冪等性、装置 lock |
| `watcher.py` | 安定ファイル検出 |
| `adapters/` | simulated / dry_run / real |
| `orchestrator.py` | サイクル制御 |
| `synthetic.py` | 合成 workbook（simulate モードとテスト用） |
| `cli.py` | `validate-config` / `inspect` / `replay` / `run` |

### 実機アダプタは未実装

`adapters/real.py` は意図的に未実装である。MaxLab Live の実体（`maxlab` パッケージ、ローカルの example、インストール版に対応した API ドキュメント）が本リポジトリに存在せず、CLAUDE.md §4 が未文書化の API 挙動の推測を禁じているため。各メソッドは `AdapterError` を送出する。ただし `safe_shutdown` は例外処理中に呼ばれるため決して raise しない。

実装契約は `IMPLEMENTATION_CONTRACT` として同ファイルにデータで保持してある。取得マシン上で、インストール済み MaxLab のバージョンに対して実装し、armed 実行前にレビューすること。

### 冪等性の境界

workbook のハッシュではなく **レコーディング識別子**（`Meta Data.Folder Path`）が境界である。`consumed_recordings` テーブルの主キーが `(experiment_id, folder_path)` で、orchestrator は**刺激の前に**識別子を確保する。確保と書き込みの間でクラッシュした場合、1サイクルを失うが、刺激を繰り返すことは起きない。

## 配置と開始

このリポジトリの root で Claude Code を起動する。

```bash
cd odaka-san
claude
```

## 重要

Claude Code はソフトウェアを作るために使い、実験中の判定主体にはしない。実験時には、テスト済みの決定論的 Python プログラムが metrics workbook を解釈して次の刺激段階を決める。

`CLAUDE.md` は指示であり強制機構ではないため、PreToolUse hook も追加した。ただし hook だけに依存せず、Python 側でも環境変数、armed 設定、装置 lock、安全上限、preflight を独立して検証すること。
