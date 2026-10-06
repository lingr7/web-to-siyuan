---
name: web-to-siyuan
description: "将知乎内容（回答/想法/专栏）、微信公众号文章（mp.weixin.qq.com）与通用 Discuz 论坛帖子（如 bbs.aardio.com）完整抓取并保存为思源笔记（SiYuan）文档，包含正文文字与图片按原文位置保存。触发场景：用户分享知乎链接、微信公众号链接或论坛帖子链接要求保存到思源笔记、存成笔记、收藏到思源；或要求归档某答主的全部回答/合集（答主合集模式：递归链接发现 + 批量提取 + 主文档/子文档批量入库）。涵盖多条链路：快速纯文本归档（WebFetch）、完整保真链路（Playwright 本机 Chrome 无头抓取 + 图片下载 + 思源 API 写入）、Discuz 论坛 curl 直抓（无反爬，HTML→Markdown 转换）、剪藏失败笔记批量重建（zhihu_reclip.py）、答主合集递归发现（recursive_crawler.js + author_pipeline.py），并包含思源 API 的多个关键坑位（标题不生效、updateBlock 根块不持久化、资产落错目录等），以及知乎风控降级页（403 → undefined_blocks.json）的抢救与预防。"
agent_created: true
---

# Web Clip to SiYuan（原 Zhihu to SiYuan）

> **姊妹 skill**：单文件/邮件附件归档（不走网页抓取）→ `save-file-to-siyuan`。本文档的 Key Learnings 是两个 skill 共用的思源 API 坑位权威参考。

## Overview

