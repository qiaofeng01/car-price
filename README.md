# 车价测算站 · 部署手册

> 👉 不懂代码、只想把网站上线：先看 **[新手操作手册.md](新手操作手册.md)**，那篇是点按钮的版本。本篇是技术说明。

> 目标：全量车型库，每天自动更新，托管在 GitHub Pages，朋友点链接就能用。
> 关键设计：**数据不进仓库**。每天由 Actions 现场抓取 → 打包 → 发布，仓库永远只有代码。

---

## 0. 前置条件

- GitHub 账号（公开仓库，Pages 免费；私有仓库需要 Pro）
- 本地：git、Python 3.10+
- 会看 Actions 日志就够了，不需要服务器

---

## 1. 仓库结构

```
car-price/
├─ .github/workflows/deploy.yml   # 每天定时：抓数据 → 打包 → 发布
├─ scripts/fetch.py               # 采集脚本（抓取逻辑在这里，唯一需要改的文件）
├─ site/                          # 站点根目录（发布这个目录）
│  ├─ index.html                  # 前端页面（你的界面放这里）
│  └─ data/                       # 运行时生成，不进仓库（.gitignore）
├─ requirements.txt
└─ README.md
```

`site/data/` 是构建产物，加进 `.gitignore`：

```gitignore
site/data/
__pycache__/
```

---

## 2. 第一步：建仓库、推代码

```bash
# 本地初始化
git init
git add .
git commit -m "init: 车价测算站骨架"

# 建远程仓库（用 gh CLI，或网页上手动建）
gh repo create car-price --public --source=. --push
```

没有 gh CLI 就在网页建空仓库，然后：

```bash
git remote add origin https://github.com/<你的用户名>/car-price.git
git branch -M main
git push -u origin main
```

---

## 3. 第二步：打开 Pages

仓库 → **Settings → Pages → Build and deployment**

- **Source** 选 **GitHub Actions**（不是 "Deploy from a branch"）
- 保存即可，不需要选分支

地址会是：

```
https://<用户名>.github.io/car-price/
```

第一次部署完成前访问会 404，正常。

---

## 4. 第三步：先跑通空链路（用示例数据）

这一步的目的是**先确认"部署"这段是通的**，再去解决"抓取"这个难点。

1. 仓库 → **Actions** → 左侧 `update-and-deploy` → 右侧 **Run workflow**
2. 输入参数保持默认（第一次会用 `--demo` 生成 3 个品牌的示例数据）
3. 跑完（约 1 分钟）→ 打开 `https://<用户名>.github.io/car-price/`

能看到页面、能选品牌车系、能出价格，就说明发布链路 OK。

> 若 Actions 里看不到按钮：把 workflow 里的 `on:` 加上 `workflow_dispatch:`（骨架里已包含）。

---

### 本地先自测（可选，但建议）

`ash
python scripts/fetch.py --out site/data --demo     # 生成示例数据
python -m http.server 8000 --directory site        # 起本地服务
# 打开 http://localhost:8000
`

能看到 3 个品牌、点选后出价，说明前端与 JSON 的结构是对得上的。

---

## 5. 第四步：接真实数据源

这一步是全部工作量所在。`scripts/fetch.py` 已经写好流程骨架，你只需要填 `Adapter` 里的三个方法：

| 方法 | 要返回什么 |
|---|---|
| `brands()` | `[{"id","name"}]` 品牌列表 |
| `series(brand_id)` | `[{"id","name"}]` 车系列表 |
| `trims(series_id)` | `[{"year","name","guide","energy","disp"}]` 配置与指导价 |

常用三种反爬与对策：

| 手段 | 现象 | 对策 |
|---|---|---|
| 字体加密 | 源码里价格是乱码字符 | 下载 woff 字体，建"字符 → 数字"映射表，一次性做好复用 |
| 接口签名 | 请求带 `sign`/`nonce`，拼不出来 | 用 Playwright 跑真实页面，取渲染后的 DOM |
| 频控 | 429 / 空响应 | 限速 1 次/秒、失败指数退避、只抓变更项 |

**先在本地跑通一个品牌**，命令：

```bash
pip install -r requirements.txt
python scripts/fetch.py --out site/data --brand vw --limit 3 --verbose
```

