/* Runtime configuration — trusted, build-time values only.
 *
 * The repository copy is the safe default: `apiBase: ''` means "same origin", which is
 * correct when the app is served by the backend itself (local dev, the tunnel, the VPS).
 *
 * GitHub Pages cannot serve /api/*, so scripts/build_pages.py writes a generated copy of
 * this file into the published directory with the deployed API origin, e.g.
 *   window.LINUXDO_AI_RUNTIME = { apiBase: 'https://tcstw.youngzyl.me:8443/linuxdo-api' };
 *
 * app.js reads `apiBase` and nothing else. It is never taken from the query string,
 * localStorage, the document or a cookie, so a crafted link cannot point the page (or the
 * owner token it holds) at another host. Only a base is valid: an absolute https:// origin
 * (or http://localhost for local dev), no path query, no fragment.
 */
window.LINUXDO_AI_RUNTIME = { apiBase: '' };
