/**
 * Sandboxed HTML preview for email_draft body_html (and any
 * other approval payload that wants to show user-visible HTML
 * before approval).
 *
 * Why an iframe + sandbox: the HTML comes from a Claude-generated
 * draft. We don't intend it to do anything malicious, but inline
 * <script>, <style>, and link tags can still leak the principal's
 * data (e.g. tracking-pixel <img src="…">), and `dangerouslySetInnerHTML`
 * on a parent-level div would let any CSS escape into the rest of
 * the page. The iframe with ``sandbox`` strips JS execution +
 * same-origin parent access; ``allow-popups`` lets the principal
 * click links if they want to inspect (those open in a new tab via
 * the injected ``<base target="_blank">``).
 *
 * No allow-scripts, no allow-same-origin, no allow-forms — those
 * would re-enable the very attacks the sandbox is here to block.
 */
export function HtmlPreview({ html }: { html: string }) {
  const srcDoc = `<!DOCTYPE html>
<html>
  <head>
    <base target="_blank" />
    <meta charset="utf-8" />
    <style>
      html, body {
        margin: 0;
        padding: 1rem;
        font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
        font-size: 14px;
        color: #171717;
        background: #ffffff;
      }
      img { max-width: 100%; height: auto; }
      a { color: #1d4ed8; }
    </style>
  </head>
  <body>${html}</body>
</html>`;
  return (
    <iframe
      title="Email body preview"
      srcDoc={srcDoc}
      sandbox="allow-popups"
      className="block min-h-[12rem] w-full rounded-md border border-neutral-200 bg-white"
    />
  );
}
