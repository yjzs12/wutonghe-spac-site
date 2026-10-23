"""AquaCrop 四阶段墒情阈值搜索；所有目标值均来自实际模拟。

先在 0/50/100 的四维网格上探索，再用 25/10/5/1 个百分点逐级搜索。
产量最高的组合为最优；同等产量下选择田间净灌水最少者。
有限搜索报告已评估候选及 Pareto 前沿，不宣称连续空间的全局最优。
"""
from contextlib import redirect_stdout
from dataclasses import replace
from itertools import product
import io
import math

from modules.irrigation import TYPE_SMT, corn_smt_label


def optimize_corn(simulate, sim_year, fixed_results,
                  application_efficiency_pct=90., log=print):
    cache = {}
    for result in fixed_results:
        if result.mode == "smt":
            cache[tuple(result.smt)] = result

    def evaluate(values):
        key = tuple(max(0, min(100, int(v))) for v in values)
        if key not in cache:
            with redirect_stdout(io.StringIO()):
                result = simulate("corn", corn_smt_label(key), list(key),
                                  mode="smt", scheme_type=TYPE_SMT,
                                  sim_year=sim_year,
                                  application_efficiency_pct=application_efficiency_pct)
            yld, water = scores(result)
            if not math.isfinite(yld) or not math.isfinite(water) or yld < 0 or water < 0:
                raise ValueError(f"墒情搜索模拟值无效: {key}")
            cache[key] = result
            if len(cache) % 20 == 0:
                log(f"  墒情控制：已完成 {len(cache)} 组 AquaCrop 阈值模拟", flush=True)
        return cache[key]

    def scores(result):
        return (float(result.summary["最终籽粒产量(t/ha)"]),
                float(result.summary["累计灌溉量(mm)"]))

    def peak_key(key):
        yld, water = scores(cache[key])
        return (-yld, water, key)

    def refine(start, step, key_fn):
        current = start
        # At most two sweeps per resolution; repeated points use the per-run cache.
        for _ in range(2):
            previous = current
            for stage in range(4):
                neighbors = {current}
                for delta in (-step, step):
                    point = list(current)
                    point[stage] = max(0, min(100, point[stage] + delta))
                    neighbors.add(tuple(point))
                for point in sorted(neighbors):
                    evaluate(point)
                current = min(neighbors, key=key_fn)
            if current == previous:
                break
        return current

    log("  墒情控制：搜索四阶段阈值（0—100，整数百分点）；最高产、同产量最少净灌水", flush=True)
    # Real rainfed is separately simulated with irrigation_method=0. Threshold 0
    # may trigger emergency irrigation and must never be relabeled as rainfed.
    for values in product((0, 50, 100), repeat=4):
        evaluate(values)
    for step in (25, 10, 5, 1):
        starts = sorted(cache, key=peak_key)[:2]
        for start in starts:
            refine(start, step, peak_key)

    chosen = min(cache, key=peak_key)
    best = cache[chosen]
    rainfed = next((r for r in fixed_results if r.mode == "rainfed"), None)
    use_rainfed = (rainfed is not None and
                   (-scores(rainfed)[0], scores(rainfed)[1]) <=
                   (-scores(best)[0], scores(best)[1]))
    if use_rainfed:
        chosen = (0, 0, 0, 0)
        best = replace(rainfed, smt=list(chosen))
    rows = []
    for key, result in sorted(cache.items()):
        yld, water = scores(result)
        rows.append({"初始期阈值(%)": key[0], "发育期阈值(%)": key[1],
                     "中期阈值(%)": key[2], "末期阈值(%)": key[3],
                     "产量(t/ha)": yld, "净灌溉量(mm)": water,
                     "控制方式": "墒情控制",
                     "选中": not use_rainfed and key == chosen})
    if rainfed is not None:
        rows.append({"初始期阈值(%)": 0, "发育期阈值(%)": 0,
                     "中期阈值(%)": 0, "末期阈值(%)": 0,
                     "产量(t/ha)": scores(rainfed)[0], "净灌溉量(mm)": scores(rainfed)[1],
                     "控制方式": "雨养（关闭灌溉）",
                     "选中": use_rainfed})
    for row in rows:
        row["Pareto前沿"] = not any(
            other["产量(t/ha)"] >= row["产量(t/ha)"] and
            other["净灌溉量(mm)"] <= row["净灌溉量(mm)"] and
            (other["产量(t/ha)"] > row["产量(t/ha)"] or
             other["净灌溉量(mm)"] < row["净灌溉量(mm)"]) for other in rows)
    report = {"engine": "AquaCrop", "year": sim_year,
              "method": "三点四维网格 + 多起点逐级坐标搜索（25/10/5/1）",
              "bounds_pct": [0, 100], "resolution_pct": 1,
              "objective": "max_yield_then_min_irrigation",
              "selection_rule": "产量最高；同等产量下田间净灌水最少",
              "peak_yield_t_ha": scores(best)[0],
              "selected_smt": list(chosen), "evaluations": len(cache),
              "selected_control": "rainfed" if use_rainfed else "smt",
              "selected_yield_t_ha": scores(best)[0],
              "selected_net_irrigation_mm": scores(best)[1],
              "scope": "已评估SMT组合及雨养中的最高产、同产量最少净灌水方案；不保证连续空间全局最优",
              "candidates": rows}
    summary = dict(best.summary)
    summary["墒情优化"] = report
    log(f"  已选 {corn_smt_label(chosen, True)}：产量 {scores(best)[0]:.4f} t/ha，"
        f"净灌水 {scores(best)[1]:.2f} mm", flush=True)
    return replace(best, label=corn_smt_label(chosen, True), summary=summary)
