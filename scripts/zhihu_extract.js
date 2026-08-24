// 网页内容抓取脚本 v3（Playwright + 本机真实 Chrome 无头模式）
//
// 用法:
//   node zhihu_extract.js <articles.json> [outdir]
//   - articles.json: [{"idx":"01","type":"answer|pin|article|weixin","url":"...","id":"可选，默认从URL推导"}]
//   - type: answer=知乎回答, pin=知乎想法, article=知乎专栏, weixin=微信公众号文章(mp.weixin.qq.com)
//   - outdir 默认 ./zhihu_extract
//
// 运行前环境变量（Windows 示例）:
//   PLAYWRIGHT_BROWSERS_PATH=<node工作区>/pw-browsers
//   NODE_PATH=<node工作区>/node_modules
//
// 关键设计（都是踩坑验证过的，勿改）:
// 1. 使用 channel:'chrome' 调用本机真实 Chrome（新无头内核），而非 Playwright 自带 Chromium
//    —— 自带 Chromium 无头会被知乎识别跳转"安全验证"页
// 2. 不做全页滚动 —— 知乎虚拟列表会把目标回答移出 DOM，导致抓到推荐流的其他回答
//    图片 URL 就在 data-original/data-actualsrc 属性里，无需真正加载
// 3. 回答页用 data-zop.itemId 精确锚定目标回答，避免排序变化抓错
// 4. 图片用 context.request.get 下载（带浏览器 Cookie、不受 CORS 限制），必须带 Referer 绕过防盗链
// 5. 微信公众号文章（weixin）：
//    - 正文容器 #js_content（懒加载图片在 data-src 属性，src 常为空）
//    - 图片域 mmbiz.qpic.cn，需带 Referer 下载（mp.weixin.qq.com 或文章 URL 均可）
//    - 链接卡片懒加载不适用（无 LinkCard），标题由调用方从 #activity-name 取
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const articlesFile = process.argv[2];
if (!articlesFile) {
  console.error('用法: node zhihu_extract.js <articles.json> [outdir]');
  process.exit(1);
}
const ARTICLES = JSON.parse(fs.readFileSync(articlesFile, 'utf8'));

const OUT_DIR = process.argv[3] ? path.resolve(process.argv[3]) : path.join(process.cwd(), 'zhihu_extract');
const IMG_DIR = path.join(OUT_DIR, 'imgs');
fs.mkdirSync(IMG_DIR, { recursive: true });

// 从 URL 推导内容 ID（用于 data-zop 锚定；weixin/无 id 返回空）
function deriveId(art) {
  if (art.id) return String(art.id);
  const m = art.url.match(/\/(?:answer|pin|p)\/(\d+)/);
  return m ? m[1] : '';
}

