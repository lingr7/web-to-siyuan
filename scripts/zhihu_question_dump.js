const { chromium } = require('playwright');
const fs = require('fs');

const urls = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox'],
  });
  const ctx = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    viewport: { width: 1440, height: 1200 },
    locale: 'zh-CN',
  });
  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });
  const page = await ctx.newPage();
  const report = [];

  for (const url of urls) {
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await page.waitForTimeout(3500);
      await page.evaluate(() => {
        document.querySelectorAll('.signFlowModal, .Modal-wrapper, .Modal-backdrop').forEach((n) => n.remove());
      });
      const qTitle = await page.evaluate(() => document.querySelector('h1')?.innerText?.trim() || document.title);

      for (let i = 0; i < 4; i++) {
        await page.mouse.wheel(0, 1500);
        await page.waitForTimeout(1000);
      }

      const data = await page.evaluate(() => {
        const seen = new Set();
        const out = [];
        document.querySelectorAll('.AnswerItem, .List-item').forEach((n) => {
          const authorEl = n.querySelector('.AuthorInfo[itemprop="author"] meta[itemprop="name"]');
          const name = authorEl ? authorEl.getAttribute('content') : '';
          let zop = null;
          try {
            zop = JSON.parse(n.getAttribute('data-zop') || 'null');
          } catch (e) {}
          const content = n.querySelector('.RichContent-inner')?.innerText || '';
          const key = (zop ? zop.itemId : '') + '|' + name + '|' + content.slice(0, 40);
          if (seen.has(key)) return;
          seen.add(key);
          if (name || content) {
            out.push({ name, itemId: zop ? zop.itemId : '', contentLen: content.length, text: content.slice(0, 1200) });
          }
        });
        return out;
      });

      console.log(`\n########## ${url}\nQ: ${qTitle} | answers: ${data.length}`);
      data.forEach((x, i) => {
        const flag = /^max/i.test(x.name) ? '  <<< MAX' : '';
        console.log(`  [${i}] ${x.name} (len=${x.contentLen})${flag}`);
      });
      const hit = data.filter((x) => /PowerShell|powershell/i.test(x.text));
      hit.forEach((x) => console.log(`  >>> 含 PowerShell: ${x.name} :: ${x.text.replace(/\n/g, ' ').slice(0, 400)}`));
      report.push({ url, qTitle, data });
    } catch (e) {
      console.log(`\n########## ${url}\n  ERROR ${e.message}`);
    }
  }

  fs.writeFileSync('questions_report.json', JSON.stringify(report, null, 2), 'utf8');
  await browser.close();
})();
