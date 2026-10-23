# -*- coding: utf-8 -*-
"""Water-rice candidate V2 and unchanged dryland configuration.

Thin plough-pan geometry: 0.20--0.28 m; source is regional evidence,
not a measured Wutonghe profile. SAT/FC/PWP retain uncalibrated grid priors.
Nominal pan Ksat=2.8 mm/day is NOT a measured or calibrated daily leakage.
Below-pan Ksat=8 mm/day is a separate unmeasured structural scenario.
Keep PADDY_SOIL_LEGACY for controlled comparisons; validate against field data
before interpreting any candidate as an operational irrigation prescription.
"""
from __future__ import annotations
from copy import deepcopy

# --------------------------------------------------------------------------- #
#  内核内置土壤预设（soil_type 字符串）
#  使用 custom 以便按层显式给定 SAT/FC/PWP/Ks
# --------------------------------------------------------------------------- #
SOIL_PRESET_PADDY = "custom"       # 水田：自定义（按层给实测值）
SOIL_PRESET_DRYLAND = "custom"    # 旱田：自定义（按层给实测值）

# --------------------------------------------------------------------------- #
#  水田（水稻）剖面 —— 格网估计与文献约束参数
#  采样点: 130.788°E, 47.212°N
#  质地: 砂质粘壤土(耕作层) → 壤土(犁底层/心土层)
# --------------------------------------------------------------------------- #
PADDY_SOIL_LEGACY = {
    "name": "梧桐河水稻土 [格网估计/等效水力参数待验证]",
    "preset": SOIL_PRESET_PADDY,
    "dz": [0.30, 0.30, 0.40],
    "layers": [
        # SAT/FC/PWP为格网估计；分层和穿透率为简化假设，不是实测犁底层。
        {"depth": 0.30, "SAT": 0.579, "FC": 0.367, "PWP": 0.153, "Ks": 8.0, "penetrability": 100},
        # Ksat=2.8mm/d保留为等效先验，敏感性范围2.0—3.4；无本地实际灌水记录，未重新标定。
        # 该量既控制排水通量上限，也通过tau=0.0866*Ksat**0.35影响非饱和排水。
        {"depth": 0.30, "SAT": 0.509, "FC": 0.320, "PWP": 0.159, "Ks": 2.8, "penetrability": 40},
        {"depth": 0.40, "SAT": 0.506, "FC": 0.310, "PWP": 0.159, "Ks": 50.0, "penetrability": 70},
    ],
}

# V2 explicit thin-pan candidate. Hydraulic values remain priors, not measurements.
# Regional geometry reference: DOI 10.13287/j.1001-9332.201812.005.
# Keep 2.8 as an uncalibrated nominal Ksat; compare 0.5/1.0/2.8, never tune to a desired saving.
PADDY_SOIL = {
    "name": "梧桐河水稻薄犁底层候选V2 [未率定]",
    "version": "rice-soil-v2-20261023",
    "preset": "custom",
    "dz": [.20, .08, .32, .40],
    "compartments": [.05]*4 + [.04]*2 + [.02, .10, .10, .10] + [.10]*4,
    "pan_bottom_m": .28,
    "puddling_sat_depth_m": .20,
    "layers": [
        {"depth": .20, "SAT": .579, "FC": .367, "PWP": .153, "Ks": 8., "penetrability": 100},
        {"depth": .08, "SAT": .509, "FC": .320, "PWP": .159, "Ks": 2.8, "penetrability": 40},
        {"depth": .32, "SAT": .509, "FC": .320, "PWP": .159, "Ks": 8., "penetrability": 70},
        {"depth": .40, "SAT": .506, "FC": .310, "PWP": .159, "Ks": 50., "penetrability": 70},
    ],
}
# Below-pan Ks=8 and penetrability=70 are explicit engineering priors; not SoilGrids observations.
PADDY_SOIL_LEGACY.update(version="rice-soil-v1-legacy", pan_bottom_m=.60,
                         puddling_sat_depth_m=.60)

