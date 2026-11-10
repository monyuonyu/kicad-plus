#!/usr/bin/env python3
"""
kicad-local ingest: 別の道具（Zener・tscircuit・SKiDL など）が作った回路図と基板を、KiCad で仕上げられる形に
そろえる（画面なし。kicad-python で動かす。入力は変えない）

  kicad-local ingest <回路図.kicad_sch> <基板.kicad_pcb> --out <出力.kicad_pcb> [--intent intent.json] [--place] [--json]

出力の基板の隣に、同じ名前の回路図（.kicad_sch。下の階層のシートも）とプロジェクト（.kicad_pro）を置く。
やること:
  1. 外から来る電源（ERC の power_pin_not_driven）に PWR_FLAG を付ける（回路図）
  2. 生成元が「つながない」とした端子（基板で NC_… のネット、回路図で 1 本きりのネット）に未接続の印（×）
  3. 基板のネット名を回路図にそろえる（NC_J_USB_SBU1 → unconnected-(J1-SBU1-PadA8)、VBUS → /VBUS など。
     つながりが同じなら配線は残す。pcbsync と同じ）。回路図にあって基板に無い部品は基板の外に仮置き
  4. intent.json があれば、ネットクラス（nets.*.netclass・netclasses。幅が無ければ current から IPC-2221）を
     プロジェクトに書き、配置の約束（placement）を確かめる。--place なら縁の約束を守れていない部品を
     縁に沿った空きへ動かす（向きはそのまま。kicad-route の findspace と place）
  5. 確かめる: 部品番号の重複、ライブラリに無いフットプリント、3D モデルの欠け、ERC、DRC（回路図との照合つき）
終了コード: 0 = 要確認なし、1 = 要確認あり（--json の checks を見る）、2 = 使い方の誤り・実行できない

intent.json の書き方は tools/kintent.py の先頭。部品は生成元の名前（J_USB）でもよい。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pcbnew

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kintent  # noqa: E402
import knetlist  # noqa: E402
import sx  # noqa: E402

KL = str(HERE.parent / "bin" / "kicad-local")
FPLIB = Path("/usr/share/kicad/footprints")
TEMPLATE_PRO = Path("/usr/share/kicad/template/kicad.kicad_pro")
GRID = 2.54


def run_json(args, timeout=600):
    """kicad-local の命令を --json で。（終了コード, 結果 or None, 出力の文字）"""
    r = subprocess.run([KL] + args + ["--json"], capture_output=True, text=True, timeout=timeout)
    try:
        return r.returncode, json.loads(r.stdout), r.stderr
    except ValueError:
        return r.returncode, None, r.stdout + r.stderr


def run_sch_steps(sch, steps):
    """ksch で手順を当てる（同じファイルに）"""
    if not steps:
        return True, ""
    f = Path(tempfile.mkdtemp()) / "steps.txt"
    f.write_text("\n".join(steps) + "\n", encoding="utf-8")
    tmp = sch.with_name(sch.stem + ".ingest-tmp.kicad_sch")
    r = subprocess.run([KL, "sch", str(sch), str(tmp), str(f), "--no-check"], capture_output=True, text=True)
    if not tmp.exists():
        return False, r.stdout + r.stderr
    os.replace(tmp, sch)
    return True, r.stdout


# ---------------------------------------------------------------- 1. 写す

def sheet_files(sch):
    """回路図と下の階層のシート（相対パス）"""
    out, seen = [], set()

    def walk(p):
        p = p.resolve()
        if p in seen or not p.is_file():
            return
        seen.add(p)
        out.append(p)
        try:
            root = sx.parse(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        for s in sx.find(root, "sheet"):
            for pr in sx.find(s, "property"):
                if len(pr) > 2 and str(pr[1]) in ("Sheetfile", "Sheet file"):
                    walk(p.parent / str(pr[2]))
    walk(Path(sch))
    return out


def copy_project(sch, pcb, out):
    """出力の隣に回路図（とシート）・プロジェクト・ライブラリ表を写す。返り値: 出力の回路図"""
    sch, pcb, out = Path(sch).resolve(), Path(pcb).resolve(), Path(out).resolve()
    dst_sch = out.with_suffix(".kicad_sch")
    if dst_sch == sch:
        raise SystemExit("出力の回路図が入力の回路図と同じになる（--out の名前か置き場所を変える）")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sch, dst_sch)
    for f in sheet_files(sch)[1:]:
        rel = f.relative_to(sch.parent) if f.is_relative_to(sch.parent) else Path(f.name)
        d = out.parent / rel
        if d.resolve() != f:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, d)
    for name in ("sym-lib-table", "fp-lib-table"):
        for src_dir in (sch.parent, pcb.parent):
            if (src_dir / name).is_file() and not (out.parent / name).exists():
                shutil.copyfile(src_dir / name, out.parent / name)
    pro = out.with_suffix(".kicad_pro")
    for cand in (pcb.with_suffix(".kicad_pro"), sch.with_suffix(".kicad_pro"), TEMPLATE_PRO):
        if cand.is_file() and cand.resolve() != pro:
            shutil.copyfile(cand, pro)
            break
    return dst_sch


# ---------------------------------------------------------------- 2. PWR_FLAG と未接続の印

def sch_extent(sch):
    """記号の位置の範囲（x0, y0, x1, y1）と、使っている #FLG の番号の最大"""
    text = Path(sch).read_text(encoding="utf-8")
    root = sx.parse(text)
    xs, ys = [], []
    for s in sx.find(root, "symbol"):
        at = sx.one(s, "at")
        if at:
            xs.append(float(at[1]))
            ys.append(float(at[2]))
    nums = [int(m) for m in re.findall(r'"#FLG(\d+)"', text)]
    if not xs:
        return (25.4, 25.4, 25.4, 25.4), max(nums, default=0)
    return (min(xs), min(ys), max(xs), max(ys)), max(nums, default=0)