// 取图片真实 URL：知乎 data-original/data-actualsrc，微信 data-src（懒加载 src 为空）
// 注意：必须定义在 EXTRACT_FN 内部（evaluate 序列化只带函数体，外部引用会 ReferenceError）
const EXTRACT_FN = (payload) => {
  const { type, id } = payload;
  const pickImgSrc = (img) =>
    img.getAttribute('data-src') || img.getAttribute('data-original') ||
    img.getAttribute('data-actualsrc') || img.getAttribute('src') || '';
  let container = null;
  if (type === 'answer') {
    const items = Array.from(document.querySelectorAll('.AnswerItem'));
    let target = items.find((it) => {
      try { return JSON.parse(it.getAttribute('data-zop') || '{}').itemId === id; } catch (e) { return false; }
    });
    if (!target && items.length) target = items[0]; // /answer/ URL 锚定后目标就是第一个
    container = target ? target.querySelector('.RichText') : null;
  } else if (type === 'pin') {
    // 想法页结构：.PinItem .RichContent-inner（不是 .PinItem-content）
    const items = Array.from(document.querySelectorAll('.PinItem'));
    container = items.length ? items[0].querySelector('.RichContent-inner') || items[0] : null;
  } else if (type === 'article') {
    container = document.querySelector('.Post-RichTextContainer');
  } else if (type === 'weixin') {
    // 微信公众号文章正文：.rich_media_content 是稳定容器，#js_content 是内部实际内容
    container = document.querySelector('#js_content') || document.querySelector('.rich_media_content');
  }
  if (!container) return null;

  const imgId = (src) => { const m = src.match(/v2-([0-9a-f]+)/); return m ? m[1] : src; };
  const seen = new Set();
  const isContentImg = (img) => {
    const src = pickImgSrc(img);
    if (!src.includes('zhimg.com/v2-') && !src.includes('mmbiz.qpic.cn')) return false;
    const key = imgId(src);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  };
  // 行内序列化：保留 <a> 超链接为 markdown 语法（知乎外链走 link.zhihu.com/?target= 跳转，解开为真实 URL）
  const mdInline = (node) => {
    let out = '';
    for (const n of node.childNodes) {
      if (n.nodeType === Node.TEXT_NODE) { out += n.textContent; continue; }
      if (n.nodeType !== Node.ELEMENT_NODE) continue;
      const t = n.tagName.toLowerCase();
      if (t === 'br') { out += '\n'; continue; }
      if (t === 'img' || t === 'noscript' || t === 'svg' || t === 'button' || t === 'style') continue;
      if (t === 'a') {
        let href = n.getAttribute('href') || '';
        const tm = href.match(/[?&]target=([^&]+)/);
        if (tm) { try { href = decodeURIComponent(tm[1]); } catch (e) { } }
        let label = mdInline(n).trim();
        // 知乎链接卡片（LinkCard）懒加载：标题可能尚未渲染，从卡片节点兜底取
        if (!label) {
          const cardTitle = n.querySelector('.LinkCard-title');
          if (cardTitle) label = (cardTitle.textContent || '').trim();
        }
        if (href && label && href !== '#' && !href.startsWith('javascript')) out += `[${label}](${href})`;
        else if (href && href !== '#' && !href.startsWith('javascript')) out += `[__LINKCARD__](${href})`;
        else out += label;
        continue;
      }
      out += mdInline(n);
    }
    return out;
  };
  const blocks = [];
  const walk = (node) => {
    for (const child of node.children) {
      const tag = child.tagName.toLowerCase();
      if (tag === 'img') {
        if (isContentImg(child)) {
          blocks.push({ type: 'img', src: pickImgSrc(child) });
        }
        continue;
      }
      if (tag === 'noscript' || tag === 'svg' || tag === 'button' || tag === 'style') continue;
      if (child.querySelector && child.querySelector('img')) { walk(child); continue; }
      const text = mdInline(child).trim();
      if (text) blocks.push({ type: 'text', text });
    }
  };
  walk(container);
  return blocks;
};

async function expandContent(page) {
  // 移除登录弹窗
  await page.evaluate(() => {
    document.querySelectorAll('.signFlowModal, .Modal-wrapper, .modal-wrapper').forEach((el) => el.remove());
  });
  // 点击"阅读全文"（JS 直点，最多 5 次）
  for (let i = 0; i < 5; i++) {
    const clicked = await page.evaluate(() => {
      const btns = Array.from(document.querySelectorAll('button, .ContentItem-expandButton'));
      const target = btns.find((b) => b.innerText && b.innerText.includes('阅读全文'));
      if (target) { target.click(); return true; }
      return false;
    });
    if (!clicked) break;
    await page.waitForTimeout(900);
  }
  // 等待链接卡片（LinkCard）标题懒加载完成，最多 15 秒
  await page.waitForFunction(
    () => !document.querySelector('.LinkCard-title.loading'),
    { timeout: 15000 }
  ).catch(() => {});
}

function mergeTextBlocks(blocks) {
  const out = [];
  for (const b of blocks) {
    if (b.type === 'text' && out.length && out[out.length - 1].text === b.text) continue;
    if (b.type === 'text' && !b.text.trim()) continue;
    out.push(b);
  }
  return out;
}

