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