def label_name(net):
    """回路図のネット名 → ラベルに書く名前（/VBUS → VBUS）。下の階層のネット（/sub/X）は None"""
    if net.startswith("/"):
        return net[1:] if net.count("/") == 1 else None
    return net


def add_pwr_flags(sch, intent):
    rc, erc, err = run_json(["erc", str(sch)])
    if erc is None:
        return {"error": f"ERC を回せない: {err[-300:]}"}
    nets = []
    for v in erc["violations"]:
        if v["type"] == "power_pin_not_driven":
            for n in v.get("nets", []):
                if n not in nets:
                    nets.append(n)
    # intent で "pwr_flag": true の電源も（端子が受け身の記号で ERC が言わないときのため）
    for want, v in (intent or {}).get("nets", {}).items():
        if v.get("pwr_flag") is True and not any(kintent.bare(n) == kintent.bare(want) for n in nets):
            nets.append(want)
    (x0, y0, x1, y1), last = sch_extent(sch)
    steps, added, skipped = [], [], []
    y = round((y1 + 20.32) / GRID) * GRID
    for i, net in enumerate(nets):
        name = label_name(net)
        if not name:
            skipped.append({"net": net, "why": "下の階層のネット。そのシートで PWR_FLAG を付ける"})
            continue
        last += 1
        ref = f"#FLG{last:02d}"
        x = round((x0 + 12.7 * len(added)) / GRID) * GRID
        steps.append(f'add "{ref}" PWR_FLAG power:PWR_FLAG at {x:.2f},{y:.2f} 1="{name}"')
        added.append({"net": net, "ref": ref, "at": [round(x, 2), round(y, 2)]})
    ok, log = run_sch_steps(sch, steps)
    if not ok:
        return {"error": f"PWR_FLAG を付けられない: {log[-300:]}", "nets": nets}
    return {"added": added, "skipped": skipped}


