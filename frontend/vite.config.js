import { defineConfig } from "vite";

/* The policy the shipped app must carry (Build Plan section 5, rule 12).
   pywebview injects window.pywebview from the host side (ExecuteScriptAsync /
   runJavaScript / evaluateJavaScript), never as a page <script>, so a strict
   script-src 'self' does not disturb the Bridge API. */
const RELEASE_CSP =
  "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
  "img-src 'self' data:; font-src 'self'; connect-src 'self'; " +
  "object-src 'none'; base-uri 'none'; form-action 'none'";

/* Vite's dev server needs an inline bootstrap and a WebSocket for hot reload.
   This only ever reaches the page while `npm run dev` is serving it; the build
   writes RELEASE_CSP into index.html untouched. */
const DEV_CSP = RELEASE_CSP
  .replace("script-src 'self';", "script-src 'self' 'unsafe-inline';")
  .replace(
    "connect-src 'self';",
    "connect-src 'self' ws: wss: http://localhost:* http://127.0.0.1:*;",
  );

function devOnlyCsp() {
  return {
    name: "vaultnotes-dev-csp",
    apply: "serve",
    transformIndexHtml(html) {
      return html.replace(
        /(<meta http-equiv="Content-Security-Policy" content=")[^"]*(")/,
        `$1${DEV_CSP}$2`,
      );
    },
  };
}

// Builds the frontend into src/vaultnotes/web/, which is what pywebview
// loads in normal (non --dev) mode. Relative base so the built files work
// from pywebview's local file serving with no server root assumptions.
export default defineConfig({
  base: "./",
  plugins: [devOnlyCsp()],
  server: {
    // This machine only: in --dev the app window trusts whatever answers here.
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: "../src/vaultnotes/web",
    emptyOutDir: true,
  },
});
