# West documentation build configuration file.
# Reference: https://www.sphinx-doc.org/en/master/usage/configuration.html

import importlib.metadata
from pathlib import Path

WEST_BASE = Path(__file__).resolve().parents[1]

project = 'West'
copyright = '2025, The Zephyr Project Contributors'
author = 'The Zephyr Project Contributors'
version = importlib.metadata.version("west")

extensions = [
    "sphinx_rtd_theme",
    "sphinx_tabs.tabs",
    'sphinx.ext.autodoc',
    "sphinx.ext.intersphinx",
]

templates_path = [str(WEST_BASE / 'doc' / '_templates')]
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

is_release = tags.has("release")  # pylint: disable=undefined-variable  # noqa: F821
docs_title = "Docs / {}".format(version if is_release else "Latest")
html_theme = 'sphinx_rtd_theme'
html_theme_options = {
    "logo_only": True,
    "prev_next_buttons_location": None,
    "navigation_depth": 5,
}
html_title = "West Documentation"
html_logo = str(WEST_BASE / "doc" / "_static" / "images" / "logo.png")
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
    "zephyr": ("https://docs.zephyrproject.org/latest/", None),
}

pygments_style = "sphinx"
highlight_language = "none"


def setup(app):
    # theme customizations
    app.add_css_file("css/custom.css")
    app.add_js_file("js/custom.js")
