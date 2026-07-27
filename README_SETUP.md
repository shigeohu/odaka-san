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

## 未確認事項（推測しない）

- `Network - Burst Level` の `Spikes per Burst per Electrode` の分母定義。`Spikes per Burst` との比が well ごとに約 166.7 / 248.5 / 190.0 と異なり、`Active Area [%]` から出る電極数とも一致しない。ベンダードキュメント要確認。
- `Active Area [%]` の正確な分母。`P005211` の 0.10 % = 1電極から約1000電極と逆算でき、同時記録1024chと整合するが、ファイル内に明記はない。
- `DIV [days]` は参照ファイルでは全て `"N/A"`。
- 適応ループで1サイクルごとに新規 workbook が出るのか、既存 workbook に追記されるのか。現状はどちらも許容する設計にしてある。
- `Number of Configurations > 1` の意味。参照ファイルでは全て `1`。それ以外は fail closed。

## 配置と開始

このリポジトリの root で Claude Code を起動する。

```bash
cd odaka-san
claude
```

最初の依頼例：

```text
CLAUDE.md と config を読み、metrics_data_20260409_150425.xlsx を
tests/fixtures に配置した上で、workbook reader と recording selection の
実装案を提示してください。実装は dry_run 限定とし、
未解決点と必要な fixture テストを明示してください。
```

## 重要

Claude Code はソフトウェアを作るために使い、実験中の判定主体にはしない。実験時には、テスト済みの決定論的 Python プログラムが metrics workbook を解釈して次の刺激段階を決める。

`CLAUDE.md` は指示であり強制機構ではないため、PreToolUse hook も追加した。ただし hook だけに依存せず、Python 側でも環境変数、armed 設定、装置 lock、安全上限、preflight を独立して検証すること。