def board_pad_nets(pcb):
    """{(部品番号, パッド): ネット名}, {ネット名: パッドの数}（S 式で読む。pcbnew の LoadBoard は 1 つの
    プロセスで 2 回目から wx の注意を山ほど出すので、出力の基板の 1 回だけにする）"""
    import kdrc
    root = sx.parse(Path(pcb).read_text(encoding="utf-8"))
    kdrc.load_net_names(root)
    pads, count = {}, {}
    for f in sx.find(root, "footprint"):
        rp = sx.prop(f, "Reference")
        ref = str(rp[2]) if rp is not None and len(rp) > 2 else None
        if ref is None:   # KiCad 6/7 の (fp_text reference …)
            ft = [t for t in sx.find(f, "fp_text") if len(t) > 2 and t[1] == "reference"]
            ref = str(ft[0][2]) if ft else "?"
        for p in sx.find(f, "pad"):
            n = kdrc.net_of(p)
            pads[(ref, str(p[1]))] = n
            if n:
                count[n] = count.get(n, 0) + 1
    return pads, count


def add_no_connects(sch, pcb):
    """生成元が「つながない」とした端子: 回路図で 1 本きりのネット（unconnected-… か名前つきでも端子 1 つ）で、
    基板でも NC_… のネットか、そのパッドだけのネット・ネット無し"""
    d = knetlist.from_sch(str(sch))
    single = [(n["ref"], n["pin"]) for net in d["nets"] for n in net["nodes"][:1] if len(net["nodes"]) == 1]
    pads, count = board_pad_nets(pcb)
    pins = []
    for ref, pin in single:
        if ref.startswith("#") or (ref, pin) not in pads:
            continue
        n = pads[(ref, pin)]
        if not n or n.startswith(("NC_", "unconnected-")) or count.get(n, 0) <= 1:
            pins.append(f"{ref}.{pin}")
    if not pins:
        return {"added": []}
    ok, log = run_sch_steps(sch, ["noconnect " + ",".join(pins)])
    if not ok:
        return {"error": f"未接続の印を付けられない: {log[-300:]}"}
    done = [p for line in log.splitlines() if line.startswith("noconnect ") and ":" not in line
            for p in line[len("noconnect "):].split(", ")]
    return {"added": done}


# ---------------------------------------------------------------- 4. intent: ネットクラスと配置

def apply_netclasses(pro, intent, board_nets):
    """intent の nets.*.netclass を .kicad_pro に書く。返り値: 書いた内容と注意"""
    want = {}
    for name, v in intent["nets"].items():
        if v.get("netclass"):
            hit = kintent.match_net(board_nets, name)
            want[hit or name] = (v["netclass"], v, hit is not None)
    if not want:
        return {"assigned": [], "classes_created": [], "missing_nets": []}
    d = json.loads(Path(pro).read_text(encoding="utf-8"))
    ns = d.setdefault("net_settings", {})
    classes = ns.setdefault("classes", [])
    default = next((c for c in classes if c.get("name") == "Default"), None) or {
        "name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3}
    have = {c.get("name"): c for c in classes}
    created, assigned, missing = [], [], []
    for net, (cls, v, found) in want.items():
        if not found:
            missing.append(net)
            continue
        spec = intent["netclasses"].get(cls, {})
        if cls not in have:
            c = dict(default, name=cls)
            if spec.get("track_width"):
                c["track_width"] = float(spec["track_width"])
            elif v.get("current"):
                c["track_width"] = max(default.get("track_width", 0.2),
                                       round(kintent.ipc2221_width_mm(float(v["current"])) + 0.005, 2))
            if spec.get("clearance"):
                c["clearance"] = float(spec["clearance"])
            classes.append(c)
            have[cls] = c
            created.append({"name": cls, "track_width": c["track_width"], "clearance": c.get("clearance")})
        elif spec:
            for k in ("track_width", "clearance"):
                if spec.get(k):
                    have[cls][k] = float(spec[k])
        assign = ns.get("netclass_assignments") or {}
        assign[net] = [cls]
        ns["netclass_assignments"] = assign
        assigned.append({"net": net, "netclass": cls})
    Path(pro).write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"assigned": assigned, "classes_created": created, "missing_nets": missing}


