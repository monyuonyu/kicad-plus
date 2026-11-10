#!/usr/bin/env python3
"""
kintent: 設計の意図（intent.json）を読む。ingest・finish widen・ERC/PWR_FLAG が使う

intent.json の形（どれも省ける）:
  {
    "nets": {
      "VBUS":  {"role": "power", "voltage": 5.0, "current": 1.5, "source": "J_USB", "netclass": "Power"},
      "GND":   {"role": "ground"},
      "CC1":   {"role": "signal"}
    },
    "netclasses": {"Power": {"track_width": 0.5, "clearance": 0.2}},
    "placement": [
      {"ref": "J_USB", "edge": "any", "side": "top"},        外形の縁に（edge: any/left/right/top/bottom、max_mm 既定 1.5）
      {"ref": "C_IN", "near": "U_LDO", "max_mm": 3},         ほかの部品の近くに（中心どうしの距離）
      {"ref": "U_LDO", "target": [20, 15], "max_mm": 2}      決まった位置に
    ]
  }

  role: power（電源）・ground（GND）・signal。source はその電源を出す部品（コネクタなら外から来る）
  voltage（V）・current（A）: current は finish widen で配線の幅に使う
  pwr_flag: true なら ERC が言わなくても PWR_FLAG を付ける（ERC の power_pin_not_driven のネットには必ず付く）
  netclass: そのネットのネットクラス。netclasses に無いクラスは、current から IPC-2221 で幅を出して作る

部品は部品番号（J1）でも、生成元の名前（Zener の J_USB・C_IN。フットプリントの欄 Path が J_USB・C_IN.C・Power.C_IN など）でもよい。
ネットは見た目の名前で比べ、先頭の / の有無は問わない（VBUS = /VBUS）。
"""
import json
from pathlib import Path

ROLES = ("power", "ground", "signal")
EDGES = ("any", "left", "right", "top", "bottom")


class IntentError(ValueError):
    pass


def load(path):
    """読んで形を確かめる。誤りは IntentError（どこが違うかを書く）"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise IntentError(f"intent を読めない: {path}: {e}") from e
    if not isinstance(d, dict):
        raise IntentError("intent の一番外は {…} にする")
    nets = d.get("nets") or {}
    if not isinstance(nets, dict):
        raise IntentError("nets は {ネット名: {…}} にする")
    for n, v in nets.items():
        if not isinstance(v, dict):
            raise IntentError(f"nets.{n} は {{…}} にする")
        if v.get("role") is not None and v["role"] not in ROLES:
            raise IntentError(f"nets.{n}.role は {'/'.join(ROLES)} のどれか（{v['role']!r}）")
        for k in ("voltage", "current"):
            if v.get(k) is not None and not isinstance(v[k], (int, float)):
                raise IntentError(f"nets.{n}.{k} は数（{v[k]!r}）")
    classes = d.get("netclasses") or {}
    if not isinstance(classes, dict):
        raise IntentError("netclasses は {クラス名: {track_width, clearance}} にする")
    place = d.get("placement") or []
    if not isinstance(place, list):
        raise IntentError("placement は [ {…}, … ] にする")
    for i, r in enumerate(place):
        if not isinstance(r, dict) or not r.get("ref"):
            raise IntentError(f"placement[{i}] に ref が無い")
        kinds = [k for k in ("edge", "near", "target") if k in r]
        if len(kinds) != 1:
            raise IntentError(f"placement[{i}]（{r['ref']}）は edge・near・target のどれか 1 つ")
        if "edge" in r and r["edge"] not in EDGES:
            raise IntentError(f"placement[{i}].edge は {'/'.join(EDGES)} のどれか（{r['edge']!r}）")
        if "side" in r and r["side"] not in ("top", "bottom"):
            raise IntentError(f"placement[{i}].side は top/bottom")
        if "target" in r and not (isinstance(r["target"], list) and len(r["target"]) == 2):
            raise IntentError(f"placement[{i}].target は [x, y]（mm）")
    return {"nets": nets, "netclasses": classes, "placement": place}


def bare(net):
    """比べるための名前（先頭の / を取る）"""
    return net[1:] if net and net.startswith("/") and net.count("/") == 1 else net


def net_lookup(intent, net):
    """基板・回路図のネット名に当たる intent の項目（無ければ None）"""
    nets = intent.get("nets") or {}
    if net in nets:
        return nets[net]
    b = bare(net)
    for k, v in nets.items():
        if bare(k) == b:
            return v
    return None


def match_net(names, want):
    """want（intent の名前）に当たる実際のネット名（names から）。無ければ None"""
    if want in names:
        return want
    b = bare(want)
    hit = [n for n in names if bare(n) == b]
    return hit[0] if len(hit) == 1 else None


def resolve_ref(name, refs, paths):
    """部品の指定（J1 か生成元の名前 J_USB）→ 部品番号。paths は {部品番号: 欄 Path の値}。
    見つからない・2 つ以上に当たるときは None"""
    if name in refs:
        return name
    # Zener の Path は「生成元の名前.下位の名前」（C_IN.C・D_LED.LED）や「上位.名前」（Power.R1）。
    # 区切り（.）で分けた並びの中に name の並びがそのままあれば当たり
    want = name.split(".")

    def has(p):
        seg = p.split(".")
        return any(seg[i:i + len(want)] == want for i in range(len(seg) - len(want) + 1))

    hit = [r for r, p in paths.items() if p and has(p)]
    if len(hit) > 1:   # 複数に当たるなら、名前で始まるもの（C_IN.C）を優先
        first = [r for r in hit if paths[r].split(".")[:len(want)] == want]
        hit = first if len(first) == 1 else hit
    return hit[0] if len(hit) == 1 else None


def ipc2221_width_mm(current, rise=10.0, copper_um=35.0, internal=False):
    """IPC-2221 の式で、その電流を流すのに要る配線の幅（mm）"""
    k = 0.024 if internal else 0.048
    area = (current / (k * rise ** 0.44)) ** (1 / 0.725)       # mil²
    thick_mil = copper_um / 25.4
    return area / thick_mil * 0.0254
