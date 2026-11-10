#!/usr/bin/env python3
"""
kstats: 基板の統計（部品・パッド・穴・面積・最小の配線幅とクリアランス）

  kicad-local stats <基板.kicad_pcb> [--json]

kicad-cli の pcb export stats を英語の環境で呼んで（本家の kicad-cli は日本語の環境だと JSON の
項目名まで訳す）、ここで日本語にする。配線や穴が無い基板の最小値（KiCad は整数の上限 2147.4836 mm を
出す）は「無い」に、外形が無いときの幅・高さ・面積は「外形（Edge.Cuts）が無い」にする。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# KiCad が「無い」の代わりに出す値（int の上限の nm）
NO_VALUE = "2147.4836"


def value(v):
    """ "0.3500 mm" → "0.3500 mm"、上限値・空 → None """
    if v is None:
        return None
    s = str(v)
    return None if s.startswith(NO_VALUE) else s


def stats(board):
    out = Path(tempfile.mkdtemp()) / "s.json"
    env = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8", LANGUAGE="")
    r = subprocess.run(["kicad-cli-local", "pcb", "export", "stats", "--format", "json", "-o", str(out), str(board)],
                       capture_output=True, text=True, env=env)
    if not out.exists():
        sys.stderr.write(f"統計を取れない: {board}\n{r.stdout}{r.stderr}")
        sys.exit(2)
    d = json.loads(out.read_text(encoding="utf-8"))
    b = d.get("board", {})
    notes = []
    if not b.get("has_outline"):
        notes.append("外形（Edge.Cuts）が無い、または閉じていないので、幅・高さ・面積・密度は出ない")
    for k, what in (("min_track_width", "配線"), ("min_track_clearance", "配線"), ("min_drill_diameter", "穴")):
        if b.get(k) is not None and value(b[k]) is None:
            b[k] = None
            notes.append({"min_track_width": "配線が無いので最小配線幅は無い",
                          "min_track_clearance": "配線が無いので最小配線クリアランスは無い",
                          "min_drill_diameter": "穴が無いので最小ドリル径は無い"}[k])
    d["notes"] = list(dict.fromkeys(notes))
    return d


LABELS = [("width", "幅"), ("height", "高さ"), ("area", "面積"), ("board_thickness", "厚さ"),
          ("min_track_width", "最小配線幅"), ("min_track_clearance", "最小配線クリアランス"),
          ("min_drill_diameter", "最小ドリル径"), ("front_copper_area", "表の銅の面積"),
          ("back_copper_area", "裏の銅の面積"), ("front_footprint_area", "表の部品の面積"),
          ("back_footprint_area", "裏の部品の面積")]
PADS = {"through_hole": "スルーホール", "smd": "表面実装", "connector": "コネクタ", "npth": "めっき無しの穴",
        "castellated": "端面スルーホール", "press_fit": "プレスフィット"}
VIAS = {"through_via": "貫通", "blind_via": "ブラインド", "buried_via": "ベリード", "micro_via": "マイクロ"}


def show(d):
    b = d.get("board", {})
    print("基板")
    for k, label in LABELS:
        if k in b:
            print(f"  {label}: {b[k] if b[k] is not None else '—'}")
    c = d.get("components", {}).get("total", {})
    if c:
        print(f"部品: {c.get('total', 0)}（表 {c.get('front', 0)}、裏 {c.get('back', 0)}）")
    pads = d.get("pads", {})
    if any(pads.values()):
        print("パッド: " + "、".join(f"{PADS.get(k, k)} {v}" for k, v in pads.items() if v))
    vias = d.get("vias", {})
    if any(vias.values()):
        print("ビア: " + "、".join(f"{VIAS.get(k, k or '貫通')} {v}" for k, v in vias.items() if v))
    holes = d.get("drill_holes", [])
    if holes:
        print("穴: " + "、".join(f"{h['x_size']}{'' if h.get('plated') else '（めっき無し）'} ×{h['count']}"
                                 for h in holes))
    for n in d.get("notes", []):
        print(f"  ※ {n}")


def main():
    ap = argparse.ArgumentParser(prog="kicad-local stats", description=__doc__.split("\n")[1])
    ap.add_argument("board")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not Path(a.board).is_file():
        print(f"基板がない: {a.board}", file=sys.stderr)
        return 2
    d = stats(a.board)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        show(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