// 补全链接卡片标题：卡片懒加载依赖可视区（不滚动拿不到），改调知乎编辑器元数据 API
//   GET /api/v4/editor/link_card_infos?scene=pcweb&urls=<逗号连接的URL>
//   返回 { <url>: { extra_info: "{\"title\": ...}" } }
async function fillLinkCardTitles(context, blocks, srcUrl) {
  const urls = new Set();
  for (const b of blocks) {
    if (b.type !== 'text') continue;
    for (const m of b.text.matchAll(/\[__LINKCARD__\]\(([^)]+)\)/g)) urls.add(m[1]);
  }
  if (!urls.size) return;
  const list = Array.from(urls);
  for (let i = 0; i < list.length; i += 10) {
    const batch = list.slice(i, i + 10);
    try {
      const ep = 'https://www.zhihu.com/api/v4/editor/link_card_infos?scene=pcweb&urls='
        + encodeURIComponent(batch.join(','));
      const resp = await context.request.get(ep, { headers: { Referer: srcUrl } });
      if (!resp.ok()) continue;
      const data = await resp.json();
      const titles = {};
      for (const [u, info] of Object.entries(data || {})) {
        try {
          const extra = JSON.parse(info.extra_info || '{}');
          if (extra.title) titles[u] = extra.title;
        } catch (e) { }
      }
      for (const b of blocks) {
        if (b.type !== 'text') continue;
        b.text = b.text.replace(/\[__LINKCARD__\]\(([^)]+)\)/g, (s, u) => {
          const t = titles[u];
          return t ? `[${t.replace(/[\[\]]/g, '')}](${u})` : `[链接](${u})`;
        });
      }
    } catch (e) {
      console.log(`  卡片标题获取失败: ${e.message.split('\n')[0]}`);
    }
  }
}

// 启动浏览器（launch + context + 反检测），浏览器崩溃后靠它重启
async function launchBrowser() {
  const browser = await chromium.launch({
    channel: 'chrome',          // 关键：用本机真实 Chrome
    headless: true,             // 新无头模式，指纹与真浏览器几乎一致
    args: ['--disable-blink-features=AutomationControlled'],
  });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  });
  // 反检测脚本：抹掉 navigator.webdriver 等无头特征
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = window.chrome || { runtime: {} };
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  });
  const page = await context.newPage();
  return { browser, context, page };
}

