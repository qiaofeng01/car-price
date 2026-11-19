# -*- coding: utf-8 -*-
"""
车型库采集骨架：抓取 → 校验 → 分片 JSON。

用法：
    python scripts/fetch.py --out site/data --demo            # 示例数据，验证链路
    python scripts/fetch.py --out site/data --brand vw --limit 3 -v
    python scripts/fetch.py --out site/data                  # 全量

产物：
    site/data/index.json          品牌 + 车系索引（首屏加载，<=100KB）
    site/data/brands/<id>.json    按品牌分片的配置与指导价
    site/data/meta.json           时间戳、统计、失败项
"""
import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone

try:
    import requests
    SESS = requests.Session()
except ImportError:                      # demo 模式不需要联网，允许缺依赖
    requests = None
    SESS = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

ENERGY = {"燃油", "混动", "插电混动", "纯电", "增程", "氢燃料"}
PRICE_RANGE = (20_000, 5_000_000)
MIN_INTERVAL = 1.0          # 同一域名的请求间隔（秒），别把对方打挂
RETRY = 3


# ----------------------------------------------------------------- 工具
def log(*a):
    print("[fetch]", *a, flush=True)


class Limiter:
    """简单的域名级限速。"""

    def __init__(self, interval=MIN_INTERVAL):
        self.interval = interval
        self._last = {}

    def wait(self, url):
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        last = self._last.get(host, 0)
        gap = time.time() - last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last[host] = time.time()


if SESS is not None:
    SESS.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
LIMITER = Limiter()


def get(url, **kw):
    if SESS is None:
        raise RuntimeError("需要 requests：pip install -r requirements.txt")
    for attempt in range(1, RETRY + 1):
        try:
            LIMITER.wait(url)
            r = SESS.get(url, timeout=20, **kw)
            if r.status_code == 429:
                time.sleep(5 * attempt)
                continue
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            log(f"  请求失败({attempt}/{RETRY}) {url[:80]} -> {e}")
            if attempt == RETRY:
                raise
            time.sleep(2 * attempt)


def slug(s):
    """品牌/车系 id：去空格与特殊字符，保持稳定。"""
    s = re.sub(r"[^\w\u4e00-\u9fa5-]", "", s.strip())
    return s or "unknown"


# ----------------------------------------------------------------- 数据源
class Adapter:
    """要接新站点，只实现这三个方法。"""

    def brands(self):
        """-> [{"id": "vw", "name": "一汽-大众"}]"""
        raise NotImplementedError

    def series(self, brand_id):
        """-> [{"id": "bora", "name": "宝来"}]"""
        raise NotImplementedError

    def trims(self, series_id):
        """-> [{"year": "2026", "name": "300TSI DSG尊行版",
                "guide": 113900, "energy": "燃油", "disp": "1.4T"}]"""
        raise NotImplementedError


class DemoAdapter(Adapter):
    """示例数据：不联网，用来先验证发布链路。"""

    def brands(self):
        return [{"id": "vw", "name": "一汽-大众"},
                {"id": "byd", "name": "比亚迪"},
                {"id": "tsl", "name": "特斯拉"}]

    def series(self, brand_id):
        return {"vw": [{"id": "bora", "name": "宝来"}, {"id": "sagitar", "name": "速腾"}],
                "byd": [{"id": "qinplus", "name": "秦PLUS"}, {"id": "songplus", "name": "宋PLUS"}],
                "tsl": [{"id": "model3", "name": "Model 3"}]}.get(brand_id, [])

    def trims(self, series_id):
        base = {"bora": 113900, "sagitar": 149900, "qinplus": 99800,
                "songplus": 159800, "model3": 235500}.get(series_id, 120000)
        out = []
        for i, name in enumerate(["舒适版", "豪华版", "尊行版", "旗舰版"]):
            out.append({"year": "2026", "name": name,
                        "guide": base + i * 10000,
                        "energy": "纯电" if series_id == "model3" else "燃油",
                        "disp": "—" if series_id == "model3" else "1.5T"})
        return out


def guess_energy(fuel_form):
    s = str(fuel_form or "")
    if "插电" in s or "PHEV" in s.upper():
        return "插电混动"
    if "增程" in s:
        return "增程"
    if "纯电" in s or s.strip() in ("电动", "纯电动"):
        return "纯电"
    if "混" in s or "HEV" in s.upper():
        return "混动"
    if "氢" in s:
        return "氢燃料"
    return "燃油"


