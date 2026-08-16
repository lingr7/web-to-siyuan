---
name: zhihu-to-siyuan
description: "将知乎内容（回答/想法/专栏）完整抓取并保存为思源笔记（SiYuan）文档，包含正文文字与图片按原文位置保存。触发场景：用户分享知乎链接要求保存到思源笔记、存成笔记、收藏到思源。涵盖两条链路：快速纯文本归档（WebFetch），以及完整保真链路（Playwright 本机 Chrome 无头抓取 + 图片下载 + 思源 API 写入），并包含思源 API 的多个关键坑位（标题不生效、updateBlock 根块不持久化、资产落错目录等）。"
agent_created: true
---

# Zhihu to SiYuan

## Overview

将知乎内容抓取并存入思源笔记。有两条链路，按需选择：

| 链路 | 工具 | 文字 | 图片 | 适用 |
|---|---|---|---|---|
| A. 快速归档 | WebFetch | ✅（可能不完整/抓错） | ❌ 全部丢失 | 只要文字、不在乎完整性 |
| B. 完整保真 | Playwright + 本机 Chrome 无头 | ✅ 完整 | ✅ 按原位置内联 | 默认推荐，尤其内容含图片或较长时 |

**重要教训**：链路 A 的 WebFetch 会把 `<img>` 标签直接丢弃（图片 URL 和位置信息都拿不到），且长回答经常只抓到前半段、多回答页面可能抓错回答。用户在意内容完整性时应直接走链路 B。

## 链路 B：完整保真流水线（推荐）

### Step 0: 环境准备（一次性）

```bash
# 在 node 隔离工作区安装 playwright（不要全局安装）
mkdir -p "<node工作区>" && cd "<node工作区>"
npm install playwright
# 无需下载自带浏览器——脚本用本机真实 Chrome（channel:'chrome'）
```

要求本机装有 Google Chrome（`C:/Program Files/Google/Chrome/Application/chrome.exe`）。Edge 也可以（channel 改为 'msedge'）。

### Step 1: 准备 articles.json

```json
[
  {"idx": "01", "type": "answer",  "url": "https://www.zhihu.com/question/xxx/answer/yyy", "title": "问题标题", "docId": ""},
  {"idx": "02", "type": "pin",     "url": "https://www.zhihu.com/pin/xxx", "title": "想法开头…", "docId": ""},
  {"idx": "03", "type": "article", "url": "https://zhuanlan.zhihu.com/p/xxx", "title": "专栏标题", "docId": ""}
]
```

- `type`: answer（回答）/ pin（想法）/ article（专栏）
- `title`: pin 没有标题，取正文开头若干字
- `docId` 留空，create 后自动回写

### Step 2: 验证思源 API 并获取笔记本

思源本地 API 固定端口 **6806**（注意：不是 52926）。

```bash
curl -s -X POST "http://127.0.0.1:6806/api/system/version" -H "Content-Type: application/json" -d '{}'
curl -s -X POST "http://127.0.0.1:6806/api/notebook/lsNotebooks" -H "Content-Type: application/json" -d '{}'
```

### Step 3: 运行抓取脚本

```bash
PLAYWRIGHT_BROWSERS_PATH="<node工作区>/pw-browsers" \
NODE_PATH="<node工作区>/node_modules" \
node scripts/zhihu_extract.js articles.json ./zhihu_extract
```

产出：
- `zhihu_extract/<idx>_blocks.json` — 正文块序列（text/img 交替，保留图片位置）
- `zhihu_extract/imgs/` — 下载的图片
- `zhihu_extract/summary.json` — 汇总

检查 summary：`ok` 应为 true；answer 类型时 `firstIsTarget` 应为 true，否则抓错回答需排查。

### Step 4: 写入思源

```bash
python scripts/siyuan_pipeline.py create --notebook <笔记本ID> --articles articles.json --extract-dir ./zhihu_extract
```

create 会自动完成：建文档（createDocWithMd）→ 修标题（renameDoc + setBlockAttrs）→ 上传图片并按原位置内联 → 资产归位（fix-assets）→ 回写 docId 到 articles.json。

已有文档但内容错误/不完整时，用 rebuild 重建（删子块 + insertBlock）：

```bash
python scripts/siyuan_pipeline.py rebuild --notebook <笔记本ID> --articles articles.json --extract-dir ./zhihu_extract
```

### Step 5: 验证

```bash
python scripts/siyuan_pipeline.py verify --articles articles.json --extract-dir ./zhihu_extract
```

