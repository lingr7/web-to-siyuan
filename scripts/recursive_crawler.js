// 递归链接发现爬虫 v2：从已知回答出发，发现罗心澄的全部回答
// v2 修复（review 后）：
// 1. 主循环外层 try/catch 兜底：单页异常不再击杀整个进程
// 2. 日志同步落盘 crawl.log（进程死亡后仍可追因）
// 3. goto 三连败的 URL 回队重试（最多 RETRY_MAX 次，超过则放弃并记录）
// 4. discovered 加 verified 标记：续跑时只重验未验证条目，不再全量 reverify
// 5. 专栏文章也校验作者（AuthorInfo / 文章 meta）
// 6. unhandledRejection / uncaughtException 不再直接崩溃退出

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const AUTHOR_TOKEN = '37e9938e5444bde972ff6db36be440b1';
const AUTHOR_NAME = '罗心澄';
const INITIAL_URLS = [
  'https://www.zhihu.com/question/2069409558779408927/answer/2072103069089797019',
  'https://zhuanlan.zhihu.com/p/24993711109',
  'https://zhuanlan.zhihu.com/p/20156297977',
];

const OUTPUT_DIR = path.resolve('./zhihu_author_extract');
const LOG_PATH = path.join(OUTPUT_DIR, 'crawl.log');
const MAX_VISITS = 2000;
const RETRY_MAX = 3;      // 同一 URL goto 失败最多重试轮数
const SAVE_EVERY = 10;    // 每 N 次访问落盘一次状态

fs.mkdirSync(OUTPUT_DIR, { recursive: true });

