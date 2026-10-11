# -*- coding: utf-8 -*-
"""统一出图样式：中文字体 + 落图后处理。

为什么需要这个
--------------
两个模型的内核在 import 时会把 matplotlib 字体设成 Windows 字体
（Microsoft YaHei / SimHei）。搬到 Linux 服务器或云沙箱后这些字体不存在，
所有中文标题、坐标轴标签都会变成"豆腐块"方框 —— 图还是能出，但没法看。

所以本模块做两件事：

1. **内置字体**：assets/fonts/NotoSansSC-Regular.ttf（Noto Sans SC，OFL 许可，
   可随项目分发）注册进 matplotlib 的字体库，并把 sans-serif 首选设为它。
   必须在**内核模块 import 之后**调用 apply() 才能覆盖内核的设置。
2. **统一 rcParams**：中文字号、DPI、图幅、负号显示等，让三个服务的图长得一致。

apply() 是幂等的，可重复调用。
"""
from __future__ import annotations

import os
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# 优先用内置字体；缺失时按可用性退回来的系统中文字体名
FALLBACK_NAMES = ["Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC",
                  "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Microsoft YaHei",
                  "SimHei", "Droid Sans Fallback", "DejaVu Sans"]

_applied = False
_font_name = None


def available_cjk_font() -> str:
    """返回当前 matplotlib 能用的中文字体名（找不到就退 DejaVu Sans）。"""
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in FALLBACK_NAMES:
        if name in installed:
            return name
    return "DejaVu Sans"


def register_bundled_fonts() -> list[str]:
    """把 assets/fonts 下的字体注册进 matplotlib，返回注册成功的字体名。"""
    from matplotlib import font_manager

    added = []
    if not FONT_DIR.exists():
        return added
    for p in sorted(FONT_DIR.glob("*.ttf")) + sorted(FONT_DIR.glob("*.otf")):
        try:
            font_manager.fontManager.addfont(str(p))
            added.append(font_manager.FontProperties(fname=str(p)).get_name())
        except Exception:                                # noqa: BLE001
            continue
    return added


def apply(verbose: bool = True) -> str:
    """注册内置字体并统一 rcParams。返回实际使用的中文字体名。"""
    global _applied, _font_name

    import matplotlib
    matplotlib.use("Agg", force=True)                    # 无显示环境，必须用 Agg
    import matplotlib.pyplot as plt
    from matplotlib import rcParams

    register_bundled_fonts()
    name = available_cjk_font()

    rcParams["font.sans-serif"] = [name] + [n for n in FALLBACK_NAMES if n != name]
    rcParams["font.family"] = "sans-serif"
    rcParams["axes.unicode_minus"] = False               # 负号别用 U+2212
    rcParams["figure.dpi"] = 110
    rcParams["savefig.dpi"] = 150
    rcParams["savefig.bbox"] = "tight"
    rcParams["axes.grid"] = True
    rcParams["grid.alpha"] = 0.28
    rcParams["grid.linestyle"] = "--"
    rcParams["axes.titlesize"] = 13
    rcParams["axes.labelsize"] = 11
    rcParams["xtick.labelsize"] = 9.5
    rcParams["ytick.labelsize"] = 9.5
    rcParams["legend.fontsize"] = 9.5
    rcParams["font.size"] = 10.5
    rcParams["figure.autolayout"] = False

    plt.rcParams.update(rcParams)
    _applied = True
    _font_name = name
    if verbose:
        env = "内置" if name == "Noto Sans SC" else ("系统" if name != "DejaVu Sans" else "缺失(中文将显示为方框)")
        print(f"[出图] 中文字体 = {name} ({env})", flush=True)
    return name


def font_status() -> dict:
    """供 /api/env 展示字体可用性。"""
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    bundled = FONT_DIR.exists() and any(FONT_DIR.glob("*.ttf"))
    return {
        "bundled_font_file": bundled,
        "font_dir": str(FONT_DIR),
        "resolved": _font_name or available_cjk_font(),
        "cjk_ok": (_font_name or available_cjk_font()) != "DejaVu Sans",
        "installed_cjk": [n for n in FALLBACK_NAMES if n in installed],
        "MPLCONFIGDIR": os.environ.get("MPLCONFIGDIR", ""),
    }


def ensure_writable_mpl_cache() -> None:
    """matplotlib 需要可写的缓存目录；容器里 HOME 可能不可写。"""
    if os.environ.get("MPLCONFIGDIR"):
        return
    base = os.environ.get("WTH_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp"
    try:
        d = Path(base) / "mplconfig"
        d.mkdir(parents=True, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = str(d)
    except OSError:
        pass