跑通后再上全量，避免在 CI 里烧 40 分钟还看不到错。

---

## 6. 第五步：定时

workflow 里已有：

```yaml
on:
  schedule:
    - cron: '0 22 * * *'   # UTC 22:00 = 北京时间次日 06:00
  workflow_dispatch:
```

⚠️ **GitHub 的 cron 是 UTC**，且高峰期会延迟 5–30 分钟，属于正常。

| 想在北京时间 | 填 cron |
|---|---|
| 每天 06:00 | `0 22 * * *` |
| 每天 08:00 | `0 0 * * *` |
| 每 6 小时 | `0 */6 * * *` |

---

## 7. 第六步：验收清单

部署后逐条过一遍：

- [ ] 打开站点，页面显示"数据更新于 YYYY-MM-DD"
- [ ] 选品牌 → 车系 → 配置，三级联动无空档
- [ ] 抽 20 条指导价，与官网/汽车之家比对，误差为 0
- [ ] 购置税按能源类型正确减免（新能源减半、上限 1.5 万）
- [ ] 手机浏览器打开正常（朋友大概率用手机）
- [ ] 断网/首次加载时，页面有兜底提示而不是白屏
- [ ] Actions 连续跑 3 天都成功

---

## 8. 七个必踩的坑

1. **60 天不活跃，定时任务会被自动停用**
   公开仓库 60 天无提交，GitHub 会关掉 schedule。对策：workflow 里保留手动触发，或每月手动跑一次；也可以在仓库里加一个每月自动更新的心跳文件。

2. **数据写进仓库 = 仓库爆炸**
   全量分片每天 5–8MB，一年多出 2GB。本方案数据只在构建产物里，不落库。若要落库，必须用 `force_orphan` 的部署方式。

3. **`permissions` 没写对，部署步骤直接失败**
   必须是 `pages: write` + `id-token: write`，骨架里已配好。

4. **全量抓取可能超时**
   Actions 单任务上限 6 小时，但你的耐心只有 40 分钟。对策：`--limit` 分批、只抓变更、加缓存（已抓过的品牌跳过）。

5. **抓取失败导致整站空数据**
   `fetch.py` 失败时不要覆盖旧数据。骨架里的做法是：先写临时目录，全部成功才替换；失败则沿用上次缓存。

6. **国内访问 github.io 有时很慢**
   朋友打开慢的话，两条路：上 Cloudflare Pages（同样的仓库，配置几乎一样），或把 `site/` 同步到腾讯云 COS 静态托管。

7. **全量抓取接近超时**
   2000+ 车系按 1 次/秒算至少 35 分钟，加上重试容易顶到 90 分钟上限。
   对策：用 matrix 把品牌按首字母拆成 4 个并行 job，各自产出分片后合并；或分两次跑（单号跑 A–M，双号跑 N–Z）。

8. **站点路径大小写敏感**
   GitHub Pages 是 Linux，`Data/Index.json` 和 `data/index.json` 是两个东西，本地 Windows 测不出来。

---

## 9. 数据规模与性能

| 项 | 量级 | 处理方式 |
|---|---|---|
| 品牌 | ~120 | 全量放 `index.json` |
| 车系 | ~2000 | 放 `index.json`（只留 id/名称） |
| 配置 | 2–5 万 | **按品牌分片**，选中品牌才加载 |
| 首屏 | ≤ 100KB | index.json 只有品牌+车系名 |
| 单品牌分片 | 20–200KB | 一次请求，秒开 |

不要做成一个 5MB 的大 JSON —— 朋友在店里用 4G 打开会以为你页面坏了。

---

## 10. 合规

- 抓取前看 `robots.txt` 与用户协议，控制频率，不要并发狂打
- 页面底部注明数据来源与更新日期，价格标"仅供参考，以门店成交为准"
- 自用/朋友小范围没问题；**对外公开或商用前，换成付费数据接口或取得授权**

---

## 附：本地预览

```bash
cd site
python -m http.server 8000
# 打开 http://localhost:8000
```

> 直接双击 index.html 会因浏览器安全策略读不到 JSON（file:// 协议），必须用本地服务。
