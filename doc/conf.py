# West documentation build configuration file.
# Reference: https://www.sphinx-doc.org/en/master/usage/configuration.html

import datetime
import importlib.metadata
from pathlib import Path

WEST_BASE = Path(__file__).resolve().parents[1]

project = 'West'
copyright = (
    f'{datetime.datetime.now(tz=datetime.timezone.utc).year}, The Zephyr Project Contributors'
)
author = 'The Zephyr Project Contributors'
version = importlib.metadata.version("west")

extensions = [
    "sphinx_rtd_theme",
    "sphinx_tabs.tabs",
    'sphinx.ext.autodoc',
    "sphinx.ext.intersphinx",
]

templates_path = [str(WEST_BASE / 'doc' / '_templates')]
exclude_patterns = ['_build', 'README.rst', 'Thumbs.db', '.DS_Store']

is_release = tags.has("release")  # pylint: disable=undefined-variable  # noqa: F821
docs_title = "Docs / {}".format(version if is_release else "Latest")
html_theme = 'sphinx_rtd_theme'
html_theme_options = {
    "logo_only": True,
    "prev_next_buttons_location": None,
    "navigation_depth": 5,
}
html_title = "West Documentation"
html_logo = str(WEST_BASE / "doc" / "_static" / "images" / "logo.svg")
html_static_path = [str(WEST_BASE / 'doc' / '_static')]

html_context = {
    "show_license": True,
    "docs_title": docs_title,
    "is_release": is_release,
    "current_version": version,
    "versions": (("latest", "/"),),
    # Set google_searchengine_id to your Search Engine ID to replace built-in search
    # engine with Google's Programmable Search Engine.
    # See https://programmablesearchengine.google.com/ for details.
    "google_searchengine_id": "6301741b36c2a481a",
}

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "zephyr": ("https://docs.zephyrproject.org/latest/", None),
}

# Google Programmable Search results page, used by the search box when the
# Google engine is selected.
html_additional_pages = {"gsearch": "gsearch.html"}

GITHUB_BLOB_URL = "https://github.com/zephyrproject-rtos/west/blob/main/doc/"

pygments_style = "sphinx"
highlight_language = "none"


def set_github_blob_url(app, pagename, templatename, context, doctree):
    # Used by the "Open on GitHub" link in _templates/breadcrumbs.html.
    if doctree is not None:
        source = app.env.doc2path(pagename).relative_to(app.srcdir).as_posix()
        context["gh_blob_url"] = GITHUB_BLOB_URL + source


def set_html_permalinks_icon(_, config):
    # The RTD theme hard-codes a Font Awesome link icon in its setup() code,
    # but custom.css already draws one, so drop the theme's.
    config.html_permalinks_icon = ""


def setup(app):
    # theme customizations
    app.add_css_file("css/custom.css")
    app.add_js_file("js/custom.js")
    app.connect("config-inited", set_html_permalinks_icon, priority=900)
    app.connect("html-page-context", set_github_blob_url)
