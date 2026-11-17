import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
  build: {
    target: ['es2022', 'chrome120', 'safari16'],
    outDir: 'dist',
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom'],
          'markdown-katex': ['react-markdown', 'remark-gfm', 'remark-math', 'rehype-katex', 'katex'],
          highlight: ['highlight.js'],
        },
      },
    },
  },
});
