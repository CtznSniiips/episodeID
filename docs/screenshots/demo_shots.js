const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  const out = process.argv[2]; const errs = [];
  for (const scheme of ['light', 'dark']) {
    const p = await b.newPage({viewport:{width:1280,height:900}, colorScheme: scheme, deviceScaleFactor: 1});
    p.on('pageerror', e => errs.push(e.message));
    await p.goto('http://127.0.0.1:8699/'); await p.waitForSelector('.series-card'); await p.waitForTimeout(400);
    await p.screenshot({ path: `${out}/home-${scheme}.png` });
    await p.goto('http://127.0.0.1:8699/#/series/1'); await p.waitForTimeout(1200);
    await p.screenshot({ path: `${out}/plan-${scheme}.png`, fullPage: false });
    await p.click('.tabs a[data-tab=episodes]'); await p.waitForTimeout(600);
    await p.screenshot({ path: `${out}/episodes-${scheme}.png` });
    await p.close();
  }
  console.log(errs); await b.close();
})();