class DongchediAdapter(Adapter):
    """懂车帝适配器（免登录 JSON 接口，2026-11 实测可用）。

    三条接口：
      1. 榜单   /motor/pc/car/rank_data              → 车系、品牌、在售车款 id
      2. 车系头 /motor/car_page/m/v1/get_head        → car_id_list 兜底
      3. 车款   /motor/car_page/v4/get_entity_json   → 逐款指导价（万元，需 ×10000）

    注意：
      - 指导价单位是「万元」，必须 ×10000
      - 站点有频控，默认 0.8s/次；被拦截会返回空或 login-required
      - 覆盖率以「在售且有销量记录的车系」为主，冷门/停售车系可能缺失
    """

    BASE = "https://www.dongchedi.com"
    BATCH = 10          # 一次请求取几个车款
    RANK_PAGES = 40     # 榜单最多翻 40 页（50/页 ≈ 2000 个车系）

    def __init__(self, city="广州", limit_series=0, verbose=False):
        self.city = city
        self.limit_series = limit_series
        self.verbose = verbose
        self._enum = None
        self._by_id = {}

    # ---------- 底层 ----------
    def _json(self, path, **params):
        r = get(self.BASE + path, params=params,
                headers={"Referer": self.BASE + "/auto/library"})
        try:
            return r.json()
        except Exception:
            raise RuntimeError(f"返回非 JSON（可能被风控）：{(r.text or '')[:160]}")

    # ---------- 枚举 ----------
    def _enumerate(self):
        if self._enum is not None:
            return self._enum
        enum, seen = {}, set()
        for page in range(self.RANK_PAGES):
            data = self._json("/motor/pc/car/rank_data", aid=1839, app_name="auto_web_pc",
                              count=50, offset=page * 50, rank_data_type=11)
            lst = ((data.get("data") or {}).get("list")) or []
            if not lst:
                break
            for it in lst:
                sid = str(it.get("series_id") or "")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                brand = (it.get("brand_name") or "其他").strip()
                bid = str(it.get("brand_id") or slug(brand))   # 优先用数字 id，URL 更稳
                enum.setdefault(bid, {"name": brand, "series": {}})["series"][sid] = {
                    "id": sid,
                    "name": (it.get("series_name") or "").strip(),
                    "car_ids": [str(c) for c in (it.get("online_car_ids") or [])],
                }
            if self.verbose and page % 5 == 0:
                log(f"  枚举第 {page + 1} 页，累计车系 {len(seen)}")
            if len(lst) < 50:
                break
        if not seen:
            raise RuntimeError("榜单接口返回空：可能被风控，或接口已变更")
        self._enum = enum
        for b in enum.values():
            for sid, s in b["series"].items():
                self._by_id[sid] = s
        log(f"枚举完成：{len(enum)} 个品牌 / {len(seen)} 个车系")
        return enum

    def brands(self):
        return [{"id": bid, "name": b["name"]} for bid, b in self._enumerate().items()]

    def series(self, brand_id):
        out = list(self._enumerate().get(brand_id, {}).get("series", {}).values())
        return out[:self.limit_series] if self.limit_series else out

    # ---------- 车款 ----------
    def _car_ids(self, s):
        if s.get("car_ids"):
            return s["car_ids"]
        head = self._json("/motor/car_page/m/v1/get_head/",
                          series_id=s["id"], city_name=self.city, data_from="pc_station")
        return [x for x in (head.get("car_id_list") or "").split(",") if x]

    def trims(self, series_id):
        self._enumerate()
        s = self._by_id.get(str(series_id))
        if s is None:
            raise RuntimeError(f"车系 {series_id} 不在枚举结果中")
        ids = self._car_ids(s)
        out = []
        for i in range(0, len(ids), self.BATCH):
            data = self._json("/motor/car_page/v4/get_entity_json/",
                              car_id_list=",".join(ids[i:i + self.BATCH]), city_name=self.city)
            for c in ((data.get("data") or {}).get("car_info") or []):
                info = c.get("info") or {}
                val = (info.get("official_price") or {}).get("compare_value")
                if not isinstance(val, (int, float)):
                    try:
                        val = float(str(c.get("official_price") or "").replace("万", ""))
                    except Exception:
                        continue
                out.append({
                    "year": str(c.get("car_year") or ""),
                    "name": (c.get("car_name") or "").strip(),
                    "guide": int(round(float(val) * 10000)),
                    "energy": guess_energy((info.get("fuel_form") or {}).get("value")),
                    "disp": (info.get("capacity_l") or {}).get("value") or "",
                })
        return out


