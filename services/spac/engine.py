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
import re
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

VERSION = "1.13.0"

# (内核绘图函数名, 输出文件名模板, 前端面板标题)
FIG_SPECS = (
    ("plot_yield_irrigation_curve", "yield_irr_curve_{crop}_{year}.png",
     "产量-灌水量响应曲线"),
    ("plot_water_dynamics", "water_dynamics_{crop}_normal_{year}.png",
     "水分动态"),
    ("plot_irrigation_schedule", "irrigation_schedule_{crop}_{year}.png",
     "灌溉制度时序对比"),
)


def bootstrap() -> None:
    """幂等初始化：字体缓存 → sys.path → 输出目录重定向 → 导入内核 → 出图样式。"""
    global _booted, _rs, _wc, _BUILTIN_DATA
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
    # ★ 记住内核自带的只读气象目录（era5 下载的 2023–2025）。每个用户跑模拟时
    #   wc.DATA_DIR 会被切到自己的私有目录，这里留一份原始值当「内置年份」的来源。
    _BUILTIN_DATA = Path(wc.DATA_DIR)

    style.apply()                                        # 内核设的 Windows 字体在这里被覆盖
    _booted = True


def _ensure():
    bootstrap()
    return _rs, _wc


def n_schemes(crop: str) -> int:
    """某作物的灌溉方案数（正常灌溉对照 CK + 水量梯度档位）。

    从内核实时取，**不要在任何地方硬编码档位数** —— 之前写死的 8 在
    「7 档改 5 档」后全部失效，页面标题和方案数会与实际表格不一致。
    """
    rs, _ = _ensure()
    return len(rs.get_smt_schemes(crop))


# --------------------------------------------------------------------------- #
#  气象数据
# --------------------------------------------------------------------------- #
#  隔离口径
#  --------
#  · 内置 era5（weather_era5_2023/2024/2025）随镜像发布，**所有用户共享只读**；
#  · 用户自己上传的 CSV 落在 work/weather/<用户名>/，只有本人可见可用；
#  · 内核只认一个 wc.DATA_DIR，所以运行前把当前用户的私有目录激活（顺带把内置
#    era5 复制一份进去），跑完不恢复 —— 任务队列本身就是串行的，同一时刻只有一个
#    用户在跑，不存在互相踩。
_BUILTIN_DATA: Path | None = None
_RE_YEAR_CSV = re.compile(r"^weather_(?:era5_)?(\d{4})\.csv$")


def builtin_data_dir() -> Path:
    _ensure()
    return _BUILTIN_DATA or Path(_wc.DATA_DIR)


def _safe_user(owner: str) -> str:
    """用户名直接当目录名用 → 必须先收敛（账号文件是可信的，但别留后门）。"""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", (owner or "").strip())
    return name or "_shared"


def weather_private_dir(owner: str) -> Path:
    d = Path(CONFIG.work_dir) / "weather" / _safe_user(owner)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_builtin_weather(dst: Path) -> None:
    """把内置 era5 复制进私有目录（只补缺，不覆盖用户已上传的同年数据）。"""
    src = builtin_data_dir()
    if not src.exists() or src.resolve() == dst.resolve():
        return
    for p in src.glob("weather_era5_[0-9][0-9][0-9][0-9].csv"):
        t = dst / p.name
        if t.exists():
            continue
        try:
            import shutil
            shutil.copyfile(p, t)
        except OSError:
            pass


def activate_weather(owner: str) -> Path:
    """把内核的气象目录切到该用户的私有目录（调用前会补上内置 era5）。"""
    _, wc = _ensure()
    d = weather_private_dir(owner)
    _seed_builtin_weather(d)
    wc.DATA_DIR = d
    return d


def available_years(owner: str | None = None) -> list[int]:
    """可用年份 = 内置 era5 年份 ∪ 该用户私有年份。"""
    _ensure()
    years: dict[int, Path] = {}
    src = builtin_data_dir()
    if src.exists():
        for p in sorted(src.glob("weather_era5_[0-9][0-9][0-9][0-9].csv")):
            m = _RE_YEAR_CSV.match(p.name)
            if m:
                years.setdefault(int(m.group(1)), p)
    if owner:
        d = weather_private_dir(owner)
        for pattern in ("weather_era5_*.csv", "weather_*.csv"):
            for p in sorted(d.glob(pattern)):
                m = _RE_YEAR_CSV.match(p.name)
                if m:
                    years[int(m.group(1))] = p      # 私有优先
    return sorted(years)


