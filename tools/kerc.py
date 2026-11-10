#!/usr/bin/env python3
"""kicad-local erc: ERC を回して要点を出す

使い方:
  kicad-local erc <回路図.kicad_sch> [--json]

終了コード: 0 = 要確認の項目なし、1 = あり、2 = 使い方の誤り・ERC を実行できない
ライブラリとの差分（lib_symbol_issues など）は参考として数えない。
各項目にネット名を付ける（回路図のネットリストから。電源記号 #PWR は値、ラベルは名前）。
古い改良版のビルドは JSON の位置が 100 分の 1 だったので、回路図の中の位置と比べて直す。
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sx  # noqa: E402

NOISE = ("lib_symbol_issues", "lib_symbol_mismatch", "footprint_link_issues")


def prop(e, key):
    p = sx.prop(e, key)
    return str(p[2]) if p and len(p) > 2 else None


def schematic_index(sch):
    """回路図（下の階層も）から {uuid: (x, y)} と {電源記号の参照: 値}"""
    where, power, seen = {}, {}, set()

    def walk(path):
        path = path.resolve()
        if path in seen or not path.is_file():
            return
        seen.add(path)
        try:
            root = sx.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        for e in root:
            if not isinstance(e, list) or not e:
                continue
            at = sx.one(e, "at")
            xy = (float(at[1]), float(at[2])) if at and len(at) >= 3 else None
            u = sx.one(e, "uuid")
            if u and xy:
                where[str(u[1])] = xy
            if e[0] == "symbol" and xy:
                for pin in sx.find(e, "pin"):
                    pu = sx.one(pin, "uuid")
                    if pu:
                        where[str(pu[1])] = xy
                ref, val = prop(e, "Reference"), prop(e, "Value")
                if ref and str(ref).startswith("#") and val:
                    power[str(ref)] = str(val)
            if e[0] == "sheet":
                f = prop(e, "Sheetfile") or prop(e, "Sheet file")
                if f:
                    walk(path.parent / str(f))

    walk(Path(sch))
    return where, power


def pin_nets(sch):
    """{(参照, ピン番号): ネット名}（kicad-local netlist から。取れなければ空）"""
    tool = Path(__file__).resolve().parent.parent / "bin" / "kicad-local"
    try:
        r = subprocess.run([str(tool), "netlist", str(sch), "--json"], capture_output=True, text=True, timeout=120)
        d = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    return {(n["ref"], n["pin"]): net["name"] for net in d.get("nets", []) for n in net.get("nodes", [])}


SYM = re.compile(r"^Symbol (\S+) Pin (\S+)")
LABEL = re.compile(r"^(?:Global |Hierarchical )?Label '(.*)'")


def net_of(desc, nets, power):
    m = SYM.match(desc)
    if m:
        ref, pin = m.groups()
        if (ref, pin) in nets:
            return nets[(ref, pin)]
        return power.get(ref)
    m = LABEL.match(desc)
    return m.group(1) if m else None


def fix_scale(arr, where):
    """位置が回路図の 100 分の 1 なら（古いビルド）100 倍に。どれか 1 つでも照合できれば判定する"""
    near = far = 0
    for v in arr:
        for i in v.get("items", []):
            p, xy = i.get("pos"), where.get(i.get("uuid", ""))
            if not p or not xy:
                continue
            d1 = abs(p["x"] - xy[0]) + abs(p["y"] - xy[1])
            d100 = abs(p["x"] * 100 - xy[0]) + abs(p["y"] * 100 - xy[1])
            if d100 < d1:
                far += 1
            else:
                near += 1
    if far > near:
        for v in arr:
            for i in v.get("items", []):
                if i.get("pos"):
                    i["pos"] = {"x": round(i["pos"]["x"] * 100, 4), "y": round(i["pos"]["y"] * 100, 4)}
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n", 2)[2])
    ap.add_argument("schematic")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    sch = Path(a.schematic)
    if not sch.is_file():
        sys.stderr.write(f"回路図がない: {sch}\n")
        return 2
    out = Path(tempfile.mkdtemp()) / "erc.json"
    env = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8", LANGUAGE="")  # 項目の説明を英語で（照合のため）
    r = subprocess.run(["kicad-cli-local", "sch", "erc", "--format", "json", "-o", str(out), str(sch)],
                       capture_output=True, text=True, env=env)
    if not out.exists():
        sys.stderr.write(f"ERC を実行できない: {sch}\n{r.stdout}{r.stderr}")
        return 2
    d = json.loads(out.read_text(encoding="utf-8"))
    arr = [dict(v, sheet=s.get("path", "")) for s in d.get("sheets", []) for v in s.get("violations", [])]
    where, power = schematic_index(sch)
    rescaled = fix_scale(arr, where)
    nets = pin_nets(sch) if arr else {}
    for v in arr:
        for i in v.get("items", []):
            i["net"] = net_of(i.get("description", ""), nets, power)
        v["nets"] = list(dict.fromkeys(i["net"] for i in v.get("items", []) if i.get("net")))
    main_ = [v for v in arr if v["type"] not in NOISE]
    if a.json:
        json.dump({"schematic": str(sch), "ok": not main_, "problems": len(main_), "violations": arr,
                   "noise_types": list(NOISE), "positions_rescaled": rescaled}, sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        c = collections.Counter(v["type"] for v in arr)
        print(f"ERC: {len(arr)} 件" + (f"(うち要確認 {len(main_)} 件)" if len(main_) != len(arr) else ""))
        for t, n in c.most_common():
            mark = "  " if t in NOISE else "! "
            print(f"  {mark}{t}: {n}")
            if t not in NOISE:
                for v in [x for x in arr if x["type"] == t][:8]:
                    p = (v.get("items") or [{}])[0].get("pos")
                    ps = f"({p['x']:.2f}, {p['y']:.2f})" if p else ""
                    ns = f" ネット {', '.join(v['nets'])}" if v.get("nets") else ""
                    print(f"       {ps} {v.get('sheet', '')} " + " ⇔ ".join(i.get("description", "")[:48]
                                                                          for i in v.get("items", [])[:2]) + ns)
        print("(先頭が ! の項目が要確認。ライブラリ差分は参考)")
    return 0 if not main_ else 1


if __name__ == "__main__":
    sys.exit(main())
