// 知乎内容抓取脚本 v3（Playwright + 本机真实 Chrome 无头模式）
//
// 用法:
//   node zhihu_extract.js <articles.json> [outdir]
//   - articles.json: [{"idx":"01","type":"answer|pin|article","url":"...","id":"可选，默认从URL推导"}]
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

// 从 URL 推导内容 ID（用于 data-zop 锚定）
function deriveId(art) {
  if (art.id) return String(art.id);
  const m = art.url.match(/\/(?:answer|pin|p)\/(\d+)/);
  return m ? m[1] : '';
}

const EXTRACT_FN = (payload) => {
  const { type, id } = payload;
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
  }
  if (!container) return null;

  const imgId = (src) => { const m = src.match(/v2-([0-9a-f]+)/); return m ? m[1] : src; };
  const seen = new Set();
  const isContentImg = (img) => {
    const src = img.getAttribute('data-original') || img.getAttribute('data-actualsrc') || img.getAttribute('src') || '';
    if (!src.includes('zhimg.com/v2-')) return false;
    const key = imgId(src);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  };
  const blocks = [];
  const walk = (node) => {
    for (const child of node.children) {
      const tag = child.tagName.toLowerCase();
      if (tag === 'img') {
        if (isContentImg(child)) {
          const src = child.getAttribute('data-original') || child.getAttribute('data-actualsrc') || child.getAttribute('src') || '';
          blocks.push({ type: 'img', src });
        }
        continue;
      }
      if (tag === 'noscript' || tag === 'svg' || tag === 'button' || tag === 'style') continue;
      if (child.querySelector && child.querySelector('img')) { walk(child); continue; }
      const text = child.innerText ? child.innerText.trim() : '';
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

(async () => {
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

  const results = [];
  for (const art of ARTICLES) {
    const id = deriveId(art);
    console.log(`[${art.idx}] ${art.url}`);
    let blocks = null;
    let matched = null;
    try {
      await page.goto(art.url, { waitUntil: 'domcontentloaded', timeout: 45000 });
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
      }
    } catch (e) {
      console.log(`  ERROR: ${e.message.split('\n')[0]}`);
    }

    const imgs = blocks ? blocks.filter((b) => b.type === 'img') : [];
    console.log(`  blocks=${blocks ? blocks.length : 0}, images=${imgs.length}${art.type === 'answer' ? `, 首答即目标: ${matched}` : ''}`);

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
    await page.waitForTimeout(800 + Math.random() * 700); // 轻微间隔防频控
  }

  fs.writeFileSync(path.join(OUT_DIR, 'summary.json'), JSON.stringify(results.map((r) => ({
    idx: r.idx, ok: r.ok, firstIsTarget: r.firstIsTarget, blocks: r.blocks.length,
    images: r.blocks.filter((b) => b.type === 'img' && b.file).length,
  })), null, 2), 'utf8');
  await browser.close();
  console.log('DONE');
})();
