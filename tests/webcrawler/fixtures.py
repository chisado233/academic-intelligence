"""Local HTML fixtures for the webcrawler tests (offline, no network)."""

from __future__ import annotations

from collections.abc import Callable

import httpx


def build_transport(
    routes: dict[str, tuple[int, str]],
) -> tuple[httpx.MockTransport, Callable[[], int]]:
    """Return a MockTransport serving *routes* plus a hit counter.

    ``routes`` maps path → (status_code, body).  Unlisted paths get 404.
    """
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        status, body = routes.get(request.url.path, (404, "Not Found"))
        return httpx.Response(
            status,
            text=body,
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    return httpx.MockTransport(handler), lambda: hits["n"]


ARTICLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Attention Is All You Need - Paper Research Crawler</title>
  <meta name="author" content="Jane Q. Researcher">
</head>
<body>
  <header>
    <nav>
      <a href="/">Home</a>
      <a href="/papers">Papers</a>
    </nav>
  </header>
  <main>
    <article>
      <h1>Attention Is All You Need</h1>
      <p>The dominant sequence transduction models are based on complex recurrent
      or convolutional neural networks that include an encoder and a decoder.</p>
      <p>We propose a new simple network architecture, the Transformer, based
      solely on attention mechanisms, dispensing with recurrence and convolutions
      entirely.</p>
      <ul>
        <li><a href="/papers/transformer.pdf">PDF (open access)</a></li>
        <li><a href="https://external.example.com/related">Related work</a></li>
      </ul>
    </article>
  </main>
  <footer>(c) 2026 Academic Intelligence</footer>
</body>
</html>
"""

CHALLENGE_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Just a moment...</title>
</head>
<body>
  <div class="cf-browser-verification">
    <p>Checking your browser before accessing example.com.</p>
    <p>Please complete the captcha to verify you are human.</p>
    <form action="/__cf_chl_f" method="post">
      <input type="hidden" name="md" value="abc123">
    </form>
  </div>
</body>
</html>
"""

SPA_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Client App</title>
</head>
<body>
  <div id="root"></div>
  <noscript>Please enable JavaScript to continue.</noscript>
  <script src="/static/app.js"></script>
</body>
</html>
"""

PLAIN_HTML = """<!DOCTYPE html>
<html>
<head><title>Not An Article</title></head>
<body></body>
</html>
"""

ALLOW_ALL_ROBOTS = "User-agent: *\nAllow: /\n"

DENY_PRIVATE_ROBOTS = (
    "User-agent: paper-research-crawler\nDisallow: /private/\nUser-agent: *\nAllow: /\n"
)
