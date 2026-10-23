"""Per-run calendar and soil settings; never change shared model defaults."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import date, timedelta
from functools import lru_cache
import math

_active = ContextVar("spac_simulation_settings", default={})

# Exact identifiers supported by the installed AquaCrop Soil constructor.
SOIL_TYPES = {
    "Default": "模型默认土壤", "Sand": "砂土", "LoamySand": "壤质砂土",
    "SandyLoam": "砂壤土", "Loam": "壤土", "SiltLoam": "粉壤土",
    "Silt": "粉土", "SandyClayLoam": "砂质黏壤土", "ClayLoam": "黏壤土",
    "SiltClayLoam": "粉质黏壤土", "SandyClay": "砂质黏土",
    "SiltClay": "粉质黏土", "Clay": "黏土", "Paddy": "水田土",
    "ac_TunisLocal": "突尼斯分层土壤",
}


def get_active(crop):
    return _active.get().get(crop, {})


def planting_date(crop):
    from config import wutonghe_config as wc
    default = wc.RICE_PLANTING_DATE if crop == "rice" else wc.CORN_PLANTING_DATE
    return get_active(crop).get("planting_date", default)


def harvest_date(crop):
    from config import wutonghe_config as wc
    default = wc.RICE_HARVEST_DATE if crop == "rice" else wc.CORN_HARVEST_DATE
    value = get_active(crop).get("harvest_date")
    return date.fromisoformat(value).strftime("%m/%d") if value else default


@contextmanager
def use_settings(settings):
    token = _active.set(deepcopy(settings))
    try:
        yield
    finally:
        _active.reset(token)


@lru_cache(maxsize=1)
def _soil_catalog():
    from config.soil_params import PADDY_SOIL, DRYLAND_SOIL, get_evap_params
    from modules.engine_core import Soil
    catalog = {}
    for key, crop, cfg, label in (
        ("wutonghe_paddy", "rice", PADDY_SOIL, "梧桐河水稻土（当前默认）"),
        ("wutonghe_dryland", "corn", DRYLAND_SOIL, "梧桐河旱田白浆土（当前默认）"),
    ):
        catalog[key] = dict(deepcopy(cfg), label=label, cn=get_evap_params(crop)["cn"],
                            source="本地格网估计与等效水力参数，待验证")
    for key, label in SOIL_TYPES.items():
        soil = Soil(key, dz=[0.1] * (20 if key in ("Paddy", "ac_TunisLocal") else 10))
        layers = []
        groups = list(soil.profile.groupby("Layer", sort=True))
        for i, (_, frame) in enumerate(groups):
            row = frame.iloc[0]
            # TunisLocal internally replaces dz with a 1.55 m grid. Its declared
            # layers are 0.3 + 1.7 m; use those layer boundaries on our 0.1 m grid.
            depth = ([0.3, 1.7][i] if key == "ac_TunisLocal" else round(float(frame.dz.sum()), 6))
            layers.append(dict(depth=depth, PWP=float(row.th_wp), FC=float(row.th_fc),
                               SAT=float(row.th_s), Ks=float(row.Ksat),
                               penetrability=float(row.penetrability)))
        catalog[key] = dict(name=f"{label}（{key}）", label=f"{label} · {key}",
                            preset=key, cn=float(soil.cn), layers=layers,
                            source="AquaCrop 内置预设；非本地实测。突尼斯预设采用原定义2m分层。" if key == "ac_TunisLocal"
                            else "AquaCrop 内置预设；非本地实测")
    return catalog


def capabilities():
    from config import wutonghe_config as wc
    from config.crop_params import get_crop_params
    from config.crop_calendar import reference_calendar, MIN_SEASON_DAYS
    return dict(
        soils=deepcopy(_soil_catalog()),
        defaults={c: dict(planting_date=planting_date(c).replace("/", "-"),
                         soil_type="wutonghe_paddy" if c == "rice" else "wutonghe_dryland")
                  for c in ("rice", "corn")},
        sim_start=wc.SIM_START_MD.replace("/", "-"),
        sim_end=wc.SIM_END_MD.replace("/", "-"),
        calendar=deepcopy(reference_calendar()),
        min_season_days=MIN_SEASON_DAYS,
        min_soil_depth={c: round(get_crop_params(c)["Zmax"] + 0.1, 6) for c in ("rice", "corn")},
        harvest={"rice": wc.RICE_HARVEST_DATE.replace("/", "-"),
                 "corn": wc.CORN_HARVEST_DATE.replace("/", "-")},
    )


def validate_settings(crops, year, options=None):
    from config import wutonghe_config as wc
    from config.crop_params import get_crop_params
    from config.crop_calendar import allocate_stages, MIN_SEASON_DAYS
    catalog = _soil_catalog()
    result = {}
    for crop in crops:
        item = (options or {}).get(crop, {})
        raw_date = item.get("planting_date")
        if raw_date:
            try:
                planted = date.fromisoformat(raw_date)
            except (TypeError, ValueError) as exc:
                raise ValueError("播种/移栽日期须为有效日期 YYYY-MM-DD") from exc
            if planted.year != year:
                raise ValueError("播种/移栽日期必须属于模拟年份")
        else:
            md = wc.RICE_PLANTING_DATE if crop == "rice" else wc.CORN_PLANTING_DATE
            planted = date.fromisoformat(f"{year}-{md.replace('/', '-')}")
        start = date.fromisoformat(f"{year}-{wc.SIM_START_MD.replace('/', '-')}")
        end_md = wc.RICE_HARVEST_DATE if crop == "rice" else wc.CORN_HARVEST_DATE
        default_plant = wc.RICE_PLANTING_DATE if crop == "rice" else wc.CORN_PLANTING_DATE
        default_span = (date.fromisoformat(f"{year}-{end_md.replace('/', '-')}") -
                        date.fromisoformat(f"{year}-{default_plant.replace('/', '-')}")).days
        raw_harvest = item.get("harvest_date")
        if raw_harvest:
            try:
                end = date.fromisoformat(raw_harvest)
            except (TypeError, ValueError) as exc:
                raise ValueError("收获日期须为有效日期 YYYY-MM-DD") from exc
        else:
            end = planted + timedelta(days=default_span)
        sim_end = date.fromisoformat(f"{year}-{wc.SIM_END_MD.replace('/', '-')}")
        if end.year != year or end > sim_end:
            raise ValueError(f"收获日期必须属于模拟年份，且不晚于{sim_end}")
        if (end - planted).days < MIN_SEASON_DAYS:
            raise ValueError(f"收获日期须晚于播种/移栽日期至少{MIN_SEASON_DAYS}天")
        # Rice requires five preparation days plus one preceding soil-state day.
        earliest = start + timedelta(days=6 if crop == "rice" else 0)
        if not earliest <= planted < end:
            raise ValueError(f"{'水稻移栽' if crop == 'rice' else '玉米播种'}日期须在{earliest}至{end - timedelta(days=1)}之间")
        key = item.get("soil_type", "wutonghe_paddy" if crop == "rice" else "wutonghe_dryland")
        if key not in catalog:
            raise ValueError("未知土壤类型，请选择模型提供的预设")
        cfg = deepcopy(catalog[key])
        if item.get("soil_layers") is not None:
            cfg["layers"] = deepcopy(item["soil_layers"])
        if item.get("soil_cn") is not None:
            cfg["cn"] = item["soil_cn"]
        layers = cfg["layers"]
        if not isinstance(layers, list) or not 1 <= len(layers) <= 10:
            raise ValueError("土壤剖面须含1至10层")
        for layer in layers:
            if set(layer) != {"depth", "PWP", "FC", "SAT", "Ks", "penetrability"}:
                raise ValueError("土层必须包含厚度、PWP、FC、SAT、Ks及根系穿透率")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in layer.values()):
                raise ValueError("土壤参数必须为有限数值")
            if not 0 < layer["PWP"] < layer["FC"] < layer["SAT"] < 1:
                raise ValueError("土壤含水率须满足 0 < PWP < FC < SAT < 1（m³/m³）")
            if not 0 < layer["Ks"] <= 10000 or not 0 < layer["penetrability"] <= 100:
                raise ValueError("Ks须在(0,10000] mm/d，根系穿透率须在(0,100]%")
            if not 0.1 <= layer["depth"] <= 3 or abs(layer["depth"] * 10 - round(layer["depth"] * 10)) > 1e-7:
                raise ValueError("每层厚度须为0.1m的整数倍，范围0.1至3m")
        depth = sum(layer["depth"] for layer in layers)
        if depth + 1e-8 < get_crop_params(crop)["Zmax"] + 0.1 or depth > 3.0 + 1e-8:
            raise ValueError("土壤总深度须覆盖最大根深及0.1m底边界，且不超过3m")
        if not isinstance(cfg["cn"], (int, float)) or not math.isfinite(cfg["cn"]) or not 1 <= cfg["cn"] <= 100:
            raise ValueError("径流曲线数CN须在1至100之间")
        modified = cfg["layers"] != catalog[key]["layers"] or cfg["cn"] != catalog[key]["cn"]
        cfg["name"] += "（用户修改）" if modified else ""
        cfg["dz"] = [0.1] * round(depth * 10)
        result[crop] = dict(planting_date=planted.strftime("%m/%d"),
                            planting_date_iso=planted.isoformat(), harvest_date=end.isoformat(),
                            calendar_stages=allocate_stages(crop, planted, end),
                            soil_type=key, soil_modified=modified, soil=cfg)
    return result