每篇应接近 100% 命中（纯图片回答无文本块属正常；被过滤的"xx 赞同"元信息块未命中也属预期）。

### Step 6: 返回 deep link

```
http://127.0.0.1:6806/stage/build/desktop/?id=<doc_id>
```

用户要求可直接点击的本地链接，多篇时用表格列出。

## 链路 A：快速纯文本归档（WebFetch）

仅当内容不含图片且较短时使用：

1. WebFetch 抓取，prompt 必须强调"完整提取所有文字、不总结、不省略、按原文顺序输出"
2. 用 Python 脚本（参考 siyuan_pipeline.py 的 create_doc 函数）调 createDocWithMd 建文档，标题修复两步缺一不可
3. 返回 deep link

风险（均实际发生过）：长回答只抓到前半段；多回答页面把不同回答内容互换/误抓推荐流内容；图片全部丢失。抓完建议与原文抽查比对。

## Key Learnings（思源 API 坑位清单）

1. **端口固定 6806**。52926 等旧端口不可用。
2. **createDocWithMd 标题不生效**：文档会显示"未命名文档"。必须 `renameDoc` + `setBlockAttrs(custom-sy-title-empty="false", title=...)` 两步都做。
3. **updateBlock 对文档根块（type=d）静默失败**：返回 code=0 但内容不持久化。改用「SQL 查子块 id → 逐个 deleteBlock → insertBlock(parentID=doc_id)」重建。已验证可持久化。
4. **/api/asset/upload 落错目录**：上传的文件存到全局 `/data/assets/`，而文档内 `assets/xxx` 相对链接按笔记本目录解析 → 图片 404。修复：用 `/api/file/getFile` 读出，再 `/api/file/putFile` 写到 `/data/<notebook>/assets/`（siyuan_pipeline.py 的 fix-assets 子命令）。
5. **asset/upload 文件字段名是 `file[]`**，不是 `files[]`/`file`。
6. **SQL 查询端点是 `/api/query/sql`**，参数名 `stmt`（不是 sql）。图片在导出的 markdown 里是内联语法，SQL 中 type='img' 查不到独立块。
7. **JSON 转义**：标题/正文含引号时，不要用 shell 拼 JSON，用 Python 脚本构造请求体。

## Key Learnings（知乎反爬与抓取要点）

1. **直接 curl / 知乎 API v4 / WebFetch 都被 403** 或降级，图片 URL 拿不到。
2. **Playwright 自带 Chromium 无头模式被知乎识别**（跳转"安全验证"页）。解法：`channel: 'chrome'` 用本机真实 Chrome 新无头内核 + `--disable-blink-features=AutomationControlled` + addInitScript 抹掉 `navigator.webdriver` 等特征。实测可通过。
3. **严禁全页滚动加载**：知乎虚拟列表会把目标回答移出 DOM，导致抓到推荐流的其他回答（真实踩坑：文档内容互相错乱）。图片 URL 就在 `data-original`/`data-actualsrc` 属性里，无需滚动。
4. **回答锚定**：`.AnswerItem` 的 `data-zop` 属性（JSON）里 `itemId` 等于 answerId 的才是目标回答，不要默认取第一个。
5. **想法（pin）页结构**：`.PinItem .RichContent-inner`（不存在 `.PinItem-content`）。
6. **图片下载**：页面内 fetch 会被 CORS 拦截，必须用 `context.request.get(url, {headers: {Referer: 页面URL}})`（走浏览器 Cookie + 带 Referer 绕防盗链）。
7. **登录弹窗**：无头访问常弹登录框遮挡"阅读全文"按钮，先 `page.evaluate` 移除 `.signFlowModal, .Modal-wrapper` 再点展开。
8. **纯图片回答存在**：有的回答正文只有一张图无文字（blocks 里只有 img），不是抓取失败。

## Scripts & Docs

- `scripts/zhihu_extract.js` — Playwright 抓取（blocks 结构 + 图片下载），用法 `node zhihu_extract.js <articles.json> [outdir]`
- `scripts/siyuan_pipeline.py` — 思源写入流水线，子命令 create / rebuild / fix-assets / verify
- `examples/articles.example.json` — 输入配置格式示例
- `docs/siyuan-api-pitfalls.md` — 思源 API 坑位详解
- `docs/zhihu-anti-scraping.md` — 知乎反爬与抓取要点
- `README.md` — GitHub 项目风格总览（快速开始 / CLI 参考 / 目录结构）