将知乎内容（回答/想法/专栏）与微信公众号文章抓取并存入思源笔记。有两条链路，按需选择：

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
  {"idx": "03", "type": "article", "url": "https://zhuanlan.zhihu.com/p/xxx", "title": "专栏标题", "docId": ""},
  {"idx": "04", "type": "weixin",  "url": "https://mp.weixin.qq.com/s/xxx", "title": "文章标题", "docId": ""}
]
```

- `type`: answer（回答）/ pin（想法）/ article（专栏）/ **weixin（微信公众号文章）**
- `title`: pin 没有标题，取正文开头若干字；weixin 取页面 `<h1 id="activity-name">` 或 document.title
- `docId` 留空，create 后自动回写

### Step 2: 验证思源 API 并获取笔记本

思源本地 API 固定端口 **6806**（注意：不是 52926）。

```bash
curl -s -X POST "http://127.0.0.1:6806/api/system/version" -H "Content-Type: application/json" -d '{}'
curl -s -X POST "http://127.0.0.1:6806/api/notebook/lsNotebooks" -H "Content-Type: application/json" -d '{}'
```

**写入前必做健康检查**：若返回 `502 Bad Gateway` / `upstream connect failed ... (os error 10061)`，说明**思源没在运行**，不是网络问题——不要重试、不要怀疑脚本。此时：抓取产物（blocks/imgs/articles.json）**已在盘上，原样保留**，告知用户启动思源后直接跑 create+verify 补写即可（产物可跨会话、跨天复用，2026-09 实测放置 5 天仍可正常入库）。

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
5. **/api/asset/upload 落错目录**：上传的文件存到全局 `/data/assets/`，而文档内 `assets/xxx` 相对链接按笔记本目录解析 → 图片 404。修复：用 `/api/file/getFile` 读出，再 `/api/file/putFile` 写到 `/data/<notebook>/assets/`（siyuan_pipeline.py 的 fix-assets 子命令）。**（2026-08-30 受控复测补充：全局 assets 的带时间戳文件实测 `GET /assets/<ts名>` 返回 200，`/assets/` 路由按文件名跨目录搜索（笔记本 + 全局）并在全局留副本；"落全局必 404"已过时。fix-assets 归位笔记本仍推荐——语义内聚、原名保留。putFile 直传笔记本 assets 同样实测可用，见坑位 8。）**
6. **asset/upload 文件字段名是 `file[]`**，不是 `files[]`/`file`。
7. **SQL 查询端点是 `/api/query/sql`**，参数名 `stmt`（不是 sql）。图片在导出的 markdown 里是内联语法，SQL 中 type='img' 查不到独立块。
8. **JSON 转义**：标题/正文含引号时，不要用 shell 拼 JSON，用 Python 脚本构造请求体。
9. **（2026-08-16 受控实验验证）createDocWithMd 的 `path` 是含标题的完整 hpath，不是父目录**：传 `/目录/标题` 会在该位置建出以此命名的文档，`title` 参数不生效（实测 title 被忽略）。传 `/` 则在根目录建"未命名文档"。另：**`removeDoc` 的 `path` 必须是物理 ID 路径 `/<parent_id>/<doc_id>.sy`（不带 notebook 前缀）**，传 hpath 或带 notebook 前缀的路径都会 `block not found`。`getBlockInfo` 返回的 `path` 含 notebook 前缀（`/<nb_id>/...`），需剥掉首段再用；其 `hpath` 字段恒为 None，层级信息只能从 state/外部记录获取。
10. **（2026-08-16 教训）URL 归一化必须补 scheme**：从笔记正文提取的链接可能是 `www.zhihu.com/...` 无 `https://` 前缀（印象笔记剪藏产物常见）。危害：a) Playwright `page.goto()` 抛 invalid URL 直接失败；b) 排序时无 scheme 的 URL 全部排在有 scheme 之后，批量抓取会"先难后易"集中崩溃；c) `来源：[知乎](url)` 链接不可点击。修复：normalize 环节 `startswith(('http://','https://'))` 判断 + 补 `https://`。**注意：URL 归一化规则变更会使 url_hash 漂移，存量 state 需一次性迁移（重算哈希、按深度合并冲突）。**
11. **（2026-08-30）通用端点调用格式（三个坑）**：a) `/api/file/putFile` 走 **multipart/form-data + 表单字段 `path`**，不是 JSON body、也不是 query 参数——Learning 5 的 fix-assets 已隐式使用，但格式未记录；b) `createDocWithMd` 在 `/api/filetree/` 下，**返回的 `data` 直接是 doc_id 字符串**（不是对象、不用再查一层）；c) `renameDoc`（`/api/filetree/renameDoc`）参数是 **`notebook + path(以 .sy 结尾的物理路径) + title`**，不是 `id + title`。
12. **（2026-09-06 教训）is_junk 垃圾过滤误杀长正文**：无锚点的 `\d+\s*赞同` 模式会把内嵌关联文章卡片的正文段整块误杀——专栏正文里「…巨大离婚收益**58 赞同 · 8 评论** 文章」是卡片标题的一部分，不是元信息（实测 02 前言段 400+ 字整块被过滤，verify 91% 未命中即此因）。修复：`is_junk` 加长度守卫——`len(text) >= 30` 的长块一律不过滤（真元信息块都 <30 字）。**改动后必查脚本里有无重复定义**（本次旧 `is_junk` 在 140 行还有一份，Python 后定义覆盖，差点白修）。
13. **（2026-09-06 教训）rebuild 的 removeDoc 降级路径会静默变更 doc_id**：rebuild 输出 `OK xx 字符` 不代表成功——若内部触发「失败降级 removeDoc 整篇重建」，原 doc 被删、新 doc_id 生成，但 articles.json **不会自动回写**。rebuild 后必须 `getChildBlocks` 实测（0 块 = 事故）；`getBlockInfo` 返回 not found 即触发过 removeDoc，需手动 create + 回写 docId。安全 SOP：备份 articles.json → 记录原块数 → rebuild → getChildBlocks 验证 → 异常时按"清空 docId → create → 手动回写"抢救。

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
11. **（2026-08-16 教训）批量抓取脚本的三条命脉**：a) **单条处理链（含 LinkCard 补标题、图片下载、文件落盘）必须整链 try-catch**——只有抓取部分有 try 的话，浏览器在下载图片环节崩溃就会杀死整个进程，后续 100+ 条全部丢；b) **summary.json 增量落盘**（每处理完一条就写），不要循环结束才写——否则崩溃时全部汇总丢失；c) **定期重启浏览器**（每 ~15 条）：知乎页面渲染几十个后 Chrome 内存暴涨，是批量中段异常的主因。另有：403 降级页正文渲染慢，用 `waitForFunction` 条件等待容器出现（25s）而非固定 sleep；单条 3 次重试每次开新 page，第 3 次失败重启整个浏览器。
12. **（2026-08-16 教训）抓取结果判定不能只看文件存在**：blocks 文件存在但 `ok:false` 或 `blocks:[]` 的，Python 侧必须读文件内容判定，否则失败项被误标 `fetched`、rebuild 时报"正文为空"误归因，且重跑 fetch 会跳过它们。同理，fetch 进程崩溃后未处理的 URL 应归因为"未处理（进程中断）"而非"反爬失败"——两者处理方式完全不同（后者会被无意义地反复重试）。
13. **（2026-08-27 教训）风控降级页的二次事故——`undefined_blocks.json`**：遇到 403 降级页时整篇正文被折叠为单块，且 `data-zop` 解析失败 → itemId 丢失 → 抓取产物落盘为 `undefined_blocks.json`（正常应为 `{idx}_blocks.json`）。抢救流程（277 块实战验证）：a) 清理 zhida 卡片链接等噪音；b) 按句末标点+空格切分恢复块结构；c) 重命名为 `{idx}_blocks.json` 后走正常 pipeline 入库。预防：articles.json 每条必须带 `idx` 字段；抓取完成后检查产物文件名，出现 `undefined_*` 即判定为降级页事故，不要直接入库。
14. **（2026-09-13 验证）批量外部链接的标题补齐走 link_card_infos API**：zhihu_extract.js 不回填 articles.json 的 title；从邮件/聊天记录批量归档时标题需自取。持续 403 降级页下 `h1.QuestionHeader-title` 抓不到（页面 title 也为空，重试无效），此时先 goto 任意知乎页拿 cookie（降级页也有 cookie），再调 `GET /api/v4/editor/link_card_infos?scene=pcweb&urls=<逗号连接>`（带 Referer）即可批量拿到问题标题。**响应是按完整 URL 键的对象**（`{"<url>": {extra_info: "{\"title\":...}"}}`），不是 `{data:[...]}`——解析须用 Object.entries 取键做 URL 匹配，title 在 extra_info JSON 字符串里。
15. **（2026-09-13）403 降级页不影响抓取与入库**：提取脚本内置的长等待自愈在持续风控下依然能拿全正文（6/6 成功），降级只影响 h1 标题——用 Learning 14 的 API 兜底即可，不必为标题反复重试页面。
16. **（2026-08-26 验证）WebFetch 预览会混入同页其他回答**：多回答页面上 WebFetch 给出的内容可能来自别的答主，Playwright 按 itemId 锚定抓到的才是目标回答。怀疑漏抓/错抓时写 debug 脚本 dump 全量块与原文逐段比对，不要只看文件存在。

