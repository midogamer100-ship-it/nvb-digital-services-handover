# portal

Static single-page portal: `index.html`, `app.js`, `styles.css`. No build step, no dependencies,
served by the gateway under a strict Content-Security-Policy (`default-src 'self'`).

Customer pages: sign-in (existing core credentials), statements (account + month, list, status,
download), loans (draft, submit, status). Staff pages (shown only for explicit extension roles):
loan review (assignment, recommendation, separate decision), batches (create, progress, failures,
retry, cancel), roles. "My identity" shows the stable subject used for role assignments.

All server data is rendered with `textContent`; the CSRF token lives in memory only; money input
is converted from a decimal string to integer minor units without floating point.
