# MAXONE Claude Codeスターターパック

## 構成

- `CLAUDE.md`：プロジェクト全体の常時ルール
- `config/experiment.yaml`：path、閾値、電圧刻み、停止条件
- `config/csv_schema.yaml`：CSV列、単位、平均発火頻度の計算法
- `config/stimulation_protocols.yaml`：電極、波形、パルス条件、安全上限
- `.claude/settings.json`：Claude Codeのプロジェクト設定
- `.claude/hooks/block_live_maxone.py`：Claude Codeからのarmed実行を遮断するhook

## Skill.mdを付けていない理由

今回はプロジェクト全体で常に必要な規則と、実験ごとに変わる数値設定が中心である。そのため、常時ルールは`CLAUDE.md`、数値・path・CSV構造はYAML、強制的な実機遮断はsettingsとhookに分離した。

Skillは、後に「新しいCSV形式を取り込む」「dry-run解析レポートを作る」などの再利用可能な手順を追加するときに有用だが、現段階では必須ではない。

## 確定した制御則

1. cycle 0は0 Vのベースライン記録で、刺激シーケンスを送信しない。
2. CSVが指定pathに自動生成される。
3. CSV完成を確認後、平均発火頻度を計算する。
4. 平均発火頻度が基準値未満なら、刺激強度を1段階だけ上げる。
5. 基準値以上なら正常終了する。
6. 応答は数分でよいため、PythonによるCSV監視方式を使用する。

## 実機前に設定する具体値

方針は確定しているが、以下の数値・名称は未提示のため`null`としている。Claude Codeに推測させない。

### `config/experiment.yaml`

- `paths.csv_watch_directory`
- `adaptive_policy.reference_threshold_hz`
- `adaptive_policy.amplitude_step_mV`
- `adaptive_policy.maximum_amplitude_mV`
- `adaptive_policy.maximum_cycles`
- `adaptive_policy.maximum_total_experiment_minutes`

### `config/csv_schema.yaml`

代表CSVを確認して設定する。

- `calculation_mode`
- 時刻列
- 電極またはchannel列
- 発火頻度列、またはspike event列
- 列の単位
- 記録時間の求め方

### `config/stimulation_protocols.yaml`

- MaxLab Live/APIバージョン
- 刺激電極と記録電極
- 位相時間、パルス数、パルス間隔
- 独立した最大刺激強度
- 累積刺激上限
- 緊急停止手順

## 配置と開始

ZIPを展開し、プロジェクトrootでClaude Codeを起動する。

```bash
cd your-maxone-project
claude
```

最初の依頼例：

```text
CLAUDE.mdとconfigを読み、examples/inputに置いた代表CSVを解析してください。
実装はdry_run限定とし、csv_schema.yamlの列マッピング案、根拠、
未解決点、必要なfixtureテストを提示してください。
```

## 重要

Claude Codeはソフトウェアを作るために使い、実験中の判定主体にはしない。実験時には、テスト済みの決定論的PythonプログラムがCSVを解釈して次の刺激段階を決める。

CLAUDE.mdは指示であり強制機構ではないため、PreToolUse hookも追加した。ただしhookだけに依存せず、Python側でも環境変数、armed設定、装置lock、安全上限、preflightを独立して検証すること。