# ----------------------------------------------------------------- 校验
def validate(trims, series_name):
    ok, dropped = [], 0
    for t in trims:
        guide = t.get("guide")
        if not isinstance(guide, int) or not (PRICE_RANGE[0] <= guide <= PRICE_RANGE[1]):
            dropped += 1
            continue
        if not str(t.get("name", "")).strip():
            dropped += 1
            continue
        if t.get("energy") not in ENERGY:
            t["energy"] = "燃油"
        ok.append(t)
    if dropped and not ok:
        log(f"  [警告] {series_name} 全部配置校验失败")
    return ok


# ----------------------------------------------------------------- 主流程
def build(adapter, out_dir, only_brand=None, limit=0):
    log("拉取品牌列表…")
    brands = adapter.brands()
    log(f"品牌 {len(brands)} 个")
    if only_brand:
        brands = [b for b in brands if b["id"] == only_brand]
        if not brands:
            raise SystemExit(f"品牌 {only_brand} 不存在")

    index = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
             "count": {"brands": 0, "series": 0, "trims": 0}, "brands": []}
    failed = []

    tmp = out_dir + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "brands"), exist_ok=True)

    for bi, b in enumerate(brands, 1):
        try:
            series = adapter.series(b["id"])
        except Exception as e:  # noqa: BLE001
            log(f"[{bi}/{len(brands)}] {b['name']} 车系抓取失败：{e}")
            failed.append({"brand": b["name"], "reason": str(e)[:120]})
            continue

        if limit:
            series = series[:limit]

        detail = {"brand": b["name"], "series": []}
        n_trims = 0
        for s in series:
            try:
                trims = validate(adapter.trims(s["id"]), s["name"])
            except Exception as e:  # noqa: BLE001
                log(f"   {s['name']} 配置抓取失败：{e}")
                continue
            if not trims:
                continue
            detail["series"].append({"id": s["id"], "name": s["name"], "trims": trims})
            n_trims += len(trims)

        if not detail["series"]:
            failed.append({"brand": b["name"], "reason": "无有效配置"})
            continue

        with open(os.path.join(tmp, "brands", b["id"] + ".json"), "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, separators=(",", ":"))

        index["brands"].append({"id": b["id"], "name": b["name"],
                                "n": len(detail["series"]),
                                "t": n_trims})
        index["count"]["brands"] += 1
        index["count"]["series"] += len(detail["series"])
        index["count"]["trims"] += n_trims
        log(f"[{bi}/{len(brands)}] {b['name']}: {len(detail['series'])} 车系 / {n_trims} 配置")

    if index["count"]["brands"] == 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit("一个品牌都没抓到，放弃覆盖旧数据（保留上次结果）")

    meta = {"updated": index["updated"],
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": index["count"], "failed": failed}
    with open(os.path.join(tmp, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 全部成功才替换旧目录，避免半成品上线
    shutil.rmtree(out_dir, ignore_errors=True)
    os.replace(tmp, out_dir)

    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(out_dir) for f in fs)
    log(f"完成：{index['count']}，产物 {size/1024/1024:.1f} MB，失败 {len(failed)} 项")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--demo", action="store_true", help="用示例数据")
    ap.add_argument("--brand", help="只跑某个品牌 id")
    ap.add_argument("--limit", type=int, default=0, help="每品牌最多几个车系")
    ap.add_argument("--city", default="广州", help="报价城市，影响经销商价（指导价全国一致）")
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    if a.demo:
        adapter = DemoAdapter()
    else:
        adapter = DongchediAdapter(city=a.city, verbose=a.verbose)
    build(adapter, a.out, only_brand=a.brand, limit=a.limit)


if __name__ == "__main__":
    sys.exit(main())
