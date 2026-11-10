#!/usr/bin/env python3
"""
adapters/zener.py: Zener（diodeinc/pcb の .zen）で作った基板を kicad-plus ingest にかける

  python3 adapters/zener.py <board.zen> --out <出力.kicad_pcb> [--intent 追加.json] [--layout 置き場]
                            [--pcb 基板.kicad_pcb] [--sch 回路図.kicad_sch] [--place] [--print-intent] [--json]

  1. .zen から設計の意図を読む: Power("VBUS", voltage="4.75V to 5.5V") → nets.VBUS {role: power, voltage: 5.5}、
     Ground("GND") → {role: ground}。--intent のファイル（電流・ネットクラス・配置の約束）を上に重ねる
  2. Zener が書き出した KiCad の基板と回路図を探す（--pcb/--sch で直接も。既定は Board(path=…) の置き場と
     .zen の隣の下を探す）。Zener の基板の書き出し（pcb layout など）は先に済ませておく（この道具は Zener を
     動かさない）
  3. kicad-plus ingest にかける（PWR_FLAG・未接続の印・ネット名・ネットクラス・配置の約束・確かめ）

部品は Zener の名前（J_USB）のまま intent に書いてよい（Zener はフットプリントの欄 Path に名前を入れる）。
--print-intent は 1 の結果を出すだけ（ingest しない）。終了コードは ingest と同じ（0 = 要確認なし、1 = あり、2 = 誤り）。
コアの外に置くのは、生成元ごとの癖（名前の付け方・書き出す場所）をここに閉じ込めるため。tscircuit・SKiDL も同じ形で足せる。
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
KL = str(HOME / "bin" / "kicad-plus")

CALL = re.compile(r'\b(Power|Ground|Net)\(\s*"([^"]+)"([^)]*)\)')
VOLT = re.compile(r'voltage\s*=\s*"([^"]+)"')
NUM = re.compile(r"(-?\d+(?:\.\d+)?)\s*(m?)V")
BOARD = re.compile(r'Board\((.*?)\)', re.S)


def volts(text):
    """ "4.75V to 5.5V" → 5.5（範囲は上の値。配線の幅や部品の耐圧を見るときは高い方が安全）、"3.3V" → 3.3"""
    vals = [float(v) / (1000 if m else 1) for v, m in NUM.findall(text)]
    return max(vals) if vals else None


def intent_from_zen(zen):
    src = Path(zen).read_text(encoding="utf-8")
    src = re.sub(r"#[^\n]*", "", src)   # 注釈は読まない
    nets = {}
    for kind, name, rest in CALL.findall(src):
        if kind == "Power":
            v = {"role": "power"}
            m = VOLT.search(rest)
            if m and volts(m.group(1)) is not None:
                v["voltage"] = volts(m.group(1))
                v["voltage_text"] = m.group(1)
            nets[name] = v
        elif kind == "Ground":
            nets[name] = {"role": "ground"}
    return {"nets": nets}


def merge(base, extra):
    out = {"nets": dict(base.get("nets", {})), "netclasses": dict(extra.get("netclasses", {})),
           "placement": list(extra.get("placement", []))}
    for n, v in extra.get("nets", {}).items():
        out["nets"][n] = dict(out["nets"].get(n, {}), **v)
    return out


def board_path(zen):
    """Board(path="eda") の置き場"""
    m = BOARD.search(Path(zen).read_text(encoding="utf-8"))
    if m:
        p = re.search(r'path\s*=\s*"([^"]+)"', m.group(1))
        if p:
            return Path(zen).parent / p.group(1)
    return None


def find_outputs(zen, layout):
    """Zener が書き出した (基板, 回路図)。見つからなければ (None, None) と探した場所"""
    roots = [Path(layout)] if layout else [r for r in (board_path(zen), Path(zen).parent) if r]
    looked = []
    for r in roots:
        looked.append(str(r))
        if not r.is_dir():
            continue
        pcbs = sorted(r.rglob("*.kicad_pcb"), key=lambda p: (len(p.parts), str(p)))
        pcbs = [p for p in pcbs if "ingest" not in p.parts and not p.name.startswith("_autosave")]
        for p in pcbs:
            sch = p.with_suffix(".kicad_sch")
            if sch.is_file():
                return p, sch, looked
        if pcbs:
            schs = sorted(r.rglob("*.kicad_sch"), key=lambda p: (len(p.parts), str(p)))
            return pcbs[0], (schs[0] if schs else None), looked
    return None, None, looked


def main():
    ap = argparse.ArgumentParser(prog="adapters/zener.py", description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n", 3)[3])
    ap.add_argument("zen")
    ap.add_argument("--out")
    ap.add_argument("--intent", help="重ねる intent.json（電流・ネットクラス・配置の約束など）")
    ap.add_argument("--layout", help="Zener の書き出しの置き場（既定は Board(path=…)）")
    ap.add_argument("--pcb")
    ap.add_argument("--sch")
    ap.add_argument("--place", action="store_true", help="縁の約束を守れていない部品を縁へ動かす（ingest --place）")
    ap.add_argument("--print-intent", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not Path(a.zen).is_file():
        sys.stderr.write(f".zen がない: {a.zen}\n")
        return 2
    intent = intent_from_zen(a.zen)
    if a.intent:
        try:
            extra = json.loads(Path(a.intent).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            sys.stderr.write(f"intent を読めない: {a.intent}: {e}\n")
            return 2
        intent = merge(intent, extra)
    if a.print_intent:
        print(json.dumps(intent, ensure_ascii=False, indent=1))
        return 0
    if not a.out:
        ap.error("--out（出力の .kicad_pcb）が要る")
    pcb, sch = (Path(a.pcb) if a.pcb else None), (Path(a.sch) if a.sch else None)
    if not pcb or not sch:
        fp, fs, looked = find_outputs(a.zen, a.layout)
        pcb, sch = pcb or fp, sch or fs
        if not pcb or not sch:
            sys.stderr.write("Zener の KiCad の書き出しが見つからない（探した所: " + "、".join(looked) + "）。\n"
                             "先に Zener で基板を書き出すか、--pcb と --sch で指定する\n")
            return 2
    f = Path(tempfile.mkdtemp(prefix="zener-intent-")) / "intent.json"
    f.write_text(json.dumps(intent, ensure_ascii=False, indent=1), encoding="utf-8")
    cmd = [KL, "ingest", str(sch), str(pcb), "--out", a.out, "--intent", str(f)] + (["--json"] if a.json else []) + (["--place"] if a.place else [])
    if not a.json:
        print(f"Zener: {Path(a.zen).name} → 回路図 {sch}、基板 {pcb}（意図: ネット {len(intent['nets'])} 本"
              + (f"、配置の約束 {len(intent.get('placement', []))} 件" if intent.get("placement") else "") + "）",
              flush=True)
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
