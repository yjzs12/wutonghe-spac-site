# -*- coding: utf-8 -*-
"""梧桐河灌区模型平台（微服务版）—— 全局配置。

单一配置来源：**配置文件 + 环境变量**两级覆盖，环境变量优先。
这样同一份代码在三种场景下都不用改：

  1. 本机开发      —— 读 平台配置.json，内核指向 kernel/（或原始模型目录）
  2. 内网服务器    —— 用 WTH_* 环境变量覆盖路径与端口
  3. 公网沙箱发布  —— 只有 PORT 一个环境变量，其余全走默认值

关键约定
--------
* 三个进程的端口：
    gateway      取 PORT（云沙箱注入）/ WTH_PORT / 8760
    spac 服务    取 SPAC_PORT / WTH_SPAC_PORT / 8801（只绑 127.0.0.1）
    crop 服务    取 CROP_PORT / WTH_CROP_PORT / 8802（只绑 127.0.0.1）
  只有 gateway 绑 0.0.0.0 对外，两个服务仅本机可达 —— 这正是"微服务 + 网关"的拓扑。
* 每个服务有自己的 URL 前缀（base_path）：网关 ""、spac "/spac"、crop "/crop"。
  页面与前端资源里的 `__BASE__` 占位符就是它。独立启动某服务时把前缀设成 ""。
* 内核目录默认指向 kernel/（由 kernel/同步内核.py 从两个模型目录复制生成）。
  想直接用原始模型目录，改 平台配置.json 的 paths.spac_model / crop_model 即可。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "平台配置.json"

DEFAULTS: dict = {
    "_说明": [
        "相对路径以本文件所在目录（模型平台微服务/）为基准。",
        "环境变量优先于本文件，见 core/config.py 顶部注释。",
        "改完重启服务生效。",
    ],
    "paths": {
        "spac_kernel": "kernel/spac",
        "crop_kernel": "kernel/crop",
        "preset_data": "data/crop",
    },
    "ports": {
        "gateway": 8760,
        "spac": 8801,
        "crop": 8802,
    },
    "runtime": {
        "work": "work",
        "uploads": "uploads",
        "auto_open_browser": True,
    },
}

_BOOL_TRUE = ("1", "true", "yes", "on", "y", "t")


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def as_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _BOOL_TRUE


def as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class Config:
    """运行期配置（进程内单例 CONFIG）。"""

    def __init__(self) -> None:
        self.root = ROOT
        self.file = CONFIG_FILE
        self._raw = self._load_file()
        self.merged = self._apply_env(_deep_merge(DEFAULTS, self._raw))

        paths = self.merged.get("paths", {})
        self.spac_dir = self._resolve(paths.get("spac_kernel"),
                                      DEFAULTS["paths"]["spac_kernel"])
        self.crop_dir = self._resolve(paths.get("crop_kernel"),
                                      DEFAULTS["paths"]["crop_kernel"])
        self.preset_data_dir = self._resolve(paths.get("preset_data"),
                                             DEFAULTS["paths"]["preset_data"])

        ports = self.merged.get("ports", {})
        self.gateway_port = as_int(os.environ.get("PORT")
                                   or os.environ.get("WTH_PORT")
                                   or ports.get("gateway"), 8760)
        self.spac_port = as_int(os.environ.get("SPAC_PORT")
                                or os.environ.get("WTH_SPAC_PORT")
                                or ports.get("spac"), 8801)
        self.crop_port = as_int(os.environ.get("CROP_PORT")
                                or os.environ.get("WTH_CROP_PORT")
                                or ports.get("crop"), 8802)

        rt = self.merged.get("runtime", {})
        self.work_dir = self._resolve(rt.get("work"), DEFAULTS["runtime"]["work"])
        self.upload_dir = self._resolve(rt.get("uploads"), DEFAULTS["runtime"]["uploads"])
        self.auto_open_browser = as_bool(os.environ.get("WTH_OPEN_BROWSER"),
                                         as_bool(rt.get("auto_open_browser"), False))

        # 云沙箱里 ROOT 是只读还是可写不确定，统一把运行期目录挪到可写位置
        if os.environ.get("WTH_RUNTIME_DIR"):
            base = Path(os.environ["WTH_RUNTIME_DIR"]).expanduser().resolve()
            self.work_dir = base / "work"
            self.upload_dir = base / "uploads"

        for d in (self.work_dir, self.upload_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    # ---- 读写 ----
    def _load_file(self) -> dict:
        if not self.file.exists():
            try:
                self.file.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
            except OSError:
                pass
            return {}
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _apply_env(self, cfg: dict) -> dict:
        cfg = json.loads(json.dumps(cfg))
        env = os.environ
        p = cfg.setdefault("paths", {})
        t = cfg.setdefault("ports", {})
        r = cfg.setdefault("runtime", {})
        if env.get("WTH_SPAC_KERNEL"):
            p["spac_kernel"] = env["WTH_SPAC_KERNEL"]
        if env.get("WTH_CROP_KERNEL"):
            p["crop_kernel"] = env["WTH_CROP_KERNEL"]
        if env.get("WTH_PRESET_DATA"):
            p["preset_data"] = env["WTH_PRESET_DATA"]
        if env.get("WTH_GATEWAY_PORT"):
            t["gateway"] = env["WTH_GATEWAY_PORT"]
        if env.get("WTH_SPAC_PORT"):
            t["spac"] = env["WTH_SPAC_PORT"]
        if env.get("WTH_CROP_PORT"):
            t["crop"] = env["WTH_CROP_PORT"]
        if env.get("WTH_WORK"):
            r["work"] = env["WTH_WORK"]
        return cfg

    # ---- 工具 ----
    def _resolve(self, value, fallback: str) -> Path:
        raw = value if value not in (None, "") else fallback
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else (self.root / p).resolve()

    def as_dict(self) -> dict:
        return {
            "ok": True,
            "root": str(self.root),
            "config_file": str(self.file),
            "kernel": {
                "spac": str(self.spac_dir),
                "spac_exists": self.spac_dir.exists(),
                "crop": str(self.crop_dir),
                "crop_exists": self.crop_dir.exists(),
            },
            "preset_data": {
                "dir": str(self.preset_data_dir),
                "exists": self.preset_data_dir.exists(),
            },
            "ports": {
                "gateway": self.gateway_port,
                "spac": self.spac_port,
                "crop": self.crop_port,
            },
            "runtime": {"work": str(self.work_dir), "uploads": str(self.upload_dir)},
            "env_overrides": sorted(k for k in os.environ if k.startswith(("WTH_", "PORT"))),
        }


CONFIG = Config()

# 服务拓扑：网关是唯一对外入口，两个服务只监听本机回环
SPAC_HOST = os.environ.get("WTH_SVC_HOST", "127.0.0.1")
CROP_HOST = SPAC_HOST
