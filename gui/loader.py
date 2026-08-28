"""Frontend asset loading for the progress panel.

The panel ships as three files under ``gui/assets`` — ``progress.html``,
``progress.css``, ``progress.js`` — so the markup, styling and behaviour stay
editable on their own. An MCP Apps resource is a single self-contained HTML
document rendered in a sandboxed iframe with no origin it could fetch siblings
from, so the CSS and JS are inlined into the HTML here, at import time.

The placeholders are comments, which keeps every file individually valid: the
HTML opens in a browser, the CSS and JS keep their editor tooling.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

HTML_FILE = "progress.html"
CSS_FILE = "progress.css"
SCRIPT_FILE = "progress.js"

CSS_PLACEHOLDER = "/* INJECT:CSS */"
SCRIPT_PLACEHOLDER = "/* INJECT:JS */"


def _read(name: str) -> str:
    """Read one asset file.

    Args:
        name: File name inside ``gui/assets``.

    Returns:
        str: File contents.

    Raises:
        RuntimeError: If the file is missing, which means the package was
            installed or copied without its assets.
    """
    path = ASSETS_DIR / name

    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(
            f"The progress panel asset {name} was not found at {path}. The gui "
            f"package needs its assets directory alongside it."
        ) from error


def _guard(text: str) -> str:
    """Neutralise sequences that would end the enclosing HTML element.

    The CSS and JS are inlined into ``<style>`` and ``<script>`` elements, where
    the HTML parser looks for a closing tag before any CSS or JS syntax applies.
    A literal ``</script>`` inside the script — in a string, or in a comment —
    would therefore cut the element short. Splitting the ``<`` keeps both
    languages' meaning intact.

    Args:
        text: Stylesheet or script source.

    Returns:
        str: Source safe to embed in an inline element.
    """
    return text.replace("</", "<\\/")


@lru_cache(maxsize=1)
def load_progress_app_html() -> str:
    """Build the progress panel as one self-contained HTML document.

    Returns:
        str: HTML with the stylesheet and script inlined.

    Raises:
        RuntimeError: If an asset is missing, or if the HTML has lost one of the
            injection placeholders — either way the panel would render blank,
            which is worse to debug later than failing at startup.
    """
    html = _read(HTML_FILE)

    for placeholder in (CSS_PLACEHOLDER, SCRIPT_PLACEHOLDER):
        if placeholder not in html:
            raise RuntimeError(
                f"{HTML_FILE} does not contain the placeholder "
                f"{placeholder!r}, so the panel's assets cannot be inlined."
            )

    html = html.replace(CSS_PLACEHOLDER, _guard(_read(CSS_FILE)))
    html = html.replace(SCRIPT_PLACEHOLDER, _guard(_read(SCRIPT_FILE)))

    return html