## 反向检索：按描述定位知乎回答/文章（2026-09-28 实战）

场景：用户只给一句描述（"知乎上关于 XXX 的回答，答主名字开头是 Y"），没有链接。**不要**尝试知乎站内搜索——未登录全部不可用。

### 免登录能力矩阵（实测）

| 能力 | 端点 | 状态 |
|---|---|---|
| 站内内容搜索 | `/search?type=content&q=` | ❌ 未登录**显示"未搜索到相关内容"**（不是真的没内容） |
| 搜索 API | `/api/v4/search_v3` | ❌ `401 {"code":101,"name":"AuthenticationError","message":"ZERR_NOT_LOGIN"}` |
| 搜索联想词 | `/api/v4/search/suggest?q=` | ✅ 可用（可确认某话题在站内是否真实存在） |
| 专栏文章列表 | `/api/v4/columns/{c_id}/items?limit=100&offset=0` | ✅ 可用（但只返回前 ~10 条，翻页不生效） |
| 问题页 / 回答页 / 专栏文章页 | HTML | ✅ 可读（风控降级页也能拿到正文） |
| 用户主页 回答/文章/专栏 列表 | `/people/{token}/answers` | ❌ 显示"请登录后查看" |
| 话题页 | `/topic/{id}/hot`、`/newest` | ❌ 空白（body≈400 字符） |
| 问题回答列表 API | `/api/v4/questions/{id}/answers` | ❌ `40362` 反爬 |

### 推荐路径：第三方搜索引擎反查

1. **用 WebFetch 抓百度**：`https://www.baidu.com/s?wd=<自然语言查询>`。
   - 百度对知乎内容收录好；`site:zhihu.com` 操作符基本无效（返回官网首页），用**自然语言 + 关键词**更有效。
   - ⚠️ 不要用 curl 直连百度（返回 ~1.5KB 反爬页），也不要用 Playwright 连续查询（首查成功、后续全部 0 结果被限流）。
   - 一次查询给多个候选；换词多试几轮（"中文社区桌面版 powershell"、"hermes 系统 PowerShell 不依赖 Git Bash" 等）。
2. **解析跳转链**：百度结果里是 `http://www.baidu.com/link?url=<token>`，用
   `curl -sL -o /dev/null -w '%{url_effective}' "<link>"` 直接拿到真实知乎 URL（`curl` 可跟到 302 目标，虽然目标页本身 403）。