def field(f, name):
    """フットプリントの欄の値（KiCad 8 でも 10 でも。無ければ ''）"""
    for fl in f.GetFields():
        if fl.GetName() == name:
            return fl.GetText()
    return ""


def fp_box(f):
    """部品の外形（クーテッドがあれば。無ければパッドとシルクの範囲）mm (x0, y0, x1, y1)"""
    side = pcbnew.F_CrtYd if f.GetLayer() == pcbnew.F_Cu else pcbnew.B_CrtYd
    try:
        poly = f.GetCourtyard(side)
        if poly.OutlineCount() > 0:
            bb = poly.BBox()
            return (pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()), pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))
    except Exception:  # noqa: BLE001
        pass
    bb = f.GetBoundingBox(False)
    return (pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()), pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))


def check_placement(b, intent):
    fps = {f.GetReference(): f for f in b.GetFootprints()}
    paths = {r: field(f, "Path") for r, f in fps.items()}
    eb = b.GetBoardEdgesBoundingBox()
    edge = (pcbnew.ToMM(eb.GetLeft()), pcbnew.ToMM(eb.GetTop()), pcbnew.ToMM(eb.GetRight()), pcbnew.ToMM(eb.GetBottom())) \
        if eb.GetWidth() > 0 else None
    out = []
    for r in intent["placement"]:
        ref = kintent.resolve_ref(r["ref"], fps, paths)
        row = {"rule": r, "ref": ref}
        if ref is None:
            row.update(ok=False, why=f"部品 {r['ref']} が基板に無い（部品番号でも欄 Path でも見つからない）")
            out.append(row)
            continue
        f = fps[ref]
        x0, y0, x1, y1 = fp_box(f)
        cx, cy = pcbnew.ToMM(f.GetPosition().x), pcbnew.ToMM(f.GetPosition().y)
        why = []
        if r.get("side"):
            on = "top" if f.GetLayer() == pcbnew.F_Cu else "bottom"
            if on != r["side"]:
                why.append(f"{'表' if on == 'top' else '裏'}にある（約束は{'表' if r['side'] == 'top' else '裏'}）")
        if "edge" in r:
            lim = float(r.get("max_mm", 1.5))
            if edge is None:
                why.append("外形（Edge.Cuts）が無いので縁を確かめられない")
            else:
                gaps = {"left": x0 - edge[0], "top": y0 - edge[1], "right": edge[2] - x1, "bottom": edge[3] - y1}
                sides = list(gaps) if r["edge"] == "any" else [r["edge"]]
                best = min(sides, key=lambda k: gaps[k])
                row["edge_gap_mm"] = round(gaps[best], 2)
                row["edge_side"] = best
                if gaps[best] > lim:
                    why.append(f"縁から {gaps[best]:.2f} mm（{ {'left': '左', 'right': '右', 'top': '上', 'bottom': '下'}[best] }の縁。"
                               f"約束は {lim} mm 以内）")
        if "near" in r:
            other = kintent.resolve_ref(r["near"], fps, paths)
            lim = float(r.get("max_mm", 5))
            if other is None:
                why.append(f"相手の部品 {r['near']} が基板に無い")
            else:
                o = fps[other].GetPosition()
                dist = ((cx - pcbnew.ToMM(o.x)) ** 2 + (cy - pcbnew.ToMM(o.y)) ** 2) ** 0.5
                row["distance_mm"] = round(dist, 2)
                if dist > lim:
                    why.append(f"{other} から {dist:.2f} mm（約束は {lim} mm 以内）")
        if "target" in r:
            tx, ty = (float(v) for v in r["target"])
            lim = float(r.get("max_mm", 1))
            dist = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
            row["distance_mm"] = round(dist, 2)
            if dist > lim:
                why.append(f"({cx:.2f}, {cy:.2f}) にある。約束の ({tx}, {ty}) から {dist:.2f} mm")
        row["ok"] = not why
        if why:
            row["why"] = "、".join(why)
            if "edge" in r and edge is not None:
                row["hint"] = f"kicad-local route で {ref} を縁へ動かす（findspace / drag）"
        out.append(row)
    return out


