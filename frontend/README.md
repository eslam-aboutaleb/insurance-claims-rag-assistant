# Frontend development

Use Node.js 24 or newer (`.node-version` selects 24). CI and both frontend Docker
images use Node 24 so the Vite 8 / Vitest 5 toolchain has the same supported runtime.

```sh
npm ci
npm audit --audit-level=moderate
npm run lint
npm run typecheck
npm test
npm run build
```

The `postcss-selector-parser` override selects the patched 7.1.7 release for
Tailwind 3 and postcss-nested, addressing GHSA-rj75-hqrm-r3gf without changing the
styling framework. The production CSS was compared byte for byte before and
after the parser update. Recheck the audit and CSS output before removing or
changing this override.

## README screenshots

The six images in `public/readme/` are captured from the frontend using Playwright
and synthetic API fixtures. No running backend, LLM key, or customer data is needed.
The fixtures demonstrate appearance; they do not validate backend or model behavior.

From `frontend/`, start `npm run dev`, then run in another terminal:

```sh
npx playwright install chromium
node scripts/readme-screenshots.mjs
```

Set `SCREENSHOT_BASE_URL` if the frontend runs at a different local URL. The script
captures the sign-in page, empty chat, conversation sidebar, policy answer, expanded
tool details, and claim status at 1440 × 1000 with the light theme and reduced motion.
