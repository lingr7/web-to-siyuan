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
3. **updateBlock 对文档根块（type=d）静默失败**：返回 code=0 但内容不持久化。改用「查子块 id → 逐个 deleteBlock → insertBlock(parentID=doc_id)」重建。
4. **（2026-08-16 修正）deleteBlock 参数名是 `id`，不是 `blockID`**：传 `blockID` 返回 code=0 但静默空操作（data=null；真正删除时 data 含 delete 操作记录）。曾误判为"思源 API bug"，受控对照实验定位为脚本参数名错误——曾因此导致 rebuild 残留旧块 + 追加新块 = 文档内容重复。另：**SQL 查 blocks 表有异步索引延迟，不是随机漏行**（2026-08-16 受控实验：快速写 30 块后 SQL 只报 29，约 4 秒后收敛到 32，同一时刻重复查询结果稳定）。写入后立刻用 SQL 查会"缺行"，这是思源已知设计（数据异步写入索引，官方在 ld246 有说明，关联 issue siyuan-note/siyuan#3212）。查刚写入的块一律用 `/api/block/getChildBlocks`（实时、不走索引）。rebuild_doc 已内置：删除后循环校验 + 失败降级 removeDoc 整篇重建（doc_id 会变，回写 articles.json）。
5. **/api/asset/upload 落错目录**：上传的文件存到全局 `/data/assets/`，而文档内 `assets/xxx` 相对链接按笔记本目录解析 → 图片 404。修复：用 `/api/file/getFile` 读出，再 `/api/file/putFile` 写到 `/data/<notebook>/assets/`（siyuan_pipeline.py 的 fix-assets 子命令）。
6. **asset/upload 文件字段名是 `file[]`**，不是 `files[]`/`file`。
7. **SQL 查询端点是 `/api/query/sql`**，参数名 `stmt`（不是 sql）。图片在导出的 markdown 里是内联语法，SQL 中 type='img' 查不到独立块。
8. **JSON 转义**：标题/正文含引号时，不要用 shell 拼 JSON，用 Python 脚本构造请求体。

## Key Learnings（知乎反爬与抓取要点）

1. **直接 curl / 知乎 API v4 / WebFetch 都被 403** 或降级，图片 URL 拿不到。
2. **Playwright 自带 Chromium 无头模式被知乎识别**（跳转"安全验证"页）。解法：`channel: 'chrome'` 用本机真实 Chrome 新无头内核 + `--disable-blink-features=AutomationControlled` + addInitScript 抹掉 `navigator.webdriver` 等特征。实测可通过。
3. **严禁全页滚动加载**：知乎虚拟列表会把目标回答移出 DOM，导致抓到推荐流的其他回答（真实踩坑：文档内容互相错乱）。图片 URL 就在 `data-original`/`data-actualsrc` 属性里，无需滚动。
4. **回答锚定**：`.AnswerItem` 的 `data-zop` 属性（JSON）里 `itemId` 等于 answerId 的才是目标回答，不要默认取第一个。
5. **想法（pin）页结构**：`.PinItem .RichContent-inner`（不存在 `.PinItem-content`）。
6. **图片下载**：页面内 fetch 会被 CORS 拦截，必须用 `context.request.get(url, {headers: {Referer: 页面URL}})`（走浏览器 Cookie + 带 Referer 绕防盗链）。
7. **登录弹窗**：无头访问常弹登录框遮挡"阅读全文"按钮，先 `page.evaluate` 移除 `.signFlowModal, .Modal-wrapper` 再点展开。
8. **纯图片回答存在**：有的回答正文只有一张图无文字（blocks 里只有 img），不是抓取失败。
9. **超链接保留**（2026-08-16 修复）：抓取时切勿用 `innerText` 提取文本（会丢弃全部 `<a href>`）。`zhihu_extract.js` 的 `mdInline()` 会把 `<a>` 序列化为 `[文本](URL)` markdown，并自动解开知乎 `link.zhihu.com/?target=` 跳转为真实 URL。写入端（createDocWithMd / insertBlock dataType=markdown）原生支持链接语法。注意：verify 命中率会因链接语法略降（SiYuan 导出格式差异），属预期。
10. **链接卡片（LinkCard）懒加载陷阱**（2026-08-16 修复）：正文里的知乎问题卡片是 `<a>` + `.LinkCard-title.loading`，标题**依赖可视区懒加载**；不滚动的无头抓取拿到的是空标题，若直接丢弃空 label 的 `<a>` 会**静默丢失整批卡片链接**（实测丢 6 个）。修复（已实装 zhihu_extract.js）：a) 空 label 的 `<a>` 先标记为 `[__LINKCARD__](url)`；b) 抓取后批量调知乎编辑器元数据 API `GET /api/v4/editor/link_card_infos?scene=pcweb&urls=<逗号连接>`（用 `context.request.get` 带浏览器 Cookie），从返回的 `extra_info` JSON 取 `title` 回填；失败兜底 `[链接](url)`；c) 另有 `waitForFunction` 等待已加载卡片的兜底。

## 批量模式：剪藏失败笔记重建（zhihu_reclip.py）

当思源中存在**大量剪藏失败的知乎笔记**（典型来自印象笔记迁移：标题为 URL / "无标题笔记"、正文只含"剪藏失败"和原文链接）时，不要逐条走链路 B，用批量工具：

```bash
cd scripts
python zhihu_reclip.py scan      # 扫描识别失败笔记（四重策略防漏）
python zhihu_reclip.py delete    # 删除旧失败文档
python zhihu_reclip.py fetch     # 自动生成 articles.json 并调用 zhihu_extract.js 批量抓取
python zhihu_reclip.py process   # 创建新文档（垃圾过滤+图片内联+fix-assets）
python zhihu_reclip.py verify    # 验证标题+内容命中率
python zhihu_reclip.py report    # 最终报告（含可点击 deep link）
python zhihu_reclip.py status    # 随时查看进度（只读）
python zhihu_reclip.py reset <hash>  # 重置单条状态重试
```

要点（千条规模实战设计）：
- **状态机 + 断点续传**：`pending → deleted → fetched → created → verified → done`，任何时刻中断重跑即恢复
- **运行数据与代码分离**：状态/缓存/日志写 `D:\zhihu-reclip-data`（环境变量 `RECLIP_DATA_DIR` 覆盖），不污染 skill 目录
- **扫描四重策略取并集**：思源 SQL 有索引 bug（`type='d'` 批量查询随机漏文档），必须加**文件树 API 递归遍历**兜底
- **notebook_id 每次校验刷新**：状态里存的笔记本可能已删除/重建
- API 限流 0.15s、指数退避重试、原子写入（临时文件+rename）

## Scripts & Docs

- `scripts/zhihu_extract.js` — Playwright 抓取（blocks 结构 + 图片下载），用法 `node zhihu_extract.js <articles.json> [outdir]`
- `scripts/siyuan_pipeline.py` — 思源写入流水线，子命令 create / rebuild / fix-assets / verify
- `scripts/zhihu_reclip.py` — 批量模式：剪藏失败笔记扫描/重建/验证（千条规模，断点续传），运行数据写 `D:\zhihu-reclip-data`
- `examples/articles.example.json` — 输入配置格式示例
- `docs/siyuan-api-pitfalls.md` — 思源 API 坑位详解
- `docs/zhihu-anti-scraping.md` — 知乎反爬与抓取要点
- `README.md` — GitHub 项目风格总览（快速开始 / CLI 参考 / 目录结构）