def load_board(path):
    """pcbnew.LoadBoard。2 回目からの wx の注意（画像の読み手の重複）は捨てる"""
    sys.stderr.flush()
    keep = os.dup(2)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 2)
    try:
        return pcbnew.LoadBoard(str(path))
    finally:
        os.dup2(keep, 2)
        os.close(null)
        os.close(keep)


SIDE_JA = {"left": "左", "right": "右", "top": "上", "bottom": "下"}
PLACE = re.compile(r"place (\S+) (-?[\d.]+),(-?[\d.]+) (-?[\d.]+)")


def place_on_edges(out, b, rows):
    """縁の約束を守れていない部品を、向きはそのまま、縁に沿った帯の中の空いた所へ（kicad-route の findspace と
    place。改良版のビルドが要る）。返り値: [{"ref", "from", "to", "ok", "why"}]"""
    route = shutil.which("kicad-route") or str(HERE.parent / "bin" / "kicad-route")
    eb = b.GetBoardEdgesBoundingBox()
    E = (pcbnew.ToMM(eb.GetLeft()), pcbnew.ToMM(eb.GetTop()), pcbnew.ToMM(eb.GetRight()), pcbnew.ToMM(eb.GetBottom()))
    fps = {f.GetReference(): f for f in b.GetFootprints()}
    done = []
    for row in rows:
        r, ref = row["rule"], row["ref"]
        if row["ok"] or "edge" not in r or ref is None or "edge_side" not in row:
            continue
        f = fps[ref]
        x0, y0, x1, y1 = fp_box(f)
        cx, cy = pcbnew.ToMM(f.GetPosition().x), pcbnew.ToMM(f.GetPosition().y)
        ang = round(f.GetOrientationDegrees()) % 360
        lim = float(r.get("max_mm", 1.5))
        side = r["edge"] if r["edge"] != "any" else row["edge_side"]
        l, t, rr, bt = cx - x0, cy - y0, x1 - cx, y1 - cy
        inset = 0.3   # findspace は外形枠を基板の縁から 0.3 mm 内側に収める

        def band_of(w):
            """中心の帯（部品の外形が縁から inset〜inset+w mm に入る範囲）"""
            return {"left": (E[0] + l + inset, E[1] + t, E[0] + l + inset + w, E[3] - bt),
                    "right": (E[2] - rr - inset - w, E[1] + t, E[2] - rr - inset, E[3] - bt),
                    "top": (E[0] + l, E[1] + t + inset, E[2] - rr, E[1] + t + inset + w),
                    "bottom": (E[0] + l, E[3] - bt - inset - w, E[2] - rr, E[3] - bt - inset)}[side]

        rec = {"ref": ref, "edge": side, "from": [round(cx, 2), round(cy, 2)]}
        best, ran = None, False
        # 縁に近い細い帯から探し、無ければ広げる（findspace は 8 か所しか出さず、近い候補はまとめるので）
        for w in sorted({0.25, 0.5, max(lim - inset, 0.25)}):
            if w > max(lim - inset, 0.25):
                continue
            band = band_of(w)
            q = subprocess.run([route, str(out), "-", "-"],
                               input=f"findspace {ref} 0.25 " + ",".join(f"{v:.3f}" for v in band) + "\n",
                               capture_output=True, text=True, timeout=600)
            ran = ran or "置き場所" in q.stdout
            same = [(float(x), float(y), round(float(a)) % 360) for rf, x, y, a in PLACE.findall(q.stdout)
                    if rf == ref and round(float(a)) % 360 == ang]
            if same:
                best = same[0]   # 帯の中では配線の短い順
                break
        if best is None:
            rec.update(ok=False, why="縁に沿った帯に、今の向きのまま置ける空きが無い" if ran
                       else "kicad-route を動かせない（改良版のビルドが要る。kicad-local doctor）")
            done.append(rec)
            continue
        tmp = out.with_name(out.stem + ".ingest-tmp.kicad_pcb")
        q = subprocess.run([route, str(out), str(tmp), "-"], input=f"place {ref} {best[0]:.3f},{best[1]:.3f} {best[2]}\n",
                           capture_output=True, text=True, timeout=600)
        moved = tmp.exists() and re.search(r"成功 1 / 失敗 0", q.stdout)
        if not moved:
            ng = [ln.strip() for ln in q.stdout.splitlines() if " NG " in ln]
            rec.update(ok=False, why="place が失敗した: " + (ng[0] if ng else (q.stdout + q.stderr)[-300:]))
            if tmp.exists():
                tmp.unlink()
        else:
            os.replace(tmp, out)
            rec.update(ok=True, to=[round(best[0], 2), round(best[1], 2)])
        for extra in (tmp.with_suffix(".kicad_pro"), tmp.with_suffix(".kicad_prl")):
            if extra.exists():
                extra.unlink()
        done.append(rec)
    return done