3. **用 Playwright 打开候选知乎页**，读作者 + 正文（脚本见下）。
4. 反向锚点：若只知道对方 GitHub/网名，**GitHub 个人资料的 `blog` 字段常直接是知乎专栏 URL**，由此反查 `/people/{url_token}`。

### 判定"答主名"的正确选择器

`.AnswerItem .AuthorInfo-name` 常常取到**空字符串**（新版 DOM 用了 CSS-in-JS）。可靠写法：

```js
const name = n.querySelector('.AuthorInfo[itemprop="author"] meta[itemprop="name"]')?.getAttribute('content');
```

（`.AuthorInfo-head` 下也有 `meta[itemprop="name"]`，同理可用。）

### 配套脚本

- `scripts/zhihu_page_info.js <url> <outName>` — 打开任意知乎页，打印 title / h1 / 作者列表 / 正文，并落盘 `.txt` + `.html`
- `scripts/zhihu_question_dump.js <问题URL...>` — 问题页批量列出所有答主 + 长度，自动高亮 `^max` 及含 PowerShell 的回答
- `scripts/zhihu_column_items.js <c_id...>` — 免登录拉取专栏文章清单（含作者）
- `scripts/zhihu_suggest_probe.js` — 探测搜索联想词（判断话题是否存在、发现真实搜索词）

## Key Learnings（微信公众号抓取要点）

1. **type='weixin'**：URL 形如 `https://mp.weixin.qq.com/s/xxx`。正文容器用 `#js_content`（`.rich_media_content` 是外层稳定容器，内部 `#js_content` 才是实际内容）。
2. **图片懒加载**：微信正文 `<img>` 的 `src` 常为空，真实地址在 **`data-src`** 属性（`mmbiz.qpic.cn` 域名）。提取顺序：`data-src` → `data-original` → `data-actualsrc` → `src`（与知乎共用 `pickImgSrc`，注意该函数必须定义在 EXTRACT_FN 内部，evaluate 序列化不携带外部引用）。
3. **图片防盗链**：下载需带 `Referer`（文章 URL 即可），与知乎同一套 `context.request.get` 机制。
4. **标题**：取 `<h1 id="activity-name">` 或 `document.title`（格式 "文章标题"），由调用方填入 articles.json 的 `title` 字段。
5. **链接卡片不适用**：微信正文无 LinkCard 懒加载，`fillLinkCardTitles` 直接跳过（无 `__LINKCARD__` 占位符）。
6. **来源标注**：siyuan_pipeline.py 按域名区分来源行——`mp.weixin.qq.com` → `> 来源：[微信公众号](url)`，其余 → `> 来源：[知乎](url)`。
7. **已验证**：2026-08-23 实测微信文章抓取（18 文本块+3 图）、写入、verify 100% 命中、fix-assets 3/3 就位。

## 通用 Discuz 论坛剪藏（2026-10-06 实战，aardio 论坛）

用户可能剪藏任意 Discuz 论坛帖子（如 bbs.aardio.com）。**无反爬，curl 直抓 HTML 即可**，不需要 Playwright。

### 页面结构与提取要点

1. **楼层容器**：`<div id="post_{pid}">`，按此分割楼层（注意过滤 `rate_{pid}`、`rate_div_{pid}`、`new` 等非数字后缀片段）
2. **正文容器**：`<td class="t_f" id="postmessage_{pid}">...</td>`——注意 id 前有 `class="t_f"`，正则要写成 `<td class="t_f" id="postmessage_%s">`
3. **楼层时间**：`<em id="authorposton{pid}">发表于 2012-10-7 18:16:28</em>`。**勿用 `<span title="...">` 提取时间**——楼层里第一个 `<span title>` 常是无关属性（如"回帖奖励"），会匹配出 "12931" 这类垃圾值
4. **楼层作者**：`class="xw1"` 的链接文本
5. **引用块**：`<div class="quote"><blockquote>...</blockquote></div>` → 转 `> ` 块引用
6. **代码块（blockcode）**：`<div class="blockcode"><div id="code_xxx"><ol><li>行1<li>行2...</ol></div><em onclick="copycode(...)">复制代码</em></div>`。两个坑：a) `<li>` 可不闭合（后一个 `<li>` 直接开新行），正则需 `(?=<li|$)` 兜底；b) `<em>复制代码</em>` 是 UI 按钮要丢弃
7. **正文图片识别**：论坛 UI 图标全在 `static/image/` 与 `uc_server/avatar`，正文真实图片才会用外链/附件 URL——过滤这两个前缀即可
8. **HTML 实体**：`&nbsp;` → 空格（含 `\u00a0`），`&quot;` 等常规 unescape
9. **多页帖子**：单页抓不全时按 `page=N` 参数循环抓取合并楼层