// ---------- 日志：只写文件（v2.1 修复 EPIPE 死循环）----------
// 教训：后台任务的 stdout 管道被关闭后 console.log 抛 EPIPE，
// 若兜底 handler 再写日志会无限递归。因此：完全不走 stdout，只写文件。
const logStream = fs.createWriteStream(LOG_PATH, { flags: 'a' });
function log(...args) {
  try {
    const line = args.map(a => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
    logStream.write(new Date().toISOString() + ' ' + line + '\n');
  } catch(e) { /* 日志系统本身坏了就只能静默 */ }
}

// 提取正文和链接的页面函数
const SCRAPE_FN = ({ token: authorToken, name: authorName }) => {
  const answerItems = document.querySelectorAll('.AnswerItem');
  let isTargetAuthor = false;
  let answerId = '';
  let questionId = '';
  let questionTitle = '';
  let authorNameFound = '';

  const urlParts = window.location.pathname.split('/');
  const urlAnswerId = urlParts[urlParts.length - 1] || '';

  let target = null;
  for (const item of answerItems) {
    try {
      const zop = JSON.parse(item.getAttribute('data-zop') || '{}');
      if (String(zop.itemId) === String(urlAnswerId)) { target = item; break; }
    } catch(e) {}
  }
  if (!target && answerItems.length) target = answerItems[0];

  if (target) {
    try {
      const zop = JSON.parse(target.getAttribute('data-zop') || '{}');
      authorNameFound = zop.authorName || '';
      answerId = String(zop.itemId) || urlAnswerId;
      questionTitle = zop.title || '';
    } catch(e) {}
    // 严格匹配：只检查目标回答 item 内部 AuthorInfo 区的作者链接
    const authorLink = target.querySelector('.AuthorInfo a[href*="/people/"]');
    if (authorLink) {
      const href = authorLink.getAttribute('href') || '';
      if (href.includes(authorToken)) isTargetAuthor = true;
    }
  }

  const pathMatch = window.location.pathname.match(/\/question\/(\d+)\/answer\/(\d+)/);
  if (pathMatch) {
    questionId = pathMatch[1];
    answerId = answerId || pathMatch[2];
  }

  if (!questionTitle) {
    const h1 = document.querySelector('h1.QuestionHeader-title');
    if (h1) questionTitle = h1.textContent.trim();
  }

  // 提取正文中的所有知乎链接
  const links = new Set();
  const allLinks = document.querySelectorAll('a[href*="zhihu.com/question/"], a[href*="/question/"]');
  for (const a of allLinks) {
    let href = a.getAttribute('href') || '';
    const tm = href.match(/[?&]target=([^&]+)/);
    if (tm) { try { href = decodeURIComponent(tm[1]); } catch(e) {} }
    if (href.startsWith('//')) href = 'https:' + href;
    if (href.startsWith('/')) href = 'https://www.zhihu.com' + href;
    const m = href.match(/zhihu\.com\/question\/(\d+)\/answer\/(\d+)/);
    if (m) links.add(`https://www.zhihu.com/question/${m[1]}/answer/${m[2]}`);
    const m2 = href.match(/zhihu\.com\/question\/(\d+)(?:[/?#]|$)/);
    if (m2 && !m) links.add(`https://www.zhihu.com/question/${m2[1]}`);
  }

  const articleLinks = document.querySelectorAll('a[href*="zhuanlan.zhihu.com/p/"]');
  for (const a of articleLinks) {
    let href = a.getAttribute('href') || '';
    if (href.startsWith('//')) href = 'https:' + href;
    if (href.startsWith('/')) href = 'https://zhuanlan.zhihu.com' + href;
    const m = href.match(/zhuanlan\.zhihu\.com\/p\/(\d+)/);
    if (m) links.add(`https://zhuanlan.zhihu.com/p/${m[1]}`);
  }

  return {
    isTargetAuthor,
    authorNameFound,
    answerId,
    questionId,
    questionTitle,
    links: Array.from(links),
    url: window.location.href,
    isArticle: window.location.hostname === 'zhuanlan.zhihu.com',
  };
};

// 在问题页检查是否有目标答主的回答
const QUESTION_CHECK_FN = ({ token: targetToken, name: targetName }) => {
  const items = document.querySelectorAll('.AnswerItem');
  const results = [];
  for (const item of items) {
    try {
      const zop = JSON.parse(item.getAttribute('data-zop') || '{}');
      const authorLink = item.querySelector('a[href*="/people/"]');
      const authorHref = authorLink ? authorLink.getAttribute('href') : '';
      const itemToken = authorHref ? (authorHref.match(/\/people\/([^/?#]+)/) || [])[1] : '';
      if (itemToken === targetToken || zop.authorName === targetName) {
        results.push({
          answerId: zop.itemId,
          questionId: window.location.pathname.match(/\/question\/(\d+)/)?.[1] || '',
          title: zop.title || '',
          authorName: zop.authorName,
        });
      }
    } catch(e) {}
  }
  const h1 = document.querySelector('h1.QuestionHeader-title');
  return {
    answers: results,
    questionTitle: h1 ? h1.textContent.trim() : document.title,
  };
};

// 专栏文章页：校验作者 + 提取链接
const ARTICLE_FN = ({ token: authorToken }) => {
  const links = new Set();
  // 专栏页作者链接（css 选择器与回答页不同）
  let isTarget = false;
  const authorEl = document.querySelector('.AuthorInfo a[href*="/people/"], a.follow-author, ._column-author, a.UserLink-link');
  const authorHref = authorEl ? (authorEl.getAttribute('href') || '') : '';
  const m = authorHref.match(/\/people\/([^/?#]+)/);
  if (m && m[1] === authorToken) isTarget = true;
  // 兜底：js-initialData
  if (!isTarget) {
    try {
      const el = document.getElementById('js-initialData');
      if (el) {
        const data = JSON.parse(el.textContent);
        const authors = (data && data.initialState && data.initialState.entities && data.initialState.entities.articles) || {};
        for (const art of Object.values(authors)) {
          if (art && art.author && (art.author.urlToken === authorToken || art.author.name === '罗心澄')) { isTarget = true; break; }
        }
      }
    } catch(e) {}
  }
  for (const a of document.querySelectorAll('a[href*="zhihu.com/question/"], a[href*="/question/"], a[href*="zhuanlan.zhihu.com/p/"]')) {
    let href = a.getAttribute('href') || '';
    const tm = href.match(/[?&]target=([^&]+)/);
    if (tm) { try { href = decodeURIComponent(tm[1]); } catch(e) {} }
    if (href.startsWith('//')) href = 'https:' + href;
    if (href.startsWith('/question/')) href = 'https://www.zhihu.com' + href;
    if (href.startsWith('/p/')) href = 'https://zhuanlan.zhihu.com' + href;
    const m1 = href.match(/zhihu\.com\/question\/(\d+)\/answer\/(\d+)/);
    if (m1) links.add(`https://www.zhihu.com/question/${m1[1]}/answer/${m1[2]}`);
    const m2 = href.match(/zhihu\.com\/question\/(\d+)(?:[/?#]|$)/);
    if (m2 && !m1) links.add(`https://www.zhihu.com/question/${m2[1]}`);
    const m3 = href.match(/zhuanlan\.zhihu\.com\/p\/(\d+)/);
    if (m3) links.add(`https://zhuanlan.zhihu.com/p/${m3[1]}`);
  }
  return { isTarget, links: Array.from(links), title: document.title };
};

// ---------- 浏览器生命周期 ----------
let browser = null;
let context = null;
let page = null;

async function launchBrowser() {
  if (browser) { try { await browser.close(); } catch(e) {} }
  browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage'],
  });
  context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = window.chrome || { runtime: {} };
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
  });
  page = await context.newPage();
  page.on('crash', () => log('  [浏览器崩溃，将在下轮重启]'));
}

// ---------- 状态 ----------
const discoveredAnswers = new Map();
const visitedUrls = new Set();
const queue = [];
const failedUrls = new Map(); // url -> 连续失败次数
let visitCount = 0;

const statePath = path.join(OUTPUT_DIR, 'crawl_state.json');

function saveState() {
  const state = {
    discovered: Object.fromEntries(discoveredAnswers),
    visited: Array.from(visitedUrls),
    queue: queue.map(q => q.url),
    queueLength: queue.length,
    failed: Object.fromEntries(failedUrls),
    savedAt: new Date().toISOString(),
  };
  const tmp = statePath + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2), 'utf8');
  fs.renameSync(tmp, statePath);
}

function normalize(u) { return u.split('?')[0].split('#')[0]; }

function enqueue(url, from) {
  const clean = normalize(url);
  if (visitedUrls.has(clean)) return false;
  if (failedUrls.has(clean)) return false;           // 已放弃的不再入队
  if (queue.some(q => normalize(q.url) === clean)) return false;
  queue.push({ url: clean, from });
  return true;
}

(async () => {
  process.on('unhandledRejection', (e) => { log('[unhandledRejection]', e && e.message); });
  process.on('uncaughtException', (e) => { log('[uncaughtException]', e && e.message); });

  await launchBrowser();

  // 断点恢复
  let resumed = false;
  if (fs.existsSync(statePath)) {
    try {
      const saved = JSON.parse(fs.readFileSync(statePath, 'utf8'));
      for (const [url, info] of Object.entries(saved.discovered || {})) {
        discoveredAnswers.set(url, info);
      }
      for (const url of saved.visited || []) visitedUrls.add(url);
      for (const [url, n] of Object.entries(saved.failed || {})) failedUrls.set(url, n);
      if (Array.isArray(saved.queue)) {
        for (const u of saved.queue) {
          const clean = normalize(u);
          if (!visitedUrls.has(clean)) queue.push({ url: clean, from: 'restored' });
        }
      }
      resumed = true;
      log(`恢复状态: discovered=${discoveredAnswers.size} visited=${visitedUrls.size} queue=${queue.length}`);
    } catch(e) {
      log(`状态加载失败，从头开始: ${e.message}`);
    }
  }

  // 未验证的旧条目重新入队（verified 标记，避免全量重访）
  let reverifyCount = 0;
  for (const [url, info] of discoveredAnswers) {
    if (info.type === 'answer' && !info.verified) {
      visitedUrls.delete(url);
      if (enqueue(url, 'reverify')) reverifyCount++;
    }
  }
  if (reverifyCount > 0) log(`reverify: ${reverifyCount} 个未验证回答重新入队`);

  if (!resumed) {
    for (const url of INITIAL_URLS) queue.push({ url: normalize(url), from: 'initial' });
    discoveredAnswers.set(normalize(INITIAL_URLS[0]), {
      answerId: '2072103069089797019',
      questionId: '2069409558779408927',
      title: '男性愿意付高额彩礼结婚，这是否意味着他们比女性更渴望婚姻？',
      foundFrom: 'initial',
      type: 'answer',
      verified: true, // 初始 URL 就是答案来源，先标记，访问后再确认
    });
  }

  log(`开始爬取，队列: ${queue.length}`);

  while (queue.length > 0 && visitCount < MAX_VISITS) {
    const { url, from } = queue.shift();
    if (visitedUrls.has(url)) continue;
    visitedUrls.add(url);
    visitCount++;

    const isAnswer = url.includes('/question/') && url.includes('/answer/');
    const isQuestion = url.includes('/question/') && !url.includes('/answer/');
    const isArticle = url.includes('zhuanlan.zhihu.com/p/');

    log(`[${visitCount}/${MAX_VISITS}] (${from}) ${url.substring(0, 90)}`);

    // 每 40 次访问重启浏览器
    if (visitCount % 40 === 0) {
      log('  定期重启浏览器');
      try { await launchBrowser(); await page.waitForTimeout(1500); } catch(e) { log(`  重启失败: ${e.message}`); }
    }

    let loaded = false;
    for (let attempt = 0; attempt < 3 && !loaded; attempt++) {
      try {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
        loaded = true;
      } catch (e) {
        log(`  goto 第 ${attempt + 1} 次失败: ${(e.message || '').split('\n')[0]}`);
        try { await launchBrowser(); await page.waitForTimeout(2000); } catch(e2) {}
      }
    }
    if (!loaded) {
      // 失败回队：连续失败 < RETRY_MAX 时放回队尾，超过则放弃
      const fails = (failedUrls.get(url) || 0) + 1;
      if (fails < RETRY_MAX) {
        failedUrls.set(url, fails);
        visitedUrls.delete(url);
        queue.push({ url, from: `retry${fails}` });
        log(`  goto 放弃本轮，第 ${fails} 次失败，回队重试`);
      } else {
        log(`  goto 最终放弃: ${url}`);
      }
      continue;
    }
    failedUrls.delete(url); // 加载成功，清除失败计数

    // ---- 单页处理兜底：任何异常不击杀进程 ----
    try {
      await page.waitForTimeout(2000);
      await page.evaluate(() => {
        document.querySelectorAll('.signFlowModal, .Modal-wrapper, .modal-wrapper').forEach(el => el.remove());
      });

      if (isAnswer) {
        for (let i = 0; i < 3; i++) {
          const clicked = await page.evaluate(() => {
            const btns = Array.from(document.querySelectorAll('button'));
            const target = btns.find(b => b.innerText && b.innerText.includes('阅读全文'));
            if (target) { target.click(); return true; }
            return false;
          });
          if (!clicked) break;
          await page.waitForTimeout(800);
        }

        const info = await page.evaluate(SCRAPE_FN, { token: AUTHOR_TOKEN, name: AUTHOR_NAME });

        if (info.isTargetAuthor) {
          const entry = discoveredAnswers.get(url);
          if (entry) {
            entry.verified = true;
            entry.answerId = entry.answerId || info.answerId;
            entry.questionId = entry.questionId || info.questionId;
            entry.title = entry.title || info.questionTitle;
          } else {
            discoveredAnswers.set(url, {
              answerId: info.answerId, questionId: info.questionId,
              title: info.questionTitle, foundFrom: from, type: 'answer', verified: true,
            });
          }
          let newLinks = 0;
          for (const link of info.links) if (enqueue(link, url)) newLinks++;
          if (newLinks > 0) log(`  ✓ 目标答主，发现 ${newLinks} 个新链接`);
        } else {
          log(`  ✗ 不是目标答主 (作者: ${info.authorNameFound || '未知'})`);
          discoveredAnswers.delete(url);
        }
      } else if (isQuestion) {
        await page.waitForTimeout(1500);
        const checkResult = await page.evaluate(QUESTION_CHECK_FN, { token: AUTHOR_TOKEN, name: AUTHOR_NAME });
        if (checkResult.answers.length > 0) {
          for (const ans of checkResult.answers) {
            const answerUrl = `https://www.zhihu.com/question/${ans.questionId}/answer/${ans.answerId}`;
            const entry = discoveredAnswers.get(answerUrl);
            if (entry) {
              entry.verified = true;
              entry.title = entry.title || checkResult.questionTitle;
            } else {
              discoveredAnswers.set(answerUrl, {
                answerId: ans.answerId, questionId: ans.questionId,
                title: checkResult.questionTitle, foundFrom: from, type: 'answer', verified: true,
              });
            }
            enqueue(answerUrl, url);
            log(`  ✓ 问题页发现目标答主的回答: ${ans.answerId}`);
          }
        } else {
          log('  问题页未发现目标答主的回答');
        }
        const pageInfo = await page.evaluate(SCRAPE_FN, { token: AUTHOR_TOKEN, name: AUTHOR_NAME });
        let newLinks = 0;
        for (const link of pageInfo.links) if (enqueue(link, url)) newLinks++;
        if (newLinks > 0) log(`  问题页新增 ${newLinks} 个链接`);
      } else if (isArticle) {
        const info = await page.evaluate(ARTICLE_FN, { token: AUTHOR_TOKEN });
        let newLinks = 0;
        for (const link of info.links) if (enqueue(link, url)) newLinks++;
        if (info.isTarget) {
          const entry = discoveredAnswers.get(url);
          if (entry) { entry.verified = true; entry.title = info.title; }
          else discoveredAnswers.set(url, {
            articleId: url.match(/\/p\/(\d+)/)?.[1] || '',
            title: info.title, foundFrom: from, type: 'article', verified: true,
          });
          log(`  ✓ 专栏(目标作者)，新增链接 ${newLinks}`);
        } else {
          log(`  专栏(非目标/未知作者)，新增链接 ${newLinks}`);
        }
      }

      if (visitCount % SAVE_EVERY === 0) {
        saveState();
        log(`--- 进度: visited=${visitCount} discovered=${discoveredAnswers.size} queue=${queue.length} ---`);
      }

      await page.waitForTimeout(800 + Math.random() * 700);
    } catch (e) {
      log(`  异常: ${(e.message || '').split('\n')[0]}`);
      if (/crash|Execution context was destroyed|Target closed/i.test(String(e.message))) {
        try { await launchBrowser(); } catch(e2) {}
      }
      // 继续，不退出
    }
  }

  saveState();

  // 生成 articles.json：只写增量，保留 pipeline 回写的 docId
  const articlesPath2 = path.join(OUTPUT_DIR, 'articles.json');
  const prev = fs.existsSync(articlesPath2) ? JSON.parse(fs.readFileSync(articlesPath2, 'utf8')) : [];
  const prevDocIds = new Map(prev.filter(a => a.docId).map(a => [a.url, a.docId]));
  const answers = Array.from(discoveredAnswers.entries())
    .filter(([url, info]) => info.type === 'answer')
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([url, info], i) => ({
      idx: String(i + 1).padStart(3, '0'),
      type: 'answer',
      url,
      title: info.title || `回答 ${info.answerId}`,
      docId: prevDocIds.get(url) || '',
      verified: !!info.verified,
      meta: {
        answerId: info.answerId,
        questionId: info.questionId,
        foundFrom: info.foundFrom,
      }
    }));

  fs.writeFileSync(articlesPath2, JSON.stringify(answers, null, 2), 'utf8');
  log(`=== 结束: 总访问=${visitCount} 发现回答=${answers.length} ===`);
  log(`articles.json 已更新 (${answers.length} 条)`);

  try { await browser.close(); } catch(e) {}
  logStream.end();
})().catch(e => {
  log(`[FATAL]', ${e && e.stack || e}`);
  try { saveState(); } catch(e2) {}
  process.exit(1);
});