# ---------------------------------------------------------------- 5. 確かめる

def fp_resolves(fpid, project_dir):
    if not fpid or ":" not in fpid:
        return False
    lib, name = fpid.split(":", 1)
    if (FPLIB / f"{lib}.pretty" / f"{name}.kicad_mod").is_file():
        return True
    for table in (project_dir / "fp-lib-table", Path.home() / ".config" / "kicad" / "10.0" / "fp-lib-table",
                  Path.home() / ".config" / "kicad" / "8.0" / "fp-lib-table"):
        if not table.is_file():
            continue
        m = re.search(r'\(lib\s+\(name\s+"?%s"?\).*?\(uri\s+"?([^")]+)"?\)' % re.escape(lib), table.read_text(encoding="utf-8"), re.S)
        if m:
            uri = m.group(1).replace("${KIPRJMOD}", str(project_dir)).replace("${KICAD10_FOOTPRINT_DIR}", str(FPLIB)) \
                .replace("${KICAD8_FOOTPRINT_DIR}", str(FPLIB))
            if (Path(uri) / f"{name}.kicad_mod").is_file():
                return True
    return False


def check_parts(sch, pcb, b):
    d = knetlist.from_sch(str(sch))
    refs = [c["ref"] for c in d["components"] if not c["ref"].startswith("#")]
    dup_sch = sorted({r for r in refs if refs.count(r) > 1})
    brefs = [f.GetReference() for f in b.GetFootprints()]
    dup_pcb = sorted({r for r in brefs if brefs.count(r) > 1})
    unresolved = []
    for c in d["components"]:
        if c["ref"].startswith("#"):
            continue
        fp = c.get("footprint") or ""
        if not fp:
            unresolved.append({"ref": c["ref"], "footprint": "", "why": "フットプリントの指定が無い"})
        elif not fp_resolves(fp, Path(pcb).parent):
            unresolved.append({"ref": c["ref"], "footprint": fp, "why": "ライブラリに見つからない"})
    return {"duplicate_refs_schematic": dup_sch, "duplicate_refs_board": dup_pcb, "unresolved_footprints": unresolved}


# ---------------------------------------------------------------- 本体

