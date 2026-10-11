# -*- coding: utf-8 -*-
"""DataFrame → JSON 安全的表格结构；文件条目；结果清单整理。

前端只认一种表格结构：``{"columns": [...], "rows": [[...], ...]}``。
所有 numpy 标量在这里统一转成 Python 原生类型，避免 JSON 序列化报错。
"""
from __future__ import annotations

from pathlib import Path

# 面积统计表的中文列名映射（种植结构模块）
AREA_CN = {
    "crop_name": "作物", "class": "类别", "class_name": "地类",
    "area_ha": "面积(ha)", "area_km2": "面积(km2)", "area_m2": "面积(m2)",
    "count": "像元数", "percentage": "占比(%)", "ratio": "占比(%)",
    "pixel_count": "像元数", "geom_type": "几何类型",
}
AREA_CN.update({"class_name": "地类"})

# 影像时序曲线列名映射
TIMESERIES_CN = {
    "month": "月份", "Month": "月份", "date": "日期",
    "NDVI": "NDVI", "NDWI": "NDWI", "EVI": "EVI", "LSWI": "LSWI",
    "crop": "作物", "class_name": "地类",
}


def df_records(df) -> dict:
    """DataFrame → ``{columns, rows}``，值全部 JSON 安全。"""
    import numpy as np

    cols = [str(c) for c in df.columns]
    rows = []
    for _, r in df.iterrows():
        row = []
        for v in r.tolist():
            if isinstance(v, np.integer):
                row.append(int(v))
            elif isinstance(v, np.floating):
                row.append(None if np.isnan(v) else round(float(v), 6))
            elif isinstance(v, np.bool_):
                row.append(bool(v))
            elif v is None or (isinstance(v, float) and v != v):
                row.append(None)
            elif hasattr(v, "item") and not isinstance(v, (str, bytes)):
                try:
                    row.append(v.item())
                except Exception:                        # noqa: BLE001
                    row.append(str(v))
            else:
                row.append(v)
        rows.append(row)
    return {"columns": cols, "rows": rows}


def rename_cn(df, mapping: dict):
    return df.rename(columns={c: mapping.get(c, c) for c in df.columns})


def file_entries(out_dir: Path, rels: list[str]) -> list[dict]:
    """相对路径清单 → 带文件名/大小的前端条目（不存在的自动跳过）。"""
    out = []
    for rel in rels:
        p = Path(out_dir) / rel
        if p.is_file():
            out.append({"rel": rel.replace("\\", "/"), "name": p.name,
                        "size": p.stat().st_size})
    return out


def scan_files(out_dir: Path, skip_prefixes: tuple[str, ...] = (),
               skip_suffixes: tuple[str, ...] = (".json", ".log")) -> list[dict]:
    """递归列出任务输出目录下的交付物（跳过中间产物与任务元数据）。"""
    out = []
    for p in sorted(Path(out_dir).rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(out_dir).as_posix()
        if rel.startswith(skip_prefixes) or rel.endswith(skip_suffixes):
            continue
        try:
            out.append({"rel": rel, "name": p.name, "size": p.stat().st_size})
        except OSError:
            continue
    return out
