const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  });
  const page = await context.newPage();

  console.log('Navigating to baoku.youdao.com...');
  await page.goto('https://baoku.youdao.com/home', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(3000);

  // Full page screenshot
  await page.screenshot({ path: 'docs/design-references/baoku/full-page-desktop.png', fullPage: true });
  console.log('Desktop screenshot saved');

  // Extract page structure
  const structure = await page.evaluate(() => {
    function walk(el, depth) {
      if (depth > 6) return null;
      const cs = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      const children = [...el.children];
      return {
        tag: el.tagName.toLowerCase(),
        classes: (el.className?.toString() || '').split(' ').slice(0, 8).join(' '),
        text: el.childNodes.length <= 3 && el.textContent.trim().length < 200 ? el.textContent.trim() : null,
        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
        bg: cs.backgroundColor !== 'rgba(0, 0, 0, 0)' ? cs.backgroundColor : null,
        bgImage: cs.backgroundImage !== 'none' ? cs.backgroundImage.slice(0, 100) : null,
        fontSize: cs.fontSize,
        color: cs.color,
        display: cs.display,
        flexDirection: cs.flexDirection,
        padding: cs.padding,
        borderRadius: cs.borderRadius !== '0px' ? cs.borderRadius : null,
        childCount: children.length,
        children: children.slice(0, 15).map(c => walk(c, depth + 1)).filter(Boolean)
      };
    }
    const app = document.querySelector('#app');
    return app ? JSON.stringify(walk(app, 0), null, 2) : 'No #app found';
  });

  require('fs').writeFileSync('docs/research/baoku/page-structure.json', structure);
  console.log('Page structure saved');

  // Extract colors and fonts
  const tokens = await page.evaluate(() => {
    const all = [...document.querySelectorAll('*')];
    const colors = new Set();
    const bgColors = new Set();
    const fonts = new Set();
    const fontSizes = new Set();
    all.slice(0, 500).forEach(el => {
      const cs = getComputedStyle(el);
      colors.add(cs.color);
      bgColors.add(cs.backgroundColor);
      fonts.add(cs.fontFamily);
      fontSizes.add(cs.fontSize);
    });
    return JSON.stringify({
      textColors: [...colors].filter(c => c !== 'rgba(0, 0, 0, 0)').slice(0, 20),
      bgColors: [...bgColors].filter(c => c !== 'rgba(0, 0, 0, 0)').slice(0, 20),
      fonts: [...fonts].slice(0, 10),
      fontSizes: [...fontSizes].sort().slice(0, 15),
    }, null, 2);
  });
  require('fs').writeFileSync('docs/research/baoku/design-tokens.json', tokens);
  console.log('Design tokens saved');

  // Extract all images
  const images = await page.evaluate(() => {
    return JSON.stringify([...document.querySelectorAll('img')].map(img => ({
      src: img.src,
      alt: img.alt,
      w: img.naturalWidth,
      h: img.naturalHeight,
    })).filter(i => i.src), null, 2);
  });
  require('fs').writeFileSync('docs/research/baoku/images.json', images);
  console.log('Images saved');

  // Extract route info
  const routeInfo = await page.evaluate(() => {
    return JSON.stringify({
      url: window.location.href,
      hash: window.location.hash,
      title: document.title,
    });
  });
  console.log('Route info:', routeInfo);

  // Try to get Vue router routes
  const vueRoutes = await page.evaluate(() => {
    const app = document.querySelector('#app')?.__vue_app__;
    if (app) {
      const router = app.config.globalProperties.$router;
      if (router) {
        return JSON.stringify(router.getRoutes().map(r => ({
          path: r.path,
          name: r.name,
          children: r.children?.map(c => ({ path: c.path, name: c.name }))
        })), null, 2);
      }
    }
    return 'No Vue router found';
  });
  require('fs').writeFileSync('docs/research/baoku/routes.json', vueRoutes);
  console.log('Routes saved');

  // Mobile screenshot
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(2000);
  await page.screenshot({ path: 'docs/design-references/baoku/full-page-mobile.png', fullPage: true });
  console.log('Mobile screenshot saved');

  await browser.close();
  console.log('Done!');
})().catch(e => { console.error(e); process.exit(1); });
