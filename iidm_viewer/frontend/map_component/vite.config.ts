import { defineConfig } from 'vite';

// Streamlit serves the component's directory over HTTP; asset URLs must be
// relative so <script src="assets/..."> resolves against the component
// mount point rather than the Streamlit app root.
export default defineConfig({
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    assetsDir: 'assets',
    sourcemap: false,
    rollupOptions: {
      output: {
        // Deterministic filenames so the wheel diff stays small between
        // rebuilds of the same library version.
        entryFileNames: 'assets/map-component.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
});
