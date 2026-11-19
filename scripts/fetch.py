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


class AutoHomeAdapter(Adapter):
    """汽车之家适配器（待实现）。

    实现要点：
      1. 品牌/车系列表页：解析即可，结构相对稳定
      2. 指导价：注意字体加密（woff 里的字符映射），先建 map 再取数
      3. 分页 + 限速，单品牌抓完 sleep 1s
    自测：
      python scripts/fetch.py --brand vw --limit 3 -v
    """

    BASE = "https://www.autohome.com.cn"

    def brands(self):
        # TODO: 抓品牌页，解析出 id/name
        raise NotImplementedError("待实现：品牌列表解析")

    def series(self, brand_id):
        # TODO: 抓品牌下车系列表
        raise NotImplementedError("待实现：车系列表解析")

    def trims(self, series_id):
        # TODO: 抓该车系配置页，注意指导价字体解密
        raise NotImplementedError("待实现：配置与指导价解析")


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
    ap.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args()

    adapter = DemoAdapter() if a.demo else AutoHomeAdapter()
    build(adapter, a.out, only_brand=a.brand, limit=a.limit)


if __name__ == "__main__":
    sys.exit(main())
