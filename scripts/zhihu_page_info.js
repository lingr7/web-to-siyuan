const { chromium } = require('playwright');
const fs = require('fs');

const url = process.argv[2];
const outName = process.argv[3] || 'page_dump';

(async () => {
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox'],
  });
  const context = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
  });
  const page = await context.newPage();
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(4000);
  // 去掉登录弹窗
  await page.evaluate(() => {
    document.querySelectorAll('.signFlowModal, .Modal-wrapper, .Modal-backdrop').forEach((n) => n.remove());
  });

  const info = await page.evaluate(() => {
    const h1 = document.querySelector('h1')?.innerText?.trim() || '';
    const authorEls = Array.from(
      document.querySelectorAll(
        '.AuthorInfo-name, .AuthorInfo .UserLink-link, .Post-Author .AuthorInfo-name, a[href*="/people/"]'
      )
    ).map((e) => (e.innerText || '').trim()).filter(Boolean);
    const content =
      document.querySelector('.Post-RichTextContainer, .RichText.ztext, .AnswerCard .RichContent-inner')?.innerText || '';
    const title = document.title;
    return { title, h1, authors: [...new Set(authorEls)].slice(0, 20), contentLen: content.length, content };
  });

  console.log('URL: ' + url);
  console.log('page title: ' + info.title);
  console.log('h1: ' + info.h1);
  console.log('authors: ' + JSON.stringify(info.authors));
  console.log('contentLen: ' + info.contentLen);
  fs.writeFileSync(outName + '.txt', info.content, 'utf8');
  fs.writeFileSync(outName + '.html', await page.content(), 'utf8');
  console.log('--- content head ---');
  console.log(info.content.slice(0, 1500));
  await browser.close();
})();
