# -*- coding: utf-8 -*-
"""
梧桐河灌区基于SPAC的典型作物生长模型 —— 全局配置
====================================================
梧桐河灌区位于黑龙江省三江平原西部，依梧桐河（松花江支流）而建，
地跨萝北县、汤原县，是黑龙江省重要水稻灌区之一，同时种植玉米、小麦。

气候属寒温带大陆性季风气候：冬季寒冷漫长，夏季温暖短促。
作物生长季约 5 月—9 月，一年一熟。水稻为水田主体作物，玉米为主要旱田作物。

本文件定义模型运行所需的全局常量（区域、坐标、生育期、路径等）。
"""

from __future__ import annotations
import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
#  路径设置
# --------------------------------------------------------------------------- #
# 模型根目录（本 config 文件所在目录的上一级）
# PyInstaller 打包为 EXE 时(__file__ 指向临时解压目录),
# 改用 EXE 所在目录 —— data/outputs 与 EXE 同级放置
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"          # 输入数据目录（气象、土壤、作物实测等）
OUTPUT_DIR = BASE_DIR / "outputs"      # 输出结果目录（CSV、图件）
FIG_DIR = OUTPUT_DIR / "figures"

for _d in (DATA_DIR, OUTPUT_DIR, FIG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
#  灌区地理与气象参数（用于 FAO-56 Penman-Monteith 参考蒸散发计算）
# --------------------------------------------------------------------------- #
# 梧桐河灌区实际范围（经纬度，WGS84）
# 130.55-130.90°E, 47.20-47.40°N —— 松花江下游平原,梧桐河灌区实际位置
# 注: 此前误用 131.0-131.4°E (小兴安岭东南缘), 地形抬升致降水偏高约2倍
REGION_NAME = "梧桐河灌区"
PROVINCE = "黑龙江省"
BBOX_LON = (130.55, 130.90)      # 经度范围
BBOX_LAT = (47.20, 47.40)        # 纬度范围
CENTER_LON = 130.725
CENTER_LAT = 47.30

# 海拔 (m)，三江平原低平，约 70 m
ALTITUDE_M = 70.0

# 纬度（弧度），用于太阳辐射计算
import math
LAT_RAD = CENTER_LAT * math.pi / 180.0

# --------------------------------------------------------------------------- #
#  典型作物生育期（播种/移栽 — 收获）
#  依据东北地区（黑龙江省）作物物候，结合大纲表1 梧桐河灌区实际调整
# --------------------------------------------------------------------------- #
# 水稻：4 月中旬育秧，5 月中旬插秧，9 月下旬收获
RICE_PLANTING_DATE = "05/15"     # 插秧（移栽）日期 mm/dd
RICE_HARVEST_DATE = "09/25"      # 收获日期 mm/dd
RICE_SEASON = ("05/15", "09/25")

# 玉米：4 月下旬播种，9 月下旬收获
CORN_PLANTING_DATE = "04/25"
CORN_HARVEST_DATE = "09/25"
CORN_SEASON = ("04/25", "09/25")

# 模拟年份（典型水文年）
SIM_YEAR = 2024
# 模拟起止（含冬前土壤水分初始化可前移，此处以生育期为界）
SIM_START = f"{SIM_YEAR}/04/01"   # 提前以稳定初始土壤水分
SIM_END = f"{SIM_YEAR}/10/15"

# 模拟期固定月-日（覆盖生育期 + 前期土壤水分初始化）
SIM_START_MD = "04/01"
SIM_END_MD = "10/15"


def get_sim_bounds(year: int):
    """按年份返回 (sim_start, sim_end) 字符串，用于多年份运行。

    Parameters
    ----------
    year : 模拟年份（如 2024）

    Returns
    -------
    (sim_start, sim_end) : 形如 ('2024/04/01', '2024/10/15')
    """
    return f"{year}/{SIM_START_MD}", f"{year}/{SIM_END_MD}"


def get_weather_csv_path(year: int):
    """按年份返回对应的气象 CSV 路径。

    文件命名规则（两种均可, 优先级从上到下）：
      1. data/weather_era5_{year}.csv   (download_weather_era5.py 下载生成)
      2. data/weather_{year}.csv        (简化命名, 手工准备的非 ERA5 数据)
    若两种都不存在，回退到默认 data/weather_era5.csv
    """
    for name in (f"weather_era5_{year}.csv", f"weather_{year}.csv"):
        csv_path = DATA_DIR / name
        if csv_path.exists():
            return str(csv_path)
    # 回退到默认（可能是任意年份的副本）
    default_csv = DATA_DIR / "weather_era5.csv"
    if default_csv.exists():
        print(f"[配置] 未找到 {DATA_DIR / f'weather_{year}.csv'}，回退到默认气象文件: {default_csv.name}")
        return str(default_csv)
    return str(DATA_DIR / f"weather_{year}.csv")  # 返回不存在的路径，让上层报错


def ensure_dirs() -> None:
    """确保所有输出目录存在。"""
    for _d in (DATA_DIR, OUTPUT_DIR, FIG_DIR):
        _d.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    print(f"区域：{PROVINCE}{REGION_NAME}")
    print(f"中心经纬度：({CENTER_LON}, {CENTER_LAT})  海拔：{ALTITUDE_M} m")
    print(f"水稻：{RICE_SEASON}  玉米：{CORN_SEASON}")
    print(f"模拟时段：{SIM_START} ~ {SIM_END}")
    print(f"输出目录：{OUTPUT_DIR}")
