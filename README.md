# web-to-siyuan

> 将知乎内容（回答 / 想法 / 专栏）完整抓取并归档为 [思源笔记](https://github.com/siyuan-note/siyuan) 文档 —— 文字完整、图片按原文位置内联保存。

[![Node](https://img.shields.io/badge/node-%E2%89%A518-339933)](https://nodejs.org)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.9-3776AB)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

## ✨ 特性

- **内容保真**：基于 Playwright 驱动本机真实 Chrome（新无头内核）抓取，绕过知乎反爬，文字与图片一次拿全
- **图片按位置内联**：图片 URL 取自 `data-actualsrc` / `data-original`，与正文块交替保存，归档后位置与原文一致
- **精确锚定回答**：通过 `.AnswerItem` 的 `data-zop.itemId` 匹配目标回答，避免多回答页面抓错内容
- **思源 API 全链路**：建文档 → 修标题 → 传图片 → 资产归位 → 回写 docId → 一键验证匹配度
- **可重复、可校验**：`verify` 子命令逐块核对抓取内容与文档实际内容，接近 100% 命中才算成功

## 🏗️ 工作流程

```
articles.json ──► zhihu_extract.js ──► zhihu_extract/
 (链接清单)       (Playwright+Chrome)    ├── 01_blocks.json   正文块序列
                  │                     ├── 02_blocks.json
                  │                     ├── imgs/            图片文件
                  ▼                     └── summary.json     抓取汇总
             siyuan_pipeline.py
                  │
        create ─ rebuild ─ fix-assets ─ verify
                  ▼
           思源笔记 (http://127.0.0.1:6806)
```

## 📦 环境要求

| 依赖 | 说明 |
|---|---|
| Node.js ≥ 18 | 运行抓取脚本，需安装 `playwright`（无需下载自带浏览器） |
| Python ≥ 3.9 | 运行写入流水线，仅用标准库，零第三方依赖 |
| Google Chrome | 本机真实 Chrome（`channel:'chrome'` 用于过知乎反爬）；Edge 亦可（改 `channel:'msedge'`） |
| 思源笔记 | 本地运行并开启 API，端口 **6806** |

## 🚀 快速开始

### 1. 安装依赖

```bash
# 在隔离的 node 工作区安装 playwright（不要全局安装）
mkdir -p ~/pw-workspace && cd ~/pw-workspace
npm install playwright
```

### 2. 准备 articles.json

```json
[
  {"idx": "01", "type": "answer",  "url": "https://www.zhihu.com/question/xxx/answer/yyy", "title": "问题标题", "docId": ""},
  {"idx": "02", "type": "pin",     "url": "https://www.zhihu.com/pin/xxx", "title": "想法开头若干字…", "docId": ""},
  {"idx": "03", "type": "article", "url": "https://zhuanlan.zhihu.com/p/xxx", "title": "专栏标题", "docId": ""}
]
```

字段说明：`type` 取 `answer`（回答）/ `pin`（想法）/ `article`（专栏）；`title` 对 pin 取正文开头；`docId` 留空，create 后自动回写。完整示例见 [`examples/articles.example.json`](./examples/articles.example.json)。

### 3. 抓取知乎内容

```bash
PLAYWRIGHT_BROWSERS_PATH=~/pw-workspace/pw-browsers \
NODE_PATH=~/pw-workspace/node_modules \
node scripts/zhihu_extract.js articles.json ./zhihu_extract
```

检查 `zhihu_extract/summary.json`：每篇 `ok` 应为 `true`；`answer` 类型 `firstIsTarget` 应为 `true`（否则抓错回答需排查）。

### 4. 写入思源笔记

```bash
# 获取笔记本 ID
curl -s -X POST "http://127.0.0.1:6806/api/notebook/lsNotebooks" -H "Content-Type: application/json" -d '{}'

# 创建文档（建文档 + 修标题 + 图片内联 + 资产归位 + 回写 docId，一步到位）
python scripts/siyuan_pipeline.py create --notebook <笔记本ID> --articles articles.json --extract-dir ./zhihu_extract
```

### 5. 验证与访问

```bash
python scripts/siyuan_pipeline.py verify --articles articles.json --extract-dir ./zhihu_extract
```

每篇应接近 100% 命中。之后即可通过 deep link 直接打开文档：

```
http://127.0.0.1:6806/stage/build/desktop/?id=<doc_id>
```

## 🧭 CLI 参考

### `scripts/zhihu_extract.js`

```
node zhihu_extract.js <articles.json> [outdir]   # outdir 默认 ./zhihu_extract
```

环境变量：`PLAYWRIGHT_BROWSERS_PATH`、`NODE_PATH`（指向含 playwright 的 node_modules）。

### `scripts/siyuan_pipeline.py`

| 子命令 | 作用 |
|---|---|
| `create` | 从抓取结果创建新文档，自动完成图片内联、标题修复、资产归位、docId 回写 |
| `rebuild` | 重建已有文档（内容错误 / 不完整时用，删子块 + insertBlock） |
| `fix-assets` | 把误存到全局 `/data/assets/` 的图片归位到笔记本 assets 目录 |
| `verify` | 核对抓取内容与文档实际内容的匹配度 |

## ⚠️ 已知坑位（实战踩坑记录）

**思源 API**（详见 [docs/siyuan-api-pitfalls.md](./docs/siyuan-api-pitfalls.md)）：

- `updateBlock` 对文档根块返回 `code=0` 但**不持久化** → 必须删子块 + `insertBlock` 重建
- `/api/asset/upload` 落到全局 `/data/assets/`，文档相对链接 404 → `putFile` 归位到笔记本 assets
- `createDocWithMd` 标题不生效 → `renameDoc` + `setBlockAttrs` 两步缺一不可
- SQL 端点参数名是 `stmt`；上传文件字段名是 `file[]`

**知乎反爬**（详见 [docs/zhihu-anti-scraping.md](./docs/zhihu-anti-scraping.md)）：

- Playwright 自带 Chromium 无头被识别 → 用本机真实 Chrome `channel:'chrome'` + 反检测脚本
- **严禁全页滚动**：虚拟列表会把目标回答移出 DOM，抓到推荐流内容
- 图片下载必须 `context.request.get` + `Referer`（页面内 fetch 被 CORS / 防盗链拦截）

## 📁 目录结构

```
web-to-siyuan/
├── SKILL.md                        # WorkBuddy skill 入口（流程步骤）
├── README.md                       # 本文件
├── LICENSE
├── scripts/
│   ├── zhihu_extract.js            # 知乎抓取（Playwright + 本机 Chrome）
│   └── siyuan_pipeline.py          # 思源写入流水线（create/rebuild/fix-assets/verify）
├── examples/
│   └── articles.example.json       # 输入配置示例
└── docs/
    ├── siyuan-api-pitfalls.md      # 思源 API 坑位详解
    └── zhihu-anti-scraping.md      # 知乎反爬与抓取要点
```

## 📄 License

[MIT](./LICENSE)