def weather_csv_path(year: int, owner: str | None = None) -> Path | None:
    """按「私有 era5 → 私有普通 → 内置 era5」的优先级找气象文件。"""
    _ensure()
    cands: list[Path] = []
    if owner:
        d = weather_private_dir(owner)
        cands += [d / f"weather_era5_{year}.csv", d / f"weather_{year}.csv"]
    src = builtin_data_dir()
    cands += [src / f"weather_era5_{year}.csv", src / f"weather_{year}.csv"]
    for p in cands:
        if p.exists():
            return p
    return None


def data_dir(owner: str | None = None) -> Path:
    """内核当前生效的气象目录（给页面显示用）。"""
    _ensure()
    return weather_private_dir(owner) if owner else Path(_wc.DATA_DIR)


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


def import_weather(src_path: Path, override_era5: bool = False,
                   owner: str = "") -> dict:
    """校验并落盘气象 CSV（覆盖同名年份数据）。

    给了 owner 就写进**该用户的私有目录**（其他用户看不到、也用不到）；
    否则退回内核内置目录（仅内部调用/兼容旧代码用）。

    override_era5=False 且同年已存在 `weather_era5_{year}.csv` 时抛 FileExistsError ——
    该文件优先级更高会屏蔽本次导入，必须先经用户确认。
    """
    import pandas as pd

    df, year, n_days, expected, n_dup = validate_weather_csv(src_path)
    ddir = weather_private_dir(owner) if owner else data_dir()
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


