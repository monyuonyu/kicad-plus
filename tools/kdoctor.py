#!/usr/bin/env python3
"""
kdoctor: この環境で kicad-plus のどの命令が使えるかを調べる

  kicad-plus doctor [--json]

改良版のビルド（配布版の build/ か KICAD_LOCAL_BUILD）があれば全部の命令が使える。無ければ本家の
KiCad（kicad-cli と Python の pcbnew）で動く範囲に自動で切り替わり、ビルドが要る命令（route・
findspace・drag・silk・gerber・画面）は使えない。足りない物と入れ方を一覧にする。
終了コード: 0 = 本家か改良版の KiCad で動く、1 = KiCad が見つからない
"""
import argparse
import ctypes.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
RELEASES = "https://github.com/monyuonyu/kicad-plus/releases/latest"


def build_dir():
    b = os.environ.get("KICAD_LOCAL_BUILD")
    if b:
        return Path(b)
    if (HOME / "build").is_dir():
        return HOME / "build"
    return Path.home() / "src" / "build-10.0.6"


def run(cmd, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def check():
    b = build_dir()
    have = {
        "build_cli": (b / "kicad" / "kicad-cli").is_file(),
        "build_pcbnew": any((b / "pcbnew").glob("_pcbnew*.so")) if (b / "pcbnew").is_dir() else False,
        "build_route": (b / "qa" / "tools" / "pns" / "kicad_route").is_file(),
        "build_gerber": (b / "gerbview" / "kicad_gerber").is_file(),
        "build_gui": (b / "pcbnew" / "pcbnew").is_file() or (b / "kicad" / "kicad").is_file(),
    }
    sys_cli = shutil.which("kicad-cli")
    info = {"build": str(b), "mode": "build" if have["build_cli"] else ("system" if sys_cli else "none")}
    # kicad-cli: the one kicad-plus uses
    rc, out = run([str(HOME / "bin" / "kicad-cli-plus"), "version"])
    info["kicad_cli"] = out.splitlines()[-1] if rc == 0 and out else None
    info["system_kicad_cli"] = sys_cli
    # pcbnew from Python, as kicad-python gives it
    rc, out = run([str(HOME / "bin" / "kicad-python"), "-c", "import pcbnew; print(pcbnew.GetBuildVersion())"])
    info["pcbnew"] = out.splitlines()[-1] if rc == 0 and out else None
    # Freerouting and java, as autoroute finds them
    sys.path.insert(0, str(HOME / "tools"))
    jar = java = None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("kar", HOME / "tools" / "kautoroute.py")
        src = (HOME / "tools" / "kautoroute.py").read_text(encoding="utf-8").replace("\nimport pcbnew\n", "\n")
        mod = importlib.util.module_from_spec(spec)
        exec(compile(src, str(spec.origin), "exec"), mod.__dict__)
        jar = mod.find_jar(None)
        java = mod.find_java(jar) if jar else shutil.which("java")
    except Exception as e:  # noqa: BLE001
        info["freerouting_error"] = str(e)
    info["freerouting"] = str(jar) if jar else None
    info["java"] = java
    info["ngspice"] = ctypes.util.find_library("ngspice")
    info["pdftotext"] = shutil.which("pdftotext")
    try:
        import numpy  # noqa: F401
        info["numpy"] = True
    except ImportError:
        info["numpy"] = False
    return info, have


def commands(info, have):
    """[(命令, 使えるか, 足りない物)]"""
    kicad = info["kicad_cli"] is not None
    py = info["pcbnew"] is not None
    need_build = f"改良版のビルド（{RELEASES} の配布版）"
    rows = [
        ("drc・erc・stats・fab・netlist（回路図）・lint・sch・review", kicad, "kicad-cli"),
        ("netlist（基板）・bom・models・why・view・finish・pcbsync・silkfix", py, "Python の pcbnew"),
        ("autoroute", py and info["freerouting"] is not None and info["java"] is not None,
         "Python の pcbnew と Freerouting（FREEROUTING_JAR）と java"),
        ("render・step", kicad, "kicad-cli"),
        ("route（配線・部品の移動・findspace・drag・silk）", have["build_route"], need_build),
        ("gerber（ガーバーの情報と差分）", have["build_gerber"], need_build),
        ("画面（kicad-plus-gui・pcbnew-plus・eeschema-plus）", have["build_gui"], need_build),
        ("sim（ngspice）", info["ngspice"] is not None and info["numpy"], "libngspice と numpy"),
        ("bom のデータシート読み", info["pdftotext"] is not None, "pdftotext（poppler-utils）"),
    ]
    return rows


def main():
    ap = argparse.ArgumentParser(prog="kicad-plus doctor", description=__doc__.split("\n")[1])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    info, have = check()
    rows = commands(info, have)
    if a.json:
        print(json.dumps({"info": info, "build": have,
                          "commands": [{"commands": c, "ok": ok, "needs": n} for c, ok, n in rows]},
                         ensure_ascii=False, indent=1))
    else:
        mode = {"build": "改良版のビルド", "system": "本家の KiCad（改良版のビルドが無いので、使える命令が減る）",
                "none": "KiCad が見つからない"}[info["mode"]]
        print(f"動く形: {mode}")
        print(f"  改良版のビルドの場所: {info['build']}{'' if have['build_cli'] else '（無い）'}")
        print(f"  kicad-cli: {info['kicad_cli'] or '使えない'}{'（本家: ' + info['system_kicad_cli'] + '）' if info['mode'] == 'system' else ''}")
        print(f"  Python の pcbnew: {info['pcbnew'] or '使えない'}")
        print(f"  Freerouting: {info['freerouting'] or '無い（FREEROUTING_JAR で指定できる）'}、java: {info['java'] or '無い'}")
        print(f"  ngspice: {info['ngspice'] or '無い'}、numpy: {'ある' if info['numpy'] else '無い'}、"
              f"pdftotext: {info['pdftotext'] or '無い'}")
        print("命令:")
        for c, ok, n in rows:
            print(f"  {'○' if ok else '×'} {c}" + ("" if ok else f"  ← 要るもの: {n}"))
    return 0 if info["kicad_cli"] else 1


if __name__ == "__main__":
    sys.exit(main())
