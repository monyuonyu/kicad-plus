# 別の道具で作った基板を取り込む（kicad-local ingest）

Zener（.zen）・tscircuit・SKiDL などのコードから作った回路図と基板は、そのままだと KiCad の確認で
引っかかることが多い。

- 外から来る電源に PWR_FLAG が無く、ERC が power_pin_not_driven を出す
- 使わない端子のネットの名前が、生成元（NC_J_USB_SBU1）と回路図（unconnected-(J1-SBU1-PadA8)）で違い、
  DRC の回路図との照合が net_conflict を出す。ネット名の / の有無（VBUS と /VBUS）でも同じことが起きる
- 未接続の印が無く、ERC が pin_not_connected を出す

`kicad-local ingest` はこれをそろえ、残った要確認を一覧にする（入力は変えない）。

```
kicad-local ingest probe.kicad_sch probe.kicad_pcb --out ingest/probe.kicad_pcb --intent intent.json [--place] [--json]
```

1. PWR_FLAG を付ける（ERC の power_pin_not_driven のネット。intent で `"pwr_flag": true` のネットも）
2. 生成元が「つながない」とした端子（回路図で 1 本きりのネット、基板で NC_… かそのパッドだけ）に未接続の印
3. 基板のネット名を回路図にそろえる（つながりが同じなら配線は残す）
4. intent.json: ネットクラスをプロジェクトに書く（幅が無ければ電流から IPC-2221）。配置の約束を確かめ、
   `--place` なら縁の約束を守れていない部品を縁に沿った空きへ動かす（向きはそのまま。改良版のビルドが要る）
5. 確かめる: 部品番号の重複、ライブラリに無いフットプリント、3D モデルの欠け、ERC、DRC（回路図との照合つき。
   1 つの部品の中だけで起きる違反は「部品のライブラリ由来」として別に数える）

終了コード: 0 = 要確認なし、1 = あり（`--json` の `checks`）、2 = 使い方の誤り。

## intent.json

```json
{
  "nets": {
    "VBUS": {"role": "power", "voltage": 5.0, "current": 1.5, "source": "J_USB", "netclass": "Power"},
    "V3V3": {"role": "power", "voltage": 3.3, "current": 0.3, "source": "U_LDO"},
    "GND":  {"role": "ground"}
  },
  "netclasses": {"Power": {"track_width": 0.5, "clearance": 0.2}},
  "placement": [
    {"ref": "J_USB", "edge": "any", "side": "top"},
    {"ref": "C_IN", "near": "U_LDO", "max_mm": 3}
  ]
}
```

- 部品は部品番号（J1）でも生成元の名前（J_USB。Zener はフットプリントの欄 Path に入れる）でもよい
- ネットは / の有無を問わない（VBUS = /VBUS）
- `edge` は any・left・right・top・bottom（`max_mm` 既定 1.5）、`near` は中心どうしの距離（既定 5）、
  `target: [x, y]` は決まった位置（既定 1）
- `current` は `kicad-local finish widen 入力 出力 --intent intent.json` でも使う（そのネットの配線を太くする）
- 形の詳しい説明は `tools/kintent.py` の先頭

## Zener から

```
python3 adapters/zener.py board.zen --out ingest/board.kicad_pcb --intent 追加.json [--place]
```

.zen の `Power("VBUS", voltage="4.75V to 5.5V")`・`Ground("GND")` から nets の role と voltage を作り、
追加の intent（電流・ネットクラス・配置の約束）を重ねて ingest にかける。Zener の KiCad の書き出し
（Board(path=…) の下）は先に済ませておく（この道具は Zener を動かさない）。`--print-intent` で作った意図だけを出す。
生成元ごとの癖（名前の付け方・書き出す場所）は adapters/ に閉じ込め、ingest は生成元を知らない。
