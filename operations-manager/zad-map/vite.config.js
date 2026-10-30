import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

// Eén ingecheckt bestand, zoals static/js/codemirror-bundle.js: de CSP van OPI staat geen
// externe host toe, en er is geen bouwstap in CI of in de Docker-build.
export default defineConfig({
  plugins: [svelte({ compilerOptions: { customElement: true } })],
  // @xyflow/svelte toetst `process.env.NODE_ENV`; in een browser bestaat `process` niet.
  define: { 'process.env.NODE_ENV': '"production"' },
  build: {
    outDir: '../python/static/js',
    emptyOutDir: false,
    cssCodeSplit: false,
    lib: {
      entry: 'src/main.js',
      name: 'ZadMap',
      formats: ['iife'],
      fileName: () => 'zad-map.js',
    },
  },
});