(async () => {
  let { browser, context, page } = await launchBrowser();
  const results = [];

  for (const art of ARTICLES) {
    const id = deriveId(art);
    console.log(`[${art.idx}] ${art.url}`);
    let blocks = null;
    let matched = null;
    let deleted = false;
    let httpStatus = -1;

    // 单条最多重试 3 次：知乎批量访问偶发 403 降级页（正文不渲染），重开新页重试可恢复
    for (let attempt = 1; attempt <= 3 && blocks === null && !deleted; attempt++) {
      try {
        if (attempt > 1) {
          try { await page.close(); } catch (e) {}
          page = await context.newPage();
          console.log(`  重试 #${attempt - 1}`);
        }
        const resp = await page.goto(art.url, { waitUntil: 'domcontentloaded', timeout: 45000 }).catch((e) => null);
        httpStatus = resp ? resp.status() : -1;
        if (httpStatus === 403) console.log(`  HTTP 403（知乎风控降级页），等待正文渲染…`);
        // 条件等待：目标容器出现（最长 25 秒），403 降级页正文渲染很慢
        await page.waitForFunction(({ type }) => {
          if (type === 'answer') return document.querySelectorAll('.AnswerItem').length > 0;
          if (type === 'pin') return document.querySelectorAll('.PinItem').length > 0;
          if (type === 'article') return !!document.querySelector('.Post-RichTextContainer');
          if (type === 'weixin') return !!document.querySelector('#js_content');
          return !!(document.body && document.body.innerText && document.body.innerText.length > 0);
        }, { type: art.type }, { timeout: 25000 }).catch(() => {});
        await page.waitForTimeout(2500);
        await expandContent(page);
        await page.waitForTimeout(1200);
        // 不做全页滚动（虚拟列表会移除目标节点）
        blocks = await page.evaluate(EXTRACT_FN, { type: art.type, id });
        if (art.type === 'answer') {
          matched = await page.evaluate((qid) => {
            const first = document.querySelector('.AnswerItem');
            if (!first) return null;
            try { return JSON.parse(first.getAttribute('data-zop') || '{}').itemId === qid; } catch (e) { return null; }
          }, id);
          // 回答被作者删除时页面会提示，直接判定失败不重试
          if (blocks === null) {
            deleted = await page.evaluate(() => {
              const t = document.title + (document.body ? document.body.innerText.slice(0, 500) : '');
              return t.includes('已被作者删除');
            });
          }
        }
      } catch (e) {
        console.log(`  ERROR: ${e.message.split('\n')[0]} (attempt ${attempt})`);
        // 页面/浏览器可能崩溃：先关页面，必要时重启整个浏览器
        try { await page.close(); } catch (e2) {}
        if (attempt >= 3) {
          try { await browser.close(); } catch (e3) {}
          const fresh = await launchBrowser();
          browser = fresh.browser; context = fresh.context; page = fresh.page;
          console.log('  浏览器已重启');
        } else {
          try { page = await context.newPage(); } catch (e4) {
            const fresh = await launchBrowser();
            browser = fresh.browser; context = fresh.context; page = fresh.page;
          }
        }
        await page.waitForTimeout(2000).catch(() => {});
      }
    }

    if (deleted) console.log('  回答已被作者删除');
    const imgs = blocks ? blocks.filter((b) => b.type === 'img') : [];
    console.log(`  blocks=${blocks ? blocks.length : 0}, images=${imgs.length}${art.type === 'answer' ? `, 首答即目标: ${matched}` : ''}`);

    // 整条落盘链兜底 try-catch：浏览器状态异常（如 context 已关闭）不得杀死进程
    try {
      if (blocks) await fillLinkCardTitles(context, blocks, art.url);
      const savedFiles = [];
      for (let i = 0; i < imgs.length; i++) {
        const url = imgs[i].src;
        try {
          const resp = await context.request.get(url, { headers: { Referer: art.url } });
          if (resp.ok()) {
            const buf = await resp.body();
            const ct = resp.headers()['content-type'] || '';
            let ext = 'jpg';
            if (ct.includes('png')) ext = 'png';
            else if (ct.includes('gif')) ext = 'gif';
            else if (ct.includes('webp')) ext = 'webp';
            const fname = `${art.idx}_${String(i + 1).padStart(2, '0')}.${ext}`;
            fs.writeFileSync(path.join(IMG_DIR, fname), buf);
            savedFiles.push(fname);
            console.log(`    下载 ${fname} (${buf.length} bytes)`);
          } else {
            savedFiles.push(null);
            console.log(`    失败 HTTP ${resp.status()}`);
          }
        } catch (e) {
          savedFiles.push(null);
          console.log(`    异常: ${e.message.split('\n')[0]}`);
        }
      }
      let imgPos = -1;
      if (blocks) {
        for (const b of blocks) {
          if (b.type === 'img') { imgPos++; b.file = imgPos < savedFiles.length ? savedFiles[imgPos] : null; }
        }
      }
      const rec = { idx: art.idx, type: art.type, url: art.url, ok: !!blocks, firstIsTarget: matched, blocks: blocks ? mergeTextBlocks(blocks) : [] };
      results.push(rec);
      fs.writeFileSync(path.join(OUT_DIR, `${art.idx}_blocks.json`), JSON.stringify(rec, null, 2), 'utf8');
      // 增量写 summary.json：单个处理完就落盘，进程崩溃也不丢已完成结果
      fs.writeFileSync(path.join(OUT_DIR, 'summary.json'), JSON.stringify(results.map((r) => ({
        idx: r.idx, ok: r.ok, firstIsTarget: r.firstIsTarget, blocks: r.blocks.length,
        images: r.blocks.filter((b) => b.type === 'img' && b.file).length,
      })), null, 2), 'utf8');
    } catch (e) {
      console.log(`  落盘异常: ${e.message.split('\n')[0]}`);
      // 上下文已坏：不写文件，但记录结果避免静默丢失
      results.push({ idx: art.idx, type: art.type, url: art.url, ok: !!blocks, firstIsTarget: matched, blocks: [] });
    }

    // 定期重启浏览器释放内存（知乎页面渲染几十个后 Chrome 内存暴涨）
    if ((results.length + 1) % 15 === 0) {
      try { await browser.close(); } catch (e) {}
      const fresh = await launchBrowser();
      browser = fresh.browser; context = fresh.context; page = fresh.page;
      console.log(`  [维护] 已处理 ${results.length} 条，重启浏览器释放内存`);
    }
    await page.waitForTimeout(1500 + Math.random() * 1200).catch(() => {}); // 间隔拉长防频控
  }

  await browser.close();
  console.log('DONE');
})();
