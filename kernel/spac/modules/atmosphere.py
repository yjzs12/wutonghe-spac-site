# -*- coding: utf-8 -*-
"""
大气交互模块
============
对应大纲 2.2 大气交互模块：
  - 输入：逐日降雨量、最高气温、最低气温、风速、相对湿度、太阳辐射
  - 输出：逐日降雨量(Prcp)、参考蒸腾蒸发量(Et0)
  - 核心：FAO-56 Penman-Monteith 公式计算参考蒸散量 ET0

数据来源：实测气象 CSV（可由 download_weather_era5.py 下载生成），
由 build_weather_df() 统一读取并计算 ET0。

CSV 列要求（中文或英文列名均可识别）：
  Date(日期, yyyy-mm-dd 或 yyyy/mm/dd), Prcp(降雨量 mm),
  Tmax(最高气温 ℃), Tmin(最低气温 ℃),
  Wind(2m 风速 m/s), RH(相对湿度 %), Rs(太阳辐射 MJ/m2/day)
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
#  FAO-56 Penman-Monteith 参考蒸散量 ET0
#    ET0 = 0.408 * Δ * (Rn - G) + γ * (900/(T+273)) * u2 * (es - ea)
#          / (Δ + γ * (1 + 0.34 * u2))
#  单位：ET0 mm/day, Rn/G MJ/m2/day, T ℃, u2 m/s, 压力 kPa
# --------------------------------------------------------------------------- #
def penman_montheith_et0(
    tmin: np.ndarray, tmax: np.ndarray,
    wind: np.ndarray, rh: np.ndarray, rs: np.ndarray,
    doy: np.ndarray, lat_rad: float, altitude: float,
) -> np.ndarray:
    """FAO-56 Penman-Monteith 日尺度参考蒸散量。

    Parameters
    ----------
    tmin, tmax : 逐日最低/最高气温 (℃)
    wind       : 2m 高度风速 (m/s)
    rh         : 相对湿度 (%)
    rs         : 太阳辐射 (MJ/m2/day)
    doy        : 年内日序 (1..365)
    lat_rad    : 纬度 (弧度)
    altitude   : 海拔 (m)
    Returns
    -------
    et0 : 逐日参考蒸散量 (mm/day)
    """
    tmean = 0.5 * (tmin + tmax)

    # —— 饱和水汽压 es = (0.6108*exp(17.27*T/(T+237.3))) ——
    es = 0.6108 * (
        np.exp(17.27 * tmax / (tmax + 237.3)) +
        np.exp(17.27 * tmin / (tmin + 237.3))
    ) * 0.5
    # 实际水汽压 ea = es(Tmin) * RH/100 （FAO 推荐 Tmin 下的饱和压近似露点）
    ea = 0.6108 * np.exp(17.27 * tmin / (tmin + 237.3)) * (rh / 100.0)
    vpd = np.clip(es - ea, 0.0, None)

    # —— 饱和水汽压曲线斜率 Δ (kPa/℃) ——
    delta = 4098.0 * (0.6108 * np.exp(17.27 * tmean / (tmean + 237.3))) / (tmean + 237.3) ** 2

    # —— 湿度计常数 γ (kPa/℃)，随气压变 ——
    # 气压 P = 101.3 * ((293 - 0.0065*z)/293)^5.26
    p = 101.3 * ((293.0 - 0.0065 * altitude) / 293.0) ** 5.26
    gamma = 0.665e-3 * p

    # —— 地外辐射 Ra (MJ/m2/day) 与日照修正 ——
    dr = 1.0 + 0.033 * np.cos(2.0 * np.pi * doy / 365.0)
    decl = 0.409 * np.sin(2.0 * np.pi * doy / 365.0 - 1.39)
    ws = np.arccos(-np.tan(lat_rad) * np.tan(decl))
    # 日序对应的日落时角；高纬冬季极夜处理
    ws = np.where(np.isfinite(ws), ws, 0.0)
    ra = (24.0 * 60.0 / np.pi) * 0.0820 * dr * (
        ws * np.sin(lat_rad) * np.sin(decl) +
        np.cos(lat_rad) * np.cos(decl) * np.sin(ws)
    )
    ra = np.clip(ra, 0.0, None)

    # —— 净辐射 Rn (MJ/m2/day) ——
    # 净短波 Rns = (1 - α) * Rs，α=0.23（参考作物）
    albedo = 0.23
    rns = (1.0 - albedo) * rs
    # 净长波 Rnl（FAO-56 简化）
    # Rso = 0.75 * Ra（晴空辐射）
    rso = 0.75 * ra
    # 阴云因子 f = 1.35*Rs/Rso - 0.35，限制在 0.3~1.0
    f = np.clip(1.35 * rs / np.where(rso > 0, rso, 1e-6) - 0.35, 0.3, 1.0)
    sigma = 4.903e-9  # Stefan-Boltzmann (MJ/K4/m2/day)
    tk4 = (273.15 + tmax) ** 4 + (273.15 + tmin) ** 4
    rnl = sigma * 0.5 * tk4 * (0.34 - 0.14 * np.sqrt(ea)) * f
    rn = np.clip(rns - rnl, 0.0, None)

    # —— 土壤热通量 G，日尺度近似 0 ——
    g = np.zeros_like(tmean)

    u2 = np.clip(wind, 0.0, None)

    et0 = (
        0.408 * delta * (rn - g) +
        gamma * (900.0 / (tmean + 273.0)) * u2 * vpd
    ) / (delta + gamma * (1.0 + 0.34 * u2))
    return np.clip(et0, 0.0, None)


# --------------------------------------------------------------------------- #
#  气象数据读取 / 生成
# --------------------------------------------------------------------------- #
# 列名别名（中文 -> 标准英文）
_COL_ALIASES = {
    "日期": "Date", "date": "Date", "时间": "Date", "day": "Date",
    "降雨量": "Prcp", "降水": "Prcp", "降水mm": "Prcp", "precip": "Prcp", "rain": "Prcp", "prcp": "Prcp",
    "最高气温": "Tmax", "最高温度": "Tmax", "tmax": "Tmax",
    "最低气温": "Tmin", "最低温度": "Tmin", "tmin": "Tmin",
    "风速": "Wind", "wind": "Wind", "u2": "Wind",
    "相对湿度": "RH", "湿度": "RH", "rh": "RH", "humidity": "RH",
    "太阳辐射": "Rs", "辐射": "Rs", "rs": "Rs", "solar": "Rs",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rename = {c: _COL_ALIASES.get(str(c).strip().lower(), _COL_ALIASES.get(str(c).strip(), c))
              for c in df.columns}
    df = df.rename(columns=rename)
    return df


def build_weather_df(csv_path: str, lat_rad: float, altitude: float) -> pd.DataFrame:
    """读取实测气象 CSV 并计算 ET0，返回 内核所需格式。

    CSV 不存在时显式报错（不生成合成数据）。
    返回 DataFrame 列：MinTemp, MaxTemp, Precipitation, ReferenceET, Date
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"实测气象 CSV 不存在: {csv_path}\n"
            f"请先运行 download_weather_era5.py 生成, 或检查路径。"
        )
    df = pd.read_csv(csv_path)
    df = _normalize_columns(df)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)

    for col in ("Tmin", "Tmax", "Prcp", "Wind", "RH", "Rs"):
        if col not in df.columns:
            raise KeyError(f"气象 CSV 缺少列: {col}（现有列: {list(df.columns)}）")

    doy = df["Date"].dt.dayofyear.to_numpy()
    et0 = penman_montheith_et0(
        df["Tmin"].to_numpy(), df["Tmax"].to_numpy(),
        df["Wind"].to_numpy(), df["RH"].to_numpy(), df["Rs"].to_numpy(),
        doy, lat_rad, altitude,
    )
    df["Et0"] = et0
    print(f"[大气交互模块] 已读取实测气象数据: {csv_path}  共 {len(df)} 天")
    return df.rename(columns={
        "Tmin": "MinTemp", "Tmax": "MaxTemp",
        "Prcp": "Precipitation", "Et0": "ReferenceET",
    })[["MinTemp", "MaxTemp", "Precipitation", "ReferenceET", "Date"]]


if __name__ == "__main__":
    # 自检：读取默认年份实测气象 CSV 并打印月统计
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config.wutonghe_config import (
        LAT_RAD, ALTITUDE_M, SIM_YEAR, get_weather_csv_path,
    )

    df = build_weather_df(get_weather_csv_path(SIM_YEAR), LAT_RAD, ALTITUDE_M)
    print(df.head())
    print(f"\n年降雨量: {df['Precipitation'].sum():.1f} mm  年ET0: {df['ReferenceET'].sum():.1f} mm")
    df["month"] = df["Date"].dt.month
    print(df.groupby("month")[["Precipitation", "ReferenceET", "MaxTemp", "MinTemp"]].mean().round(1))