def main():
    ap = argparse.ArgumentParser(prog="kicad-local ingest", description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n", 3)[3])
    ap.add_argument("schematic")
    ap.add_argument("board")
    ap.add_argument("--out", required=True)
    ap.add_argument("--intent")
    ap.add_argument("--no-pwr-flag", action="store_true", help="PWR_FLAG を付けない")
    ap.add_argument("--no-nc", action="store_true", help="未接続の印を付けない")
    ap.add_argument("--place", action="store_true",
                    help="縁の約束（placement の edge）を守れていない部品を、縁に沿った空きへ動かす（改良版のビルドが要る）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    for f in (a.schematic, a.board):
        if not Path(f).is_file():
            sys.stderr.write(f"ファイルがない: {f}\n")
            return 2
    out = Path(a.out)
    if out.suffix != ".kicad_pcb":
        sys.stderr.write("--out は .kicad_pcb にする\n")
        return 2
    if out.resolve() == Path(a.board).resolve():
        sys.stderr.write("--out が入力の基板と同じ。別の名前にする\n")
        return 2
    intent = None
    if a.intent:
        try:
            intent = kintent.load(a.intent)
        except kintent.IntentError as e:
            sys.stderr.write(f"{e}\n")
            return 2
    say = (lambda *x: None) if a.json else print
    rep = {"schematic": a.schematic, "board": a.board, "out": str(out), "intent": a.intent}

    sch = copy_project(a.schematic, a.board, out)
    rep["out_schematic"] = str(sch)
    say(f"写した: {sch.name}・{out.with_suffix('.kicad_pro').name}")

    if not a.no_pwr_flag:
        rep["pwr_flag"] = add_pwr_flags(sch, intent)
        for x in rep["pwr_flag"].get("added", []):
            say(f"  PWR_FLAG {x['ref']} → {x['net']}")
        for x in rep["pwr_flag"].get("skipped", []):
            say(f"  ! PWR_FLAG を付けなかった: {x['net']}（{x['why']}）")
        if rep["pwr_flag"].get("error"):
            say(f"  ! {rep['pwr_flag']['error']}")
    if not a.no_nc:
        rep["no_connect"] = add_no_connects(sch, a.board)
        if rep["no_connect"].get("added"):
            say(f"  未接続の印: {', '.join(rep['no_connect']['added'])}")
        if rep["no_connect"].get("error"):
            say(f"  ! {rep['no_connect']['error']}")

    rc, sync, err = run_json(["pcbsync", str(sch), a.board, str(out), "--no-drc"])
    if sync is None or not out.exists():
        sys.stderr.write(f"基板を回路図にそろえられない:\n{err[-1500:]}")
        return 2
    rep["sync"] = {k: sync.get(k) for k in ("renamed_nets", "pad_nets", "added", "missing_library", "footprints",
                                            "extra", "redo_nets", "tracks_removed", "parity")}
    say(f"  ネット名をそろえた: {len(sync.get('renamed_nets') or [])} 本、端子のネットを直した: {len(sync.get('pad_nets') or [])} 個、"
        f"部品を足した: {len(sync.get('added') or [])} 個、消した配線: {sync.get('tracks_removed', 0)} 本")

    b = load_board(out)
    if intent:
        names = [knetlist.unescape(str(n)) for n in b.GetNetsByName().keys() if str(n)]
        rep["netclasses"] = apply_netclasses(out.with_suffix(".kicad_pro"), intent, names)
        for x in rep["netclasses"]["assigned"]:
            say(f"  ネットクラス {x['net']} → {x['netclass']}")
        rep["placement"] = check_placement(b, intent)
        if a.place and any(not x["ok"] for x in rep["placement"]):
            rep["placed"] = place_on_edges(out, b, rep["placement"])
            for x in rep["placed"]:
                say(f"  動かした {x['ref']}: {x['from']} → {x['to']}（{SIDE_JA[x['edge']]}の縁）" if x["ok"]
                    else f"  ! {x['ref']} を縁へ動かせなかった: {x['why']}")
            if any(x["ok"] for x in rep["placed"]):
                b = load_board(out)
                rep["placement"] = check_placement(b, intent)

    rep["parts"] = check_parts(sch, out, b)
    _, models, _ = run_json(["models", str(out)])
    rep["models"] = {"missing": (models or {}).get("missing", []), "no_model": (models or {}).get("no_model", [])}
    _, erc, _ = run_json(["erc", str(sch)])
    _, drc, _ = run_json(["drc", str(out)])
    rep["erc"] = erc
    rep["drc"] = drc

    # まとめ: 要確認の項目
    checks = []
    if erc is None:
        checks.append({"kind": "erc", "what": "ERC を回せなかった"})
    else:
        for v in erc["violations"]:
            if v["type"] not in erc.get("noise_types", []):
                checks.append({"kind": "erc", "type": v["type"], "nets": v.get("nets", []),
                               "what": " ⇔ ".join(i.get("description", "") for i in v.get("items", [])[:2])})
    if drc is None:
        checks.append({"kind": "drc", "what": "DRC を回せなかった"})
    else:
        for key in ("violations", "unconnected_items", "schematic_parity"):
            for v in drc.get(key, []):
                if v["type"] in drc.get("noise_types", []) or key == "unconnected_items":
                    continue   # 未配線は取り込みの直後には当たり前（数だけ出す）
                checks.append({"kind": "drc" if key == "violations" else "parity", "type": v["type"],
                               "what": " ⇔ ".join(i.get("description", "") for i in v.get("items", [])[:2])})
    p = rep["parts"]
    for r in p["duplicate_refs_schematic"] + p["duplicate_refs_board"]:
        checks.append({"kind": "duplicate_ref", "ref": r, "what": f"部品番号 {r} が重複"})
    for u in p["unresolved_footprints"]:
        checks.append({"kind": "unresolved_footprint", "ref": u["ref"], "what": f"{u['ref']}: {u['footprint'] or '（なし）'} {u['why']}"})
    for m in rep["models"]["missing"]:
        checks.append({"kind": "missing_3d_model", "what": json.dumps(m, ensure_ascii=False) if not isinstance(m, str) else m})
    for x in (rep.get("netclasses") or {}).get("missing_nets", []):
        checks.append({"kind": "intent_net", "what": f"intent のネット {x} が基板に無い"})
    for x in rep.get("placement") or []:
        if not x["ok"]:
            checks.append({"kind": "placement", "ref": x["ref"] or x["rule"]["ref"], "what": x["why"]})
    rep["checks"] = checks
    rep["counts"] = {
        "power_pin_not_driven": sum(1 for v in (erc or {}).get("violations", []) if v["type"] == "power_pin_not_driven"),
        "net_conflict": sum(1 for v in (drc or {}).get("schematic_parity", []) if v["type"] == "net_conflict"),
        "unconnected_items": len((drc or {}).get("unconnected_items", [])),
        "from_footprint_library": len((drc or {}).get("from_footprint_library", [])),
    }
    rep["ok"] = not checks
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        c = rep["counts"]
        say(f"ERC power_pin_not_driven {c['power_pin_not_driven']}、DRC net_conflict {c['net_conflict']}、"
            f"未配線 {c['unconnected_items']}、部品のライブラリ由来の違反 {c['from_footprint_library']}（参考）")
        for x in rep.get("placement") or []:
            say(f"  配置 {x['rule']['ref']}{'（' + x['ref'] + '）' if x['ref'] and x['ref'] != x['rule']['ref'] else ''}: "
                + ("○" + (f" 縁から {x['edge_gap_mm']} mm" if "edge_gap_mm" in x else "") if x["ok"] else "× " + x["why"]))
        if checks:
            say(f"要確認 {len(checks)} 件:")
            for x in checks[:40]:
                say(f"  ! [{x['kind']}{'/' + x['type'] if x.get('type') else ''}] {x['what']}")
        else:
            say("要確認なし")
        say(f"→ {out}（回路図 {sch.name}）")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
