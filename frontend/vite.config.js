import { defineConfig } from "vite";

// Builds the frontend into src/vaultnotes/web/, which is what pywebview
// loads in normal (non --dev) mode. Relative base so the built files work
// from pywebview's local file serving with no server root assumptions.
export default defineConfig({
  base: "./",
  build: {
    outDir: "../src/vaultnotes/web",
    emptyOutDir: true,
  },
});