### 入库

转换成 Markdown 后走 `createDocWithMd`（hpath 含标题）+ renameDoc + setBlockAttrs 两步标题修复，来源行标 `> 来源：[论坛名 - 标题](url)`。验证用 `exportMdContent` 导出比对长度 + 关键词抽查（"复制代码"/内部 div id 残留 = 转换缺陷）。

### 笔记本陷阱

思源中可能存在**两个同名笔记本**（如两个"我的笔记本"），lsNotebooks 返回顺序不代表主次——归档目标笔记本用 id 精确指定（剪藏归档用 20260812074723-ubt46f7）。

## 批量模式：剪藏失败笔记重建（zhihu_reclip.py）

当思源中存在**大量剪藏失败的知乎笔记**（典型来自印象笔记迁移：标题为 URL / "无标题笔记"、正文只含"剪藏失败"和原文链接）时，不要逐条走链路 B，用批量工具：

```bash
cd scripts
python zhihu_reclip.py scan      # 扫描识别失败笔记（四重策略防漏）
python zhihu_reclip.py delete    # 删除旧失败文档（process 模式用）
python zhihu_reclip.py fetch     # 自动生成 articles.json 并调用 zhihu_extract.js 批量抓取
python zhihu_reclip.py process   # 创建新文档（垃圾过滤+图片内联+fix-assets）
python zhihu_reclip.py rebuild   # 逐条在原文档上重建内容（不删除原 doc，保留原路径+doc_id）
python zhihu_reclip.py verify    # 验证标题+内容命中率
python zhihu_reclip.py report    # 最终报告（含可点击 deep link）
python zhihu_reclip.py status    # 随时查看进度（只读）
python zhihu_reclip.py reset <hash>  # 重置单条状态重试
```

**两种重建模式**：
- `delete → fetch → process`：删除旧文档 → 在根目录新建文档（doc_id 变化，原路径丢失）
- `fetch → rebuild`：保留原文档 → 在原位重建内容（删子块+insertBlock+标题修复+fix-assets，doc_id 和路径不变）。**逐条处理、每条存盘**，中断后重跑即恢复

要点（千条规模实战设计）：
- **状态机 + 断点续传**：`pending → deleted → fetched → created/rebuilt → verified → done`，任何时刻中断重跑即恢复
- **运行数据与代码分离**：状态/缓存/日志写 `D:\zhihu-reclip-data`（环境变量 `RECLIP_DATA_DIR` 覆盖），不污染 skill 目录
- **扫描四重策略取并集**：思源 SQL 有索引 bug（`type='d'` 批量查询随机漏文档），必须加**文件树 API 递归遍历**兜底
- **notebook_id 每次校验刷新**：状态里存的笔记本可能已删除/重建
- **rebuild 逐条存盘**：每条 rebuild 后立即 save_state，中断不丢已完成进度
- API 限流 0.15s、指数退避重试、原子写入（临时文件+rename）

## 答主合集模式：递归发现 + 批量入库（recursive_crawler.js + author_pipeline.py）

当需要把**某个答主的全部回答**归档（常见于：答主开启隐私保护/改匿名，主页 `/answers` 列表为空、`/api/v4/members/<token>/answers` 返回 0 条，无法直接枚举）时，用**递归链接发现**替代直列：从少量已知回答出发，沿正文里的知乎链接扩散，逐页校验作者，直到覆盖饱和。

```bash
# 1. 递归发现（从已知回答 URL 出发；INITIAL_URLS 需手改脚本头部）
node recursive_crawler.js                 # 产出 crawl_state.json（断点续爬）
# 2. 生成待抓清单（自动跳过已完成篇目；保留已回写 docId 防重复建文档）
python author_pipeline.py gen-articles --state zhihu_author_extract/crawl_state.json --articles zhihu_author_extract/articles.json
# 3. 提取正文+图片（复用链路 B 的 zhihu_extract.js，含 LinkCard 补标题）
node zhihu_extract.js zhihu_author_extract/articles.json zhihu_author_extract
# 4. 写入思源（主文档=答主档案+目录，子文档=每篇回答）
python author_pipeline.py create --notebook <笔记本ID> --articles zhihu_author_extract/articles.json --extract-dir zhihu_author_extract --author "答主名" --author-token <token> --author-about "<简介>"
# 5. 验证 + deep link
python author_pipeline.py verify --articles zhihu_author_extract/articles.json --extract-dir zhihu_author_extract
```

