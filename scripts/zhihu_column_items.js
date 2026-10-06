const { chromium } = require('playwright');

const columns = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox'],
  });
  const ctx = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    locale: 'zh-CN',
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });
  const page = await ctx.newPage();
  await page.goto('https://www.zhihu.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(1500);

  for (const c of columns) {
    const r = await page.evaluate(async (cid) => {
      const out = [];
      let offset = 0;
      for (let i = 0; i < 6; i++) {
        const res = await fetch(
          `/api/v4/columns/${cid}/items?limit=100&offset=${offset}`,
          { credentials: 'include' }
        );
        if (!res.ok) return { err: 'HTTP ' + res.status, out };
        const j = await res.json();
        (j.data || []).forEach((it) =>
          out.push({
            title: it.title,
            url: it.url,
            author: it.author?.name || '',
            authorUrl: it.author?.url_token || '',
          })
        );
        if (j.paging?.is_end) break;
        offset += 100;
      }
      return { err: null, out };
    }, c);
    console.log(`\n=== column ${c} (${r.out.length} items) err=${r.err} ===`);
    r.out.forEach((x) => console.log(` - [${x.author}] ${x.title}`));
  }
  await browser.close();
})();