def weather_preview(year: int, limit: int = 12, owner: str | None = None) -> dict:
    """读回已有年份气象数据的前若干行，供页面表格预览。"""
    import pandas as pd

    _ensure()
    p = weather_csv_path(year, owner)
    if p is None:
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
    * 灌溉＝CK逐日总净量（含移栽日净泡田储水估算），与优化汇总、KPI同口径。

    CK 方案代表当地常规灌溉管理水平，是灌溉规划里需水量的标准口径。
    """
    import pandas as pd

    rs, wc = _ensure()
    from modules.atmosphere import build_weather_df
    # 生育期与出苗天数取自内核配置，避免在页面侧硬编码日期
    from config.simulation_settings import planting_date
    from modules.crop_sim import build_crop
    _PLANTING = {c: planting_date(c) for c in ("rice", "corn")}

    ck = next((r for r in results
               if r.crop == crop and ((crop == "corn" and "墒情优化" in r.summary) or (crop == "rice" and r.label == "常规淹水"))), None)
    if ck is None:
        return None

    wdf = build_weather_df(wc.get_weather_csv_path(year), wc.LAT_RAD, wc.ALTITUDE_M)

    d = ck.daily[["模拟日期", "蒸腾蒸发量(mm)", "每日灌溉量(mm)"]].copy()
    d["每日灌溉量(mm)"] = ck.daily["每日总净灌溉量(mm)"]
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

    if crop == "rice":
        planted = pd.Timestamp(f"{year}/{_PLANTING[crop]}")
        m = m[m["日期"] >= planted].copy()
        m["每日灌溉量(mm)"] = ck.daily.loc[ck.daily["模拟日期"] >= planted, "每日灌溉量(mm)"].to_numpy()
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

    # 物候口径：模拟期固定从 04-01 起，但水稻 05-15 才移栽、玉米 04-25 才播种，
    # 出苗还要再等 EmergenceCD 天。这段"有模拟、无作物"的区间里冠层覆盖度为 0、
    # 也不灌溉，ETc 只剩裸土蒸发，Kc=ETc/ET0 会掉到 0.1 以下且逐日大幅摆动 ——
    # 它是**裸土蒸发系数**，不是作物系数。必须把这段单独算出来并在页面上讲明，
    # 否则「全期 Kc」会被误读成作物系数（2026-10-20 核查结论）。
    plant_md = _PLANTING.get(crop)
    phenology = None
    if plant_md:
        try:
            _md = str(plant_md).replace("-", "/").split("/")
            p_dt = pd.Timestamp(year=int(year), month=int(_md[0]), day=int(_md[1]))
            emerg_days = int(build_crop(crop).EmergenceCD)
            # AquaCrop 把移栽/播种当日记为第 1 天，故出苗日 = 移栽日 + (EmergenceCD-1)
            e_dt = p_dt + pd.Timedelta(days=max(emerg_days - 1, 0))
            pre = m[m["日期"] < p_dt]
            post = m[m["日期"] >= p_dt]
            phenology = {
                "planting": p_dt.strftime("%m-%d"),
                "emergence": e_dt.strftime("%m-%d"),
                "plant_term": "移栽" if crop == "rice" else "播种",
                "emergence_term": "返青恢复" if crop == "rice" else "出苗",
                "pre_days": int(len(pre)),
                "pre_et0": round(float(pre["ReferenceET"].sum()), 1),
                "pre_etc": round(float(pre["蒸腾蒸发量(mm)"].sum()), 1),
                "pre_irrigation": round(float(pre["每日灌溉量(mm)"].sum()),1),
                "pre_kc": _kc(pre["蒸腾蒸发量(mm)"].sum(), pre["ReferenceET"].sum())
                if len(pre) else None,
                "crop_days": int(len(post)),
                "crop_et0": round(float(post["ReferenceET"].sum()), 1),
                "crop_etc": round(float(post["蒸腾蒸发量(mm)"].sum()), 1),
                "crop_kc": _kc(post["蒸腾蒸发量(mm)"].sum(), post["ReferenceET"].sum())
                if len(post) else None,
            }
        except Exception as e:                           # noqa: BLE001
            log(f"  ⚠ 物候口径统计失败: {type(e).__name__}: {e}")

    # 逐日明细：按月份分组。页面默认只显示月表，点击月份行才展开该月逐日，
    # 这样既拿到日步长数据，又不会把页面拉长。
    daily_by_month: dict[int, list] = {}
    for _, dr in m.sort_values("日期").iterrows():
        et0_d = float(dr["ReferenceET"])
        etc_d = float(dr["蒸腾蒸发量(mm)"])
        daily_by_month.setdefault(int(dr["月"]), []).append([
            dr["日期"].strftime("%m-%d"),
            round(et0_d, 2), round(etc_d, 2), _kc(etc_d, et0_d),
            round(float(dr["Precipitation"]), 2),
            round(float(dr["每日灌溉量(mm)"]), 2),
        ])

    out = {
        "crop": crop,
        "name": rs.CROP_CN[crop],
        "period": f"{m['日期'].iloc[0]:%m-%d} ~ {m['日期'].iloc[-1]:%m-%d}",
        "phenology": phenology,
        "scheme": ck.label,
        "monthly": {
            "columns": ["时段", "参考需水量ET0(mm)", "作物需水量ETc(mm)",
                        "作物系数Kc", "降雨量(mm)", "净灌水量(mm)"],
            "rows": rows,
        },
        "daily": {
            "columns": ["日期", "参考需水量ET0(mm)", "作物需水量ETc(mm)",
                        "作物系数Kc", "降雨量(mm)", "净灌水量(mm)"],
            "by_month": {str(k): v for k, v in sorted(daily_by_month.items())},
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
    ax.bar(x, irr, width=1.0, bottom=prcp, color="#f4a259", label="净灌水量")
    ax.plot(x, day_df["ReferenceET"], "--", color="#8d99ae", lw=1.3,
            label="参考需水量 ET0")
    ax.plot(x, day_df["蒸腾蒸发量(mm)"], "-", color="#d62828", lw=1.5,
            label="作物需水量 ETc")
    ax.set_title(f"{crop_cn}需水过程（{year} 年）", fontsize=11)
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
#  生长过程曲线（冠层覆盖度 / 干物质量）
# --------------------------------------------------------------------------- #
#  (daily 列名, 图题, 纵轴名, 文件名模板)
GROWTH_CURVE_SPECS = (
    ("冠层覆盖度(%)", "冠层覆盖度过程曲线", "冠层覆盖度 CC (%)",
     "canopy_cover_{crop}_{year}.png"),
    ("干物质量(t/ha)", "干物质量累积曲线", "干物质量 (t/ha)",
     "dry_matter_{crop}_{year}.png"),
)


def _plot_growth_curve(results, crop: str, crop_cn: str, year: int,
                       column: str, title: str, ylabel: str,
                       path: Path, log=print) -> bool:
    """逐日生长过程曲线：正常灌溉对照高亮，各灌溉档位叠加对比。

    口径提示：数据自模拟期 04-01 起，而作物 05-15（水稻移栽）/ 04-25（玉米播种）
    才进入田间，之前的 CC / 干物质恒为 0，属模型空转段。图上以竖虚线标出，
    引用指标时须剔除该段（见页面「模拟期、移栽日期与 Kc 口径」说明）。
    """
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd

    rows = [r for r in results
            if getattr(r, "crop", None) == crop
            and r.daily is not None and column in getattr(r.daily, "columns", [])]
    if not rows:
        log(f"  ⚠ {crop_cn}{title} 跳过：结果里没有「{column}」列")
        return False

    rs, wc = _ensure()
    ck = next((r for r in rows if (crop == "corn" and "墒情优化" in r.summary) or
               (r.crop == "rice" and r.label == "常规淹水")), None)
    others = [r for r in rows if r is not ck]

    fig, ax = plt.subplots(figsize=(10.5, 4.6), dpi=150)
    colors = plt.cm.viridis(np.linspace(0.15, 0.85, max(len(others), 1)))
    for i, r in enumerate(others):
        d = r.daily
        ax.plot(d["模拟日期"], d[column], "-", color=colors[i], lw=1.2,
                alpha=0.7, label=r.label)
    if ck is not None:
        ax.plot(ck.daily["模拟日期"], ck.daily[column], "-", color="#d62828",
                lw=2.2, label=ck.label, zorder=5)

    # 作物进入田间日期（水稻移栽 / 玉米播种），之前为空转段
    from config.simulation_settings import planting_date
    md = planting_date(crop)
    stage_cn = "移栽" if crop == "rice" else "播种"
    if md:
        try:
            xd = pd.Timestamp(f"{year}-{md.replace('/', '-')}")
            ax.axvline(xd, color="#444", ls="--", lw=1.1, alpha=0.75)
            ax.annotate(f"{stage_cn} {md}", xy=(xd, 0.55),
                        xycoords=("data", "axes fraction"),
                        xytext=(3, 0), textcoords="offset points",
                        rotation=90, va="center", ha="left",
                        fontsize=8, color="#444")
        except Exception:                                # noqa: BLE001
            pass

    ax.set_ylabel(ylabel)
    xlabel = "日期"
    if md:
        xlabel += f"（{stage_cn}前为空转段，曲线无作物含义）"
    ax.set_xlabel(xlabel)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.grid(axis="y", ls=":", alpha=0.5)
    ax.set_title(f"{crop_cn}{title}（{year} 年 · 灌溉方案对比）",
                 fontsize=11, pad=22)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.005, 1, 0.1), mode="expand",
              ncols=8, fontsize=7.5, frameon=False, handlelength=1.6,
              columnspacing=1.0)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return path.exists()


# --------------------------------------------------------------------------- #
#  运行
# --------------------------------------------------------------------------- #
def validate_run_settings(crops, irrigation_options=None, rice_management=None):
    _ensure()
    from config.irrigation_params import utilization_settings
    options=irrigation_options or {}
    defaults={"rice":"rice_surface","corn":"corn_canal"}
    if "rice" in crops:
        from modules.paddy_control import PaddyLayerController, bands_for_ck
        from config.crop_params import RICE_PARAMS
        PaddyLayerController(bands_for_ck(), flowering_start_dap=int(RICE_PARAMS["HIstartCD"])+1,
                             flowering_days=int(RICE_PARAMS["FloweringCD"]),
                             maturity_dap=int(RICE_PARAMS["MaturityCD"]), **(rice_management or {}))
    settings={}
    for crop in crops:
        item=options.get(crop,{})
        settings[crop]=utilization_settings(crop,item.get("profile",defaults[crop]),item.get("coefficient"),item.get("application_efficiency_pct",90.))
    return settings


def validate_agronomy_settings(crops, year, options=None):
    _ensure()
    from config.simulation_settings import validate_settings
    return validate_settings(crops, year, options)


def agronomy_capabilities():
    _ensure()
    from config.simulation_settings import capabilities
    from config.crop_params import RICE_PARAMS
    result = capabilities()
    result["rice_stomatal_depletion_start_fraction"] = float(RICE_PARAMS["p_up2"])
    return result


def run(crops: list[str], year: int, out_dir: Path, progress=None,
        owner: str = "", irrigation_options=None, agronomy_options=None, rice_management=None) -> dict:
    agronomy = validate_agronomy_settings(crops, year, agronomy_options)
    from config.simulation_settings import use_settings
    with use_settings(agronomy):
        return _run_configured(crops, year, out_dir, progress, owner, irrigation_options, agronomy,  rice_management)


def _run_configured(crops: list[str], year: int, out_dir: Path, progress=None,
                    owner: str = "", irrigation_options=None, agronomy=None, rice_management=None) -> dict:
    """运行当前档位优化并出图，把所有交付物落到 out_dir。

    `owner` 决定内核读哪套气象数据：运行前把 `wc.DATA_DIR` 切到该用户私有目录，
    这样 A 上传的气象 CSV 不会影响 B 的模拟结果。
    """
    rs, wc = _ensure()
    from config.wutonghe_config import ensure_dirs
    from modules.irrigation_diagnostics import irrigation_evidence,attach_source_demand
    import json
    if "rice" in crops:
        rice_management=dict(ci_natural_drydown=True,ci_split_refill=True,
                             ci_ponding_limit_days=None,ci_ponding_limit_depth_mm=None,
                             ci_duration_drain_target_mm=None,awd_natural_drydown=True,
                             awd_ponding_limit_days=None,awd_ponding_limit_depth_mm=None,
                             awd_duration_drain_target_mm=None,pre_drying_days=3,
                             drying_handoff=True,drying_min_days=5,recovery_budget_days=2) | (rice_management or {})
    settings=validate_run_settings(crops,irrigation_options,rice_management)
    from config.soil_params import get_soil_config
    rice_soil=get_soil_config("rice") if "rice" in crops else None
    from config.crop_params import RICE_PARAMS
    rice_stomatal_start = float(RICE_PARAMS["p_up2"]) if "rice" in crops else None


    if owner:
        activate_weather(owner)          # ★ 必须在 ensure_dirs 之前
    ensure_dirs()
    out_dir = Path(out_dir)
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    log = print
    log("#" * 62)

    for crop, item in agronomy.items():
        log(f"# {rs.CROP_CN[crop]}：田间日期 {item['planting_date_iso']}；收获 {item['harvest_date']}；土壤 {item['soil']['name']}")
    log(f"#  {year} 年 | {' + '.join(rs.CROP_CN[c] for c in crops)} | "
        f"共 {sum(n_schemes(c) for c in crops)} 方案")
    log("#" * 62)

    year_results = []
    figures: list[dict] = []
    for crop in crops:
        if progress:
            progress(f"{rs.CROP_CN[crop]} 模拟中")
        results = rs.run_crop_optimization(crop, year, settings[crop]["application_efficiency_pct"], rice_management=rice_management)
        if len(results)!=n_schemes(crop):
            raise RuntimeError(f"{rs.CROP_CN[crop]}有方案失败，不输出不完整的候选结论")
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

    # ---- 逐日结果与 内核原始 Excel ----
    daily_files, excel_files = [], []
    for r in year_results:
        # 文件名用稳定档位 ID(L1–L5 / 对照 CK), 与中文显示名解耦
        tag = f"{r.crop}_{rs.scheme_id(r.label)}_{year}"
        try:
            r.save_csv(str(out_dir / f"daily_{tag}.csv"))
            daily_files.append(f"daily_{tag}.csv")
        except Exception as e:                           # noqa: BLE001
            log(f"  ⚠ save_csv 失败 ({tag}): {e}")
        try:
            r.save_original_excel(str(out_dir / f"original_{tag}.xlsx"))
            excel_files.append(f"original_{tag}.xlsx")
        except Exception as e:                           # noqa: BLE001
            log(f"  ⚠ save_original_excel 失败 ({tag}): {e}")

    # ---- 汇总表 / 最优方案 ----
    summary = attach_source_demand(rs.build_optimization_table(year_results, year),settings)
    summary.to_csv(out_dir / f"irrigation_optimization_{year}.csv",
                   index=False, encoding="utf-8-sig")
    log(f"\n汇总表已保存 (irrigation_optimization_{year}.csv)")
    log(summary.to_string(index=False))

    optimal = rs.find_optimal(summary)
    evidence=irrigation_evidence(year_results,summary)
    evidence_name=f"irrigation_evidence_{year}.csv"
    evidence.to_csv(out_dir/evidence_name,index=False,encoding="utf-8-sig")
    settings_name=f"run_settings_{year}.json"
    (out_dir/settings_name).write_text(json.dumps(dict(model_revision="2026-10-26-crop-simplified-v7",optimization_rule="max_yield_then_min_irrigation",irrigation=settings,agronomy=agronomy,rice_management=rice_management,rice_soil=rice_soil,rice_parameter_version="wutonghe-rice-v6-20261023",rice_stomatal_depletion_start_fraction=rice_stomatal_start),ensure_ascii=False,indent=2),encoding="utf-8")
    optimal_files = []
    if not optimal.empty:
        optimal.to_csv(out_dir / f"optimal_irrigation_{year}.csv",
                       index=False, encoding="utf-8-sig")
        optimal_files.append(f"optimal_irrigation_{year}.csv")
        log(f"\n★ {year} 年灌溉筛选结果（最高产，同产量取灌水最少）：")
        log(optimal.to_string(index=False))

    # ---- 生长过程曲线（冠层覆盖度 / 干物质量，正常灌溉对照 + 各档叠加）----
    for crop in crops:
        for column, title, ylabel, tmpl in GROWTH_CURVE_SPECS:
            fname = tmpl.format(crop=crop, year=year)
            try:
                if _plot_growth_curve(year_results, crop, rs.CROP_CN[crop], year,
                                      column, title, ylabel, fig_dir / fname,
                                      log=log):
                    figures.append({
                        "key": f"{column}:{crop}",
                        "title": f"{title}（{rs.CROP_CN[crop]}）",
                        "group": rs.CROP_CN[crop],
                        "rel": f"figures/{fname}",
                        "size": (fig_dir / fname).stat().st_size,
                    })
                    log(f"  已保存: {fname}")
            except Exception as e:                       # noqa: BLE001
                log(f"  ⚠ 生长曲线生成失败 {fname}: {type(e).__name__}: {e}")

    # ---- 作物需水量（逐月 ET0 / ETc / Kc / 降雨 / 灌溉，正常灌溉对照）----
    import pandas as pd

    water_req = None
    wr_crops = []
    wr_daily_rows: list = []
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
        # 逐日明细长表：作物 / 月份 / 日期 / ET0 / ETc / Kc / 降雨 / 灌溉
        for mo, day_rows in wr["daily"]["by_month"].items():
            for dr in day_rows:
                wr_daily_rows.append([wr["name"], f"{mo}月"] + list(dr))

    if wr_crops:
        wr_csv = f"water_requirement_{year}.csv"
        pd.concat([f for _, f in wr_crops], ignore_index=True) \
          .to_csv(out_dir / wr_csv, index=False, encoding="utf-8-sig")
        wr_daily_csv = None
        if wr_daily_rows:
            wr_daily_csv = f"water_requirement_daily_{year}.csv"
            pd.DataFrame(wr_daily_rows,
                         columns=["作物", "月份"]
                         + wr_crops[0][0]["daily"]["columns"]) \
              .to_csv(out_dir / wr_daily_csv, index=False, encoding="utf-8-sig")
        water_req = {
            "year": year,
            "basis": "水稻常规淹水CF；玉米模型最优方案",
            "crops": [wr for wr, _ in wr_crops],
            "csv": wr_csv,
            "daily_csv": wr_daily_csv,
        }
        log(f"\n需水量统计已保存 ({wr_csv}"
            + (f" ｜ 逐日明细 {wr_daily_csv}" if wr_daily_csv else "") + ")")

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

    corn_optimization = next((r.summary["墒情优化"] for r in year_results if "墒情优化" in r.summary), None)
    search_files = []
    if corn_optimization:
        search_name = f"corn_smt_search_{year}.csv"
        report_name = f"corn_smt_optimization_{year}.json"
        pd.DataFrame(corn_optimization["candidates"]).to_csv(out_dir/search_name, index=False, encoding="utf-8-sig")
        (out_dir/report_name).write_text(json.dumps(corn_optimization, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        search_files = [search_name, report_name]

    management_files=[]
    for result in year_results:
        if result.crop == "rice" and result.management_daily is not None:
            name=f"management_rice_{rs.scheme_id(result.label)}_{year}.csv"
            result.management_daily.to_csv(out_dir/name,index=False,encoding="utf-8-sig")
            management_files.append(name)
    prep_files=[]
    for result in year_results:
        if result.crop=="rice" and result.puddling_daily is not None:
            prep_name=f"puddling_balance_{year}.csv"
            result.puddling_daily.to_csv(out_dir/prep_name,index=False,encoding="utf-8-sig")
            prep_files.append(prep_name)
            break
    deliv = [f"irrigation_optimization_{year}.csv",evidence_name,settings_name] + optimal_files + prep_files + search_files + management_files \
        + ([water_req["csv"]] if water_req else []) \
        + ([water_req["daily_csv"]] if water_req and water_req.get("daily_csv") else []) \
        + daily_files + excel_files

    # 关键指标速览（页面顶部卡片）
    kpi = []
    if not optimal.empty:
        for _, row in optimal.iterrows():
            chosen=summary[summary["作物"].eq(row["作物"])&summary["灌溉方案"].eq(row["最优方案"])].iloc[0]
            peak=summary[summary["作物"].eq(row["作物"])].sort_values(["最终籽粒产量(t/ha)","累计灌溉量(mm)"],ascending=[False,True]).iloc[0]
            kpi.append({
                "最高产方案":peak["灌溉方案"],
                "较峰值减产(%)":None if pd.isna(row.get("较峰值减产(%)")) else float(row["较峰值减产(%)"]),
                "产量达最大百分比(%)":None if pd.isna(row.get("产量达最大百分比(%)")) else float(row["产量达最大百分比(%)"]),
                "作物": row.get("作物"),
                "最优方案": row.get("最优方案"),
                "灌溉参数": row.get("灌溉参数"),
                "累计灌溉量(mm)": row.get("累计灌溉量(mm)"),
                "产量(t/ha)": row.get("产量(t/ha)"),
                "最大产量(t/ha)": row.get("最大产量(t/ha)"),
                "灌溉WUE(kg/m³)": None if pd.isna(row.get("灌溉WUE(kg/m³)")) else float(row["灌溉WUE(kg/m³)"]),
                "水源取水量估算(mm)":None if settings[{"水稻":"rice","玉米":"corn"}[row["作物"]]]["coefficient"] is None else float(chosen["水源取水量估算(mm)"]),
            })

    return {
        "type": "spac",
        "model_revision": "2026-10-26-crop-simplified-v7",
        "optimization_rule":"max_yield_then_min_irrigation",
        "rice_optimization_status":"four_regime_max_yield",
        "rice_parameter_version":"wutonghe-rice-v6-20261023",
        "rice_stomatal_depletion_start_fraction":rice_stomatal_start,
        "rice_soil":rice_soil,
        "rice_management":rice_management or {},
        "corn_optimization":corn_optimization,
        "irrigation_settings":settings,
        "agronomy_settings":agronomy,
        "evidence":df_records(evidence),
        "irrigation_basis": "net_puddling_balance_v4",
        "yield_basis": {"rice": "14.5%含水率稻谷（来源表未注明，暂定）", "corn": "干籽粒"},
        "model_notes": "输入气象、种植日期与土壤参数，输出各方案产量、用水及最优制度。",
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
