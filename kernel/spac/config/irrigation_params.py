"""Workbook utilization ranges; application efficiency has a separate boundary.

Profiles are assumptions chosen by the user, not observations of the district.
The selected utilization coefficient is treated as source-to-effective-field
efficiency for the source-demand estimate. Do not multiply AppEff into it again.
"""
import math

UTILIZATION_PROFILES = {
    "unspecified": {"label": "未指定（只报告田间用水）", "crops": ["rice", "corn"], "range": None},
    "rice_surface": {"label": "水田·地表水", "crops": ["rice"], "range": [0.68, 0.75], "source_cell": "P3"},
    "rice_groundwater": {"label": "水田·地下水", "crops": ["rice"], "range": [0.80, 0.90], "source_cell": "T3"},
    "corn_canal": {"label": "旱田·地面渠灌", "crops": ["corn"], "range": [0.65, 0.75], "source_cell": "R8"},
    "corn_well": {"label": "旱田·地面井灌", "crops": ["corn"], "range": [0.75, 0.85], "source_cell": "Q8"},
    "corn_sprinkler": {"label": "旱田·喷灌", "crops": ["corn"], "range": [0.80, 0.86], "source_cell": "S8"},
    "corn_micro": {"label": "旱田·微喷灌", "crops": ["corn"], "range": [0.86, 0.90], "source_cell": "T8"},
    "corn_drip": {"label": "旱田·滴灌", "crops": ["corn"], "range": [0.90, 0.95], "source_cell": "U8"},
}


def utilization_settings(crop, profile="unspecified", coefficient=None, application_efficiency_pct=90.):
    if profile not in UTILIZATION_PROFILES or crop not in UTILIZATION_PROFILES[profile]["crops"]:
        raise ValueError("作物与灌溉利用系数类型不匹配")
    field=float(application_efficiency_pct)
    if not math.isfinite(field) or not 0<field<=100:
        raise ValueError("田间施加效率须大于0且不超过100%")
    item=UTILIZATION_PROFILES[profile]
    limits=item["range"]
    if limits is None:
        if coefficient is not None:
            raise ValueError("请先指定供水/灌溉方式，再填写利用系数")
        value=None
    else:
        value=sum(limits)/2 if coefficient is None else float(coefficient)
        if not math.isfinite(value) or not limits[0]<=value<=limits[1]:
            raise ValueError(f"{item['label']}利用系数须在{limits[0]}—{limits[1]}之间")
        if value>field/100+1e-10:
            raise ValueError("按含田间损失的总利用系数口径，总系数不能高于田间施加效率；请核实这两个参数")
    return dict(profile=profile,label=item["label"],coefficient=value,reference_range=limits,
                application_efficiency_pct=field,source="产量 生育期.xlsx / Sheet1",
                source_cell=item.get("source_cell"),selection_basis="区间中值假设" if limits and coefficient is None else "用户设置" if limits else "未指定",
                boundary="净需水量÷所选总利用系数；总系数含田间损失，不再乘除田间效率")
