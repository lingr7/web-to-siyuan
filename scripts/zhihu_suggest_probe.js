const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox'],
  });
  const context = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    locale: 'zh-CN',
  });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
  });
  const page = await context.newPage();
  await page.goto('https://www.zhihu.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(2000);

  const prefixes = [
    'hermes',
    'hermes 桌面',
    'hermes 中文',
    'hermes 中文社区',
    'hermes 桌面版',
    'hermes power',
    'hermes powershell',
    'hermes 适配',
    'hermes Max',
    'hermes 安装',
  ];

  for (const p of prefixes) {
    const r = await page.evaluate(async (q) => {
      try {
        const res = await fetch('/api/v4/search/suggest?q=' + encodeURIComponent(q), {
          credentials: 'include',
        });
        const j = await res.json();
        return (j.suggest || []).map((s) => s.query);
      } catch (e) {
        return ['ERR ' + e.message];
      }
    }, p);
    console.log(`[${p}] -> ${JSON.stringify(r, null, 0)}`);
  }

  await browser.close();
})();