**核心机制**：
- **严格作者匹配**：只检查目标 `.AnswerItem` 内部 `.AuthorInfo a[href*="/people/"]` 是否含目标 token——绝不能全页扫描 `/people/` 链接（其他答主正文引用目标答主时会出现指向其主页的链接，导致误判）
- **verified 标记**：discovered 条目确认后打 `verified:true`，续跑时只重验未验证条目，避免每次重启全量重访
- **reverify 清除旧误判**：旧版本误收的条目，重验发现不是目标答主即从 discovered 删除
- **三型页面分路处理**：回答页（严格校验+提取链接）/ 问题页（扫首屏 AnswerItem 找目标答主 + 提取全页链接）/ 专栏页（作者校验走 `.AuthorInfo`、`follow-author`、`js-initialData` 兜底）

**反爬与长跑铁律**（2026-08-16 实战，全部踩过坑）：
1. **同一时刻只允许一个知乎客户端**！爬虫与提取并行 → 两个 Chrome 同时打站 → 403 风控风暴互相拖垮，双双卡死。必须串行：提取完成后才续跑爬虫
2. **长跑任务分段续跑**：本环境后台任务约 20-40 分钟被回收一次。把大任务拆成可断点续传的小段——每轮 `gen-articles` 自动跳过已有 blocks 的篇目（pending.json），被清理后重启即续，进度不丢
3. **日志用文件重定向，别依赖 stdout 管道**：`>> extract.log 2>&1`。后台任务 stdout 管道被清理后 console.log 抛 EPIPE，若异常处理器又调 console.log 会无限递归（实测 crawl.log 膨胀到 803MB）
4. **单客户端下 403 自动缓解**：提取脚本内置降级页等待自愈；爬虫 goto 三连败回队重试（RETRY_MAX=3），超过放弃不卡死
5. **递归发现的覆盖上限**：只能找到"被其他页面链接到"的回答，孤立回答不可达（答主 1283 篇实测仅发现 84 篇，且高度集中于少数父回答）。如需要更高覆盖，可补充知乎站内搜索 API（页面内 fetch 带 Cookie）等渠道

**author_pipeline.py 要点**：
- `gen-articles` 按 URL 排序生成稳定 idx；**保留已有 docId**——重复运行 create 不会重建已建文档
- create 复用链路 B 全部坑位经验：主文档幂等（SQL 查标题）、标题两步修复（renameDoc + setBlockAttrs）、子文档 path 前缀 `/{父id}.sy/`、fix-assets 资产归位并回读校验
- verify 命中率 ≥90% 即合格；略低只因正文含超链接锚点语法，属预期

## Scripts & Docs

- `scripts/zhihu_extract.js` — Playwright 抓取（blocks 结构 + 图片下载），用法 `node zhihu_extract.js <articles.json> [outdir]`
- `scripts/siyuan_pipeline.py` — 思源写入流水线，子命令 create / rebuild / fix-assets / verify
- `scripts/zhihu_reclip.py` — 批量模式：剪藏失败笔记扫描/重建/验证（千条规模，断点续传），运行数据写 `D:\zhihu-reclip-data`
- `scripts/recursive_crawler.js` — 答主合集模式：递归链接发现（断点续爬 + verified 标记 + 崩溃兜底）
- `scripts/author_pipeline.py` — 答主合集模式：gen-articles / create / verify（主文档+子文档批量入库）
- `scripts/zhihu_page_info.js` — 单页信息探测（作者/正文落盘），反向检索用
- `scripts/zhihu_question_dump.js` — 问题页批量列答主，反向检索用
- `scripts/zhihu_column_items.js` — 免登录拉专栏文章清单
- `scripts/zhihu_suggest_probe.js` — 知乎搜索联想词探测
- `scripts/parse_discuz.py` 参考 — 通用 Discuz 论坛 HTML→Markdown 转换（2026-10-06 aardio 实战版位于各会话工作区，含楼层分割/blockcode/引用块处理，未收入 scripts/，按 SKILL.md「通用 Discuz 论坛剪藏」章节结构复写即可）
- `examples/articles.example.json` — 输入配置格式示例
- `docs/siyuan-api-pitfalls.md` — 思源 API 坑位详解
- `docs/zhihu-anti-scraping.md` — 知乎反爬与抓取要点
- `README.md` — GitHub 项目风格总览（快速开始 / CLI 参考 / 目录结构）
