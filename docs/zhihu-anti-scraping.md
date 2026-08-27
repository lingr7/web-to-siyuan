# 知乎反爬与抓取要点

以下经验来自实战（2026-08），修复方案已固化在 `scripts/zhihu_extract.js` 中。

## 1. 常规途径全部失效

- 直接 `curl` 知乎页面 → **403**
- 知乎 API v4（`/api/v4/answers/...`）→ 403 或降级
- WebFetch / LLM 抓取 → 页面能到但 `<img>` 标签全部丢弃，且长回答常只抓到前半段、多回答页面会抓错回答

## 2. Playwright 自带 Chromium 无头被识别

跳转到"安全验证"页。**解法**（三者缺一不可）：

```javascript
const browser = await chromium.launch({
  channel: 'chrome',          // ① 用本机真实 Chrome（新无头内核）
  headless: true,
  args: ['--disable-blink-features=AutomationControlled'],  // ②
});
await context.addInitScript(() => {                          // ③ 抹掉自动化特征
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  window.chrome = window.chrome || { runtime: {} };
  Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
  Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
});
```

本机装有 Edge 时把 `channel` 改为 `'msedge'` 亦可。

## 3. 严禁全页滚动加载

知乎虚拟列表会把目标回答**移出 DOM**，导致抓到推荐流的其他回答（真实踩坑：多篇文档内容互相错乱）。

图片 URL 就在 `data-original` / `data-actualsrc` 属性里，**无需滚动加载图片**。正确做法：`domcontentloaded` + 固定等待 + 点击"阅读全文"即可。

## 4. 回答精确锚定

多回答页面中，排序可能变化，不能默认取第一个 `.AnswerItem`。`.AnswerItem` 的 `data-zop` 属性是 JSON，其 `itemId` 等于 answerId 的才是目标回答。

通过 `/answer/` 直达 URL 访问时，锚定后目标就是第一个；脚本同时记录 `firstIsTarget` 供校验。

## 5. 想法（pin）页结构

容器是 `.PinItem .RichContent-inner`（不存在 `.PinItem-content`）。

## 6. 图片下载方式

页面内 `fetch` 会被 CORS 拦截，直接 `request` 拿不到防盗链资源。**必须**：

```javascript
const resp = await context.request.get(url, { headers: { Referer: pageUrl } });
```

走浏览器上下文（自带 Cookie）+ 带 `Referer` 绕防盗链。

## 7. 登录弹窗遮挡

无头访问常弹登录框，遮挡"阅读全文"按钮。先移除再操作：

```javascript
document.querySelectorAll('.signFlowModal, .Modal-wrapper, .modal-wrapper').forEach(el => el.remove());
```

## 8. 纯图片回答存在

有的回答正文只有图片没有文字（blocks 里只有 img 块），这是正常情况，不是抓取失败。`verify` 时文本块为 0 属预期。

## 9. 风控降级页的二次事故：undefined_blocks.json（2026-08-27）

403 降级页不仅导致抓取慢/失败，恢复后正文可能整篇被折叠成**单块**，且 `data-zop` 解析失败 → itemId 丢失 → 产物落盘为 `undefined_blocks.json`（正常命名是 `{idx}_blocks.json`）。

**抢救流程**（277 块实战验证）：
1. 清理 zhida 卡片链接等噪音内容
2. 按句末标点+空格切分，恢复块结构
3. 重命名为 `{idx}_blocks.json`，走正常 pipeline 入库

**预防**：articles.json 每条必须带 `idx`；抓取完成后检查产物文件名，出现 `undefined_*` 即判定降级页事故，不要直接入库。

## 10. WebFetch 内容与目标回答不符（2026-08-26 验证）

多回答页面上 WebFetch 给出的预览内容可能混入其他答主的回答。Playwright 按 itemId 锚定（见第 4 节）抓到的才是目标回答。怀疑漏抓/错抓时，写 debug 脚本 dump 全量块与原文逐段比对，不要只看文件是否存在。