# 玉米剖面沿用本次修改前配置，未重新率定。
DRYLAND_SOIL = {
    "name": "梧桐河旱田(白浆土) [格网估计/等效水力参数待验证]",
    "preset": SOIL_PRESET_DRYLAND,
    "dz": [0.30, 0.30, 0.40],
    "layers": [
        # 层1 耕作层 (0-30cm): 砂30.3% 黏27.3% 粉42.3%  容重1143 kg/m³  OC 18.3 g/kg
        # Cosby估算 Ks=97 mm/day（疏松耕作层,合理）
        # penetrability: 内核要求百分数(0-100), 100=完全可穿透
        {"depth": 0.30, "SAT": 0.568, "FC": 0.340, "PWP": 0.174, "Ks": 97.0,  "penetrability": 100},
        # 层2 白浆层 (30-60cm): 砂31% 黏27% 粉42%  容重1290 kg/m³  OC 5 g/kg
        # 白浆土紧实低透水,Cosby估算=99 偏高,人工调低至 15
        {"depth": 0.30, "SAT": 0.513, "FC": 0.300, "PWP": 0.173, "Ks": 15.0,  "penetrability": 60},
        # 层3 淀积层 (60-100cm): 砂31% 黏27% 粉42%  容重1310 kg/m³  OC 0 g/kg
        # Cosby估算=99,粘重淀积层调低至 30
        {"depth": 0.40, "SAT": 0.506, "FC": 0.300, "PWP": 0.173, "Ks": 30.0,  "penetrability": 80},
    ],
}

# --------------------------------------------------------------------------- #
#  蒸发与径流参数（按水田/旱田分别设置）
#
#  产流机制差异（关键！）：
#  - 水田：田埂蓄满溢流机制（bunds=True 时 内核跳过 CN 计算）
#          降雨全部进入入渗/蓄存 → 入渗受表层 Ks 限制 → 多余水形成田面水层
#          → 水层超过田埂高度(z_bund)才溢流。CN 实际不参与计算，
#          此处保留仅用于语义完整与无田埂情景的退化计算。
#  - 旱田：SCS-CN 曲线数法，CN 直接决定降雨-径流分配
#
#  - EVAP_Z_*: 土壤蒸发影响深度（m），水田因有水层覆盖蒸发浅，旱田裸土蒸发深
# --------------------------------------------------------------------------- #
# 水田（水稻）：水层覆盖，土壤蒸发弱；产流由田埂溢流控制（非 CN）
PADDY_EVAP_Z_SURF = 0.04
PADDY_EVAP_Z_MIN  = 0.10
PADDY_EVAP_Z_MAX  = 0.20
PADDY_CN = 78.0       # 仅占位，田埂开启时不参与计算（蓄满溢流机制）

# 旱田（玉米）：裸土蒸发强，疏松耕作层入渗好；产流由 CN 控制
DRYLAND_EVAP_Z_SURF = 0.04
DRYLAND_EVAP_Z_MIN  = 0.15
DRYLAND_EVAP_Z_MAX  = 0.30
DRYLAND_CN = 61.0     # 旱田粉壤土，中等透水（CN 实际生效）


def get_evap_params(crop: str) -> dict:
    """根据作物返回蒸发深度参数。"""
    if crop.lower() in ("rice", "paddy", "水稻"):
        return {
            "evap_z_surf": PADDY_EVAP_Z_SURF,
            "evap_z_min":  PADDY_EVAP_Z_MIN,
            "evap_z_max":  PADDY_EVAP_Z_MAX,
            "cn":          PADDY_CN,
        }
    return {
        "evap_z_surf": DRYLAND_EVAP_Z_SURF,
        "evap_z_min":  DRYLAND_EVAP_Z_MIN,
        "evap_z_max":  DRYLAND_EVAP_Z_MAX,
        "cn":          DRYLAND_CN,
    }


def get_soil_config(crop: str) -> dict:
    """根据作物类型返回对应土壤配置。
    crop: 'rice' -> 水田土壤；'corn'/'maize' -> 旱田土壤
    """
    from config.simulation_settings import get_active
    c = crop.lower()
    key = "rice" if c in ("rice", "paddy", "水稻") else "corn" if c in ("corn", "maize", "玉米") else c
    override = get_active(key).get("soil")
    if override is not None:
        return override
    if c in ("rice", "paddy", "水稻"):
        return PADDY_SOIL
    if c in ("corn", "maize", "玉米"):
        return DRYLAND_SOIL
    raise ValueError(f"未知作物: {crop}")


if __name__ == "__main__":
    for name, cfg in (("水田", PADDY_SOIL), ("玉米田", DRYLAND_SOIL)):
        print(f"\n=== {name}: {cfg['name']} (preset={cfg['preset']}) ===")
        for i, lay in enumerate(cfg["layers"], 1):
            print(f"  层{i}: {lay['depth']:.2f}m  SAT={lay['SAT']:.2f} "
                  f"FC={lay['FC']:.2f} PWP={lay['PWP']:.2f} Ks={lay['Ks']:.1f}mm/d")
