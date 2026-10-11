# -*- coding: utf-8 -*-
"""作物生长模型微服务 —— 内核桥接层。

原则：**算法原样复用，不改写内核**。本文件只做四件事：

1. 把内核目录挂到 sys.path 最前（`config` 包会被 site-packages 里的同名 PyPI 包
   遮蔽，必须 insert(0)，等价于"在模型目录下直接跑"的解析顺序）；
2. 把内核的输出目录重定向到可写的运行期目录 —— 内核配置模块在 import 时会把
   OUTPUT_DIR 绑给使用方，所以**必须在 import 内核之前**替换；
3. 在内核 import 之后统一中文字体（内核自己设的是 Windows 字体，服务器上没有）；
4. 在既有函数外面套一层"收集产物 + 结构化结果"的包装。

复用入口：
    config.wutonghe_config        路径/生育期/气象文件定位
    modules.atmosphere            _normalize_columns（气象列名归一）
    run_scenarios                 run_crop_optimization / build_optimization_table
                                  / find_optimal / plot_*（3 类单作物图 + 多作物对比图）
    spac_model                    SimulationResult.save_csv / save_original_excel
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from core import style
from core.config import CONFIG
from core.tables import df_records, file_entries

KERNEL_DIR: Path = CONFIG.spac_dir
# 内核自己会把中间产物写进 OUTPUT_DIR，这里统一落到 work/_kernel/（不外泄给用户）
KERNEL_OUT = CONFIG.work_dir / "_kernel_spac"

_booted = False
_rs = None
_wc = None

VERSION = "1.0.0"

# (内核绘图函数名, 输出文件名模板, 前端面板标题)
FIG_SPECS = (
    ("plot_yield_irrigation_curve", "yield_irr_curve_{crop}_{year}.png",
     "产量-灌水量响应曲线"),
    ("plot_water_dynamics", "water_dynamics_{crop}_normal_{year}.png",
     "水分动态（正常灌溉对照）"),
    ("plot_irrigation_schedule", "irrigation_schedule_{crop}_{year}.png",
     "灌溉制度时序对比"),
)


def bootstrap() -> None:
    """幂等初始化：字体缓存 → sys.path → 输出目录重定向 → 导入内核 → 出图样式。"""
    global _booted, _rs, _wc
    if _booted:
        return

    style.ensure_writable_mpl_cache()                    # 必须先于 import matplotlib

    if not KERNEL_DIR.exists():
        raise RuntimeError(f"SPAC 内核目录不存在: {KERNEL_DIR}\n"
                           f"请先运行 kernel/同步内核.py 生成，或修改 平台配置.json 的 paths.spac_kernel")

    if str(KERNEL_DIR) not in sys.path:
        sys.path.insert(0, str(KERNEL_DIR))

    # ---- 重定向内核输出目录（务必在 import run_scenarios 之前）----
    import config.wutonghe_config as wc                  # noqa: N813
    KERNEL_OUT.mkdir(parents=True, exist_ok=True)
    wc.OUTPUT_DIR = KERNEL_OUT
    wc.FIG_DIR = KERNEL_OUT / "figures"
    wc.FIG_DIR.mkdir(parents=True, exist_ok=True)

    import run_scenarios as rs                           # noqa: N813  （会连带导入 spac_model）
    _rs, _wc = rs, wc

    style.apply()                                        # 内核设的 Windows 字体在这里被覆盖
    _booted = True


def _ensure():
    bootstrap()
    return _rs, _wc


# --------------------------------------------------------------------------- #
#  气象数据
# --------------------------------------------------------------------------- #
def available_years() -> list[int]:
    """扫描内核 data 目录，返回已有气象数据的年份（era5 命名优先）。"""
    _, wc = _ensure()
    years: dict[int, Path] = {}
    if wc.DATA_DIR.exists():
        for pattern in ("weather_era5_*.csv", "weather_*.csv"):
            for p in sorted(wc.DATA_DIR.glob(pattern)):
                stem = p.stem.replace("weather_era5_", "").replace("weather_", "")
                if stem.isdigit():
                    years.setdefault(int(stem), p)
    return sorted(years)


def data_dir() -> Path:
    _, wc = _ensure()
    return Path(wc.DATA_DIR)


def weather_period() -> tuple[str, str]:
    _, wc = _ensure()
    return wc.SIM_START_MD, wc.SIM_END_MD


class WeatherImportError(ValueError):
    """气象 CSV 校验失败，消息面向最终用户，可原样展示。"""


_SAVE_COLS = {
    "Date": "日期", "Tmin": "最低气温", "Tmax": "最高气温",
    "Prcp": "降雨量", "Wind": "风速", "RH": "相对湿度", "Rs": "太阳辐射",
}
REQUIRED = ("Date", "Tmin", "Tmax", "Prcp", "Wind", "RH", "Rs")
_CN = {"Date": "日期", "Tmin": "最低气温", "Tmax": "最高气温", "Prcp": "降雨量",
       "Wind": "风速", "RH": "相对湿度", "Rs": "太阳辐射"}


def validate_weather_csv(src_path: Path):
    """校验外部逐日气象 CSV。

    与桌面版 spac_app.validate_weather_csv **同一套规则**：7 列齐全（中英文列名
    均可，自动识别 UTF-8/BOM/GBK 编码）→ 日期可解析 → 关键列无缺失 → 覆盖模拟期。
    这里之所以重写而不是调用 spac_app，是因为 spac_app 在模块级 import tkinter，
    服务器/容器里通常没有 GUI 依赖。规则本身逐条对齐。

    返回 (标准化 df, 年份, 模拟期天数, 应有天数)。
    """
    import pandas as pd

    _ensure()
    from modules.atmosphere import _normalize_columns

    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            df = pd.read_csv(src_path, encoding=enc)
            break
        except UnicodeDecodeError:
            df = None
            continue
        except Exception as e:                           # noqa: BLE001
            raise WeatherImportError(f"读取 CSV 失败：{type(e).__name__}: {e}") from e
    if df is None:
        raise WeatherImportError("无法识别文件编码，请用 Excel 重新另存为 CSV 后再试。")

    df = _normalize_columns(df)

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        need = "、".join(_CN.get(c, c) for c in missing)
        raise WeatherImportError(f"CSV 缺少必需列：{need}\n"
                                 "（需要 7 列：日期 最低气温 最高气温 降雨量 风速 相对湿度 太阳辐射）")

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    if df["Date"].isna().any():
        raise WeatherImportError("日期列存在无法识别的值。\n"
                                 "请检查日期格式（如 2026-04-01 或 2026/4/1）。")

    n_dup = int(df["Date"].duplicated().sum())
    if n_dup:
        df = df.drop_duplicates(subset="Date", keep="first")
    df = df.sort_values("Date").reset_index(drop=True)

    n_nan = int(df[list(REQUIRED)].isna().sum().sum())
    if n_nan:
        raise WeatherImportError(f"数据包含 {n_nan} 个空缺失值。\n请补全后再导入（缺失天数不允许）。")

    year = int(df["Date"].dt.year.mode().iloc[0])
    start_md, end_md = weather_period()
    start = pd.Timestamp(f"{year}/{start_md}")
    end = pd.Timestamp(f"{year}/{end_md}")
    expected = (end - start).days + 1
    n_days = int(((df["Date"] >= start) & (df["Date"] <= end)).sum())
    if n_days == 0:
        raise WeatherImportError(
            f"数据中不包含 {year} 年 {start_md} ~ {end_md}（模拟期）的记录。\n"
            "请确认 CSV 覆盖当年 4 月 1 日至 10 月 15 日。")
    return df, year, n_days, expected, n_dup


def import_weather(src_path: Path, override_era5: bool = False) -> dict:
    """校验并落盘气象 CSV（覆盖同名年份数据）。

    override_era5=False 且同年已存在 `weather_era5_{year}.csv` 时抛 FileExistsError ——
    该文件优先级更高会屏蔽本次导入，必须先经用户确认。
    """
    import pandas as pd

    df, year, n_days, expected, n_dup = validate_weather_csv(src_path)
    ddir = data_dir()
    era5 = ddir / f"weather_era5_{year}.csv"
    had_era5 = era5.exists()
    if had_era5 and not override_era5:
        raise FileExistsError(
            f"{era5.name} 已存在且优先级更高，本次导入不会生效。\n"
            f"确认要用本次导入的 {year} 年数据替换它吗？")

    ddir.mkdir(parents=True, exist_ok=True)
    dst = ddir / f"weather_{year}.csv"
    out = df.rename(columns=_SAVE_COLS)[list(_SAVE_COLS.values())].copy()
    out["日期"] = out["日期"].dt.strftime("%Y-%m-%d")
    out.to_csv(dst, index=False, encoding="utf-8-sig")

    if had_era5:
        try:
            era5.unlink()
        except OSError:
            pass

    start_md, end_md = weather_period()
    return {
        "ok": True,
        "year": int(year),
        "n_days": int(n_days),
        "expected": int(expected),
        "missing_days": int(expected - n_days),
        "duplicated_days_ignored": int(n_dup),
        "prcp": round(float(df["Prcp"].sum()), 1),
        "rs_mean": round(float(df["Rs"].mean()), 2),
        "tmin_mean": round(float(df["Tmin"].mean()), 2),
        "tmax_mean": round(float(df["Tmax"].mean()), 2),
        "saved_to": str(dst),
        "replaced_era5": bool(had_era5 and override_era5),
        "period": f"{year}-{start_md.replace('/', '-')} ~ {year}-{end_md.replace('/', '-')}",
    }


def weather_preview(year: int, limit: int = 12) -> dict:
    """读回已有年份气象数据的前若干行，供页面表格预览。"""
    import pandas as pd

    _, wc = _ensure()
    path = wc.get_weather_csv_path(year)
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"未找到 {year} 年气象数据文件"}
    df = pd.read_csv(p, encoding="utf-8-sig")
    head = df.head(limit)
    stats = {
        "行数": int(len(df)),
        "总降雨(mm)": round(float(df["降雨量"].sum()), 1) if "降雨量" in df else None,
        "日均太阳辐射(MJ/m²)": round(float(df["太阳辐射"].mean()), 2) if "太阳辐射" in df else None,
        "平均气温(℃)": round(float((df["最低气温"] + df["最高气温"]).mean() / 2), 2)
        if {"最低气温", "最高气温"} <= set(df.columns) else None,
    }
    return {"ok": True, "file": str(p), "table": df_records(head), "stats": stats}


# --------------------------------------------------------------------------- #
#  作物需水量（基于正常灌溉对照方案的逐月统计）
# --------------------------------------------------------------------------- #
def water_requirement(results, crop: str, year: int,
                      fig_path: Path | None = None, log=print) -> dict | None:
    """统计单作物逐月需水量，返回结构化结果（供页面表格 / CSV / KPI 用）。

    数据口径
    --------
    * ETc（作物需水量）＝ 内核逐日「蒸腾蒸发量(mm)」（蒸腾 Tr + 棵间蒸发 Es）；
    * ET0（参考需水量）＝ 与内核同一套 FAO-56 Penman-Monteith 实现
      （modules.atmosphere.build_weather_df），只累计生育期内的天数；
    * Kc（作物系数）＝ ETc / ET0，月值与全期值同口径相除；
    * 降雨 / 灌溉 ＝ 正常灌溉对照（CK）方案的逐日值。

    CK 方案代表当地常规灌溉管理水平，是灌溉规划里需水量的标准口径。
    """
    import pandas as pd

    rs, wc = _ensure()
    from modules.atmosphere import build_weather_df

    ck = next((r for r in results
               if r.crop == crop and r.label == rs.NORMAL_LABEL), None)
    if ck is None:
        return None

    wdf = build_weather_df(wc.get_weather_csv_path(year), wc.LAT_RAD, wc.ALTITUDE_M)

    d = ck.daily[["模拟日期", "蒸腾蒸发量(mm)", "每日灌溉量(mm)"]].copy()
    d["日期"] = pd.to_datetime(d["模拟日期"])
    m = (d.merge(wdf[["Date", "ReferenceET", "Precipitation"]],
                 left_on="日期", right_on="Date", how="left")
         .drop(columns=["Date"]))
    if m["ReferenceET"].isna().any():        # 生育期超出气象覆盖时丢弃多余天
        m = m.dropna(subset=["ReferenceET"])
    if m.empty:
        return None

    def _kc(etc: float, et0: float):
        return round(float(etc) / float(et0), 3) if et0 > 1e-6 else None

    m["月"] = m["日期"].dt.month
    g = m.groupby("月").agg(
        et0=("ReferenceET", "sum"),
        etc=("蒸腾蒸发量(mm)", "sum"),
        prcp=("Precipitation", "sum"),
        irr=("每日灌溉量(mm)", "sum"),
    ).reset_index()

    rows = [[f"{int(r.月)}月", round(r.et0, 1), round(r.etc, 1),
             _kc(r.etc, r.et0), round(r.prcp, 1), round(r.irr, 1)]
            for r in g.itertuples()]
    tot = g[["et0", "etc", "prcp", "irr"]].sum()
    rows.append(["全生育期合计", round(tot.et0, 1), round(tot.etc, 1),
                 _kc(tot.etc, tot.et0), round(tot.prcp, 1), round(tot.irr, 1)])

    out = {
        "crop": crop,
        "name": rs.CROP_CN[crop],
        "period": f"{m['日期'].iloc[0]:%m-%d} ~ {m['日期'].iloc[-1]:%m-%d}",
        "monthly": {
            "columns": ["时段", "参考需水量ET0(mm)", "作物需水量ETc(mm)",
                        "作物系数Kc", "降雨量(mm)", "灌水量(mm)"],
            "rows": rows,
        },
        "total_et0": round(float(tot.et0), 1),
        "total_etc": round(float(tot.etc), 1),
        "total_prcp": round(float(tot.prcp), 1),
        "total_irr": round(float(tot.irr), 1),
        "kc": _kc(tot.etc, tot.et0),
    }

    if fig_path is not None:
        try:
            _plot_water_requirement(m, rs.CROP_CN[crop], year, fig_path)
        except Exception as e:                               # noqa: BLE001
            log(f"  ⚠ 需水过程图失败: {type(e).__name__}: {e}")
    return out


def _plot_water_requirement(day_df, crop_cn: str, year: int, path: Path) -> None:
    """逐日需水过程图：降雨/灌溉堆叠柱 + ET0 虚线 + ETc 实线。"""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    x = day_df["日期"]
    prcp = day_df["Precipitation"].clip(lower=0)
    irr = day_df["每日灌溉量(mm)"].clip(lower=0)

    fig, ax = plt.subplots(figsize=(9.5, 4.0), dpi=150)
    ax.bar(x, prcp, width=1.0, color="#8dc6e8", label="降雨量")
    ax.bar(x, irr, width=1.0, bottom=prcp, color="#f4a259", label="灌水量")
    ax.plot(x, day_df["ReferenceET"], "--", color="#8d99ae", lw=1.3,
            label="参考需水量 ET0")
    ax.plot(x, day_df["蒸腾蒸发量(mm)"], "-", color="#d62828", lw=1.5,
            label="作物需水量 ETc")
    ax.set_title(f"{crop_cn}需水过程（{year} 年 · 正常灌溉对照）", fontsize=11)
    ax.set_xlabel("日期")
    ax.set_ylabel("mm / 天")
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.legend(loc="upper left", ncols=4, fontsize=8.5, framealpha=0.9)
    ax.grid(axis="y", ls=":", alpha=0.5)
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


# --------------------------------------------------------------------------- #
#  运行
# --------------------------------------------------------------------------- #
def run(crops: list[str], year: int, out_dir: Path, progress=None) -> dict:
    """跑 8 方案优化并出图，把所有交付物落到 out_dir。

    与桌面版 spac_app.run_year 等价；差异只在图件与数据的落盘位置 —— 这里全部
    写进本任务的输出目录，便于 Web 端按任务隔离、直接预览与下载。
    """
    rs, wc = _ensure()
    from config.wutonghe_config import ensure_dirs

    ensure_dirs()
    out_dir = Path(out_dir)
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    log = print
    log("#" * 62)
    log(f"#  {year} 年 | {' + '.join(rs.CROP_CN[c] for c in crops)} | 共 {8 * len(crops)} 方案")
    log("#" * 62)

    year_results = []
    figures: list[dict] = []
    for crop in crops:
        if progress:
            progress(f"{rs.CROP_CN[crop]} 模拟中")
        results = rs.run_crop_optimization(crop, year)
        year_results.extend(results)
        if progress:
            progress(f"{rs.CROP_CN[crop]} 模拟完成，开始出图")
        for fn_name, tmpl, title in FIG_SPECS:
            fname = tmpl.format(crop=crop, year=year)
            fpath = fig_dir / fname
            try:
                getattr(rs, fn_name)(results, crop, year, str(fpath))
                if fpath.exists():
                    figures.append({
                        "key": f"{fn_name}:{crop}",
                        "title": f"{title}（{rs.CROP_CN[crop]}）",
                        "group": rs.CROP_CN[crop],
                        "rel": f"figures/{fname}",
                        "size": fpath.stat().st_size,
                    })
            except Exception as e:                       # noqa: BLE001
                log(f"  ⚠ 图件生成失败 {fname}: {type(e).__name__}: {e}")

    # ---- 逐日结果与 AquaCrop 原始 Excel ----
    daily_files, excel_files = [], []
    for r in year_results:
        # 文件名用稳定档位 ID(L1–L7 / 对照 CK), 与中文显示名解耦
        tag = f"{r.crop}_{rs.scheme_id(r.label)}_{year}"
        try:
            r.save_csv(str(out_dir / f"daily_{tag}.csv"))
            daily_files.append(f"daily_{tag}.csv")
        except Exception as e:                           # noqa: BLE001
            log(f"  ⚠ save_csv 失败 ({tag}): {e}")
        try:
            r.save_original_excel(str(out_dir / f"aqua_original_{tag}.xlsx"))
            excel_files.append(f"aqua_original_{tag}.xlsx")
        except Exception as e:                           # noqa: BLE001
            log(f"  ⚠ save_original_excel 失败 ({tag}): {e}")

    # ---- 汇总表 / 最优方案 ----
    summary = rs.build_optimization_table(year_results, year)
    summary.to_csv(out_dir / f"irrigation_optimization_{year}.csv",
                   index=False, encoding="utf-8-sig")
    log(f"\n汇总表已保存 (irrigation_optimization_{year}.csv)")
    log(summary.to_string(index=False))

    optimal = rs.find_optimal(summary)
    optimal_files = []
    if not optimal.empty:
        optimal.to_csv(out_dir / f"optimal_irrigation_{year}.csv",
                       index=False, encoding="utf-8-sig")
        optimal_files.append(f"optimal_irrigation_{year}.csv")
        log(f"\n★ {year} 年经济最优灌溉方案（产量达 95% 最大值）：")
        log(optimal.to_string(index=False))

    # ---- 作物需水量（逐月 ET0 / ETc / Kc / 降雨 / 灌溉，正常灌溉对照）----
    import pandas as pd

    water_req = None
    wr_crops = []
    for crop in crops:
        if progress:
            progress(f"{rs.CROP_CN[crop]} 需水量统计")
        fname = f"water_requirement_{crop}_{year}.png"
        wr = water_requirement(year_results, crop, year,
                               fig_dir / fname, log=log)
        if wr is None:
            continue
        if (fig_dir / fname).exists():
            figures.append({
                "key": f"water_req:{crop}",
                "title": f"作物需水过程（{rs.CROP_CN[crop]}）",
                "group": rs.CROP_CN[crop],
                "rel": f"figures/{fname}",
                "size": (fig_dir / fname).stat().st_size,
            })
        f = pd.DataFrame(wr["monthly"]["rows"], columns=wr["monthly"]["columns"])
        f.insert(0, "作物", wr["name"])
        wr_crops.append((wr, f))

    if wr_crops:
        wr_csv = f"water_requirement_{year}.csv"
        pd.concat([f for _, f in wr_crops], ignore_index=True) \
          .to_csv(out_dir / wr_csv, index=False, encoding="utf-8-sig")
        water_req = {
            "year": year,
            "basis": rs.NORMAL_LABEL,
            "crops": [wr for wr, _ in wr_crops],
            "csv": wr_csv,
        }
        log(f"\n需水量统计已保存 ({wr_csv})")

    # ---- 多作物对比图（两个作物都跑时才有意义）----
    if len(crops) > 1:
        fname = f"multi_crop_comparison_{year}.png"
        fpath = fig_dir / fname
        try:
            rs.plot_multi_crop_comparison(summary, year, str(fpath))
            if fpath.exists():
                figures.append({
                    "key": "plot_multi_crop_comparison:all",
                    "title": "水稻 / 玉米 方案对比",
                    "group": "综合对比",
                    "rel": f"figures/{fname}",
                    "size": fpath.stat().st_size,
                })
        except Exception as e:                           # noqa: BLE001
            log(f"  ⚠ 多作物对比图失败: {type(e).__name__}: {e}")

    deliv = [f"irrigation_optimization_{year}.csv"] + optimal_files \
        + ([water_req["csv"]] if water_req else []) \
        + daily_files + excel_files

    # 关键指标速览（页面顶部卡片）
    kpi = []
    if not optimal.empty:
        for _, row in optimal.iterrows():
            kpi.append({
                "作物": row.get("作物"),
                "最优方案": row.get("最优方案"),
                "灌溉参数": row.get("灌溉参数"),
                "累计灌溉量(mm)": row.get("累计灌溉量(mm)"),
                "产量(t/ha)": row.get("产量(t/ha)"),
                "最大产量(t/ha)": row.get("最大产量(t/ha)"),
                "产量达最大百分比(%)": row.get("产量达最大百分比(%)"),
                "灌溉WUE(kg/m³)": row.get("灌溉WUE(kg/m³)"),
            })

    return {
        "type": "spac",
        "year": year,
        "crops": crops,
        "crop_names": [rs.CROP_CN[c] for c in crops],
        "n_scenarios": len(year_results),
        "kpi": kpi,
        "summary": df_records(summary),
        "optimal": df_records(optimal) if not optimal.empty else None,
        "water_req": water_req,
        "figures": figures,
        "files": file_entries(out_dir, deliv),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------- #
#  环境自检
# --------------------------------------------------------------------------- #
def probe_env() -> dict:
    items = []

    def add(name, ok, detail=""):
        items.append({"name": name, "ok": bool(ok), "detail": str(detail)[:160]})

    add("SPAC 内核目录", KERNEL_DIR.exists(), KERNEL_DIR)
    for mod, label in [("numpy", "numpy"), ("pandas", "pandas"),
                       ("matplotlib", "matplotlib"), ("scipy", "scipy"),
                       ("aquacrop", "aquacrop (AquaCrop-OSPy)"),
                       ("openpyxl", "openpyxl")]:
        try:
            m = __import__(mod)
            import importlib.metadata as md
            add(label, True, getattr(m, "__version__", None) or md.version(mod))
        except Exception as e:                           # noqa: BLE001
            add(label, False, f"{type(e).__name__}: {e}")

    try:
        bootstrap()
        add("内核可加载", True, "run_scenarios / spac_model 已导入")
    except Exception as e:                               # noqa: BLE001
        add("内核可加载", False, f"{type(e).__name__}: {e}")
        return {"ok": False, "items": items, "font": {}}

    try:
        ys = available_years()
        add("已有气象年份", bool(ys), "、".join(str(y) for y in ys) or "无")
    except Exception as e:                               # noqa: BLE001
        add("已有气象年份", False, str(e))

    fs = style.font_status()
    add("中文字体", fs["cjk_ok"], f"{fs['resolved']}"
        + ("（内置 Noto Sans SC）" if fs["bundled_font_file"] else ""))

    return {"ok": all(i["ok"] for i in items), "items": items, "font": fs}
