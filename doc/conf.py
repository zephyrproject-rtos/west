# West documentation build configuration file.
# Reference: https://www.sphinx-doc.org/en/master/usage/configuration.html

from pathlib import Path

WEST_BASE = Path(__file__).resolve().parents[1]

project = 'West'
copyright = '2025, The Zephyr Project Contributors'
author = 'The Zephyr Project Contributors'

extensions = [
    "sphinx_rtd_theme",
    "sphinx_tabs.tabs",
    'sphinx.ext.autodoc',
    "sphinx.ext.intersphinx",
]

templates_path = [str(WEST_BASE / 'doc' / '_templates')]
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

html_theme = 'sphinx_rtd_theme'
html_theme_options = {
    "logo_only": True,
    "prev_next_buttons_location": None,
    "navigation_depth": 5,
}
html_title = "West Documentation"
html_logo = str(WEST_BASE / "doc" / "_static" / "images" / "logo.png")
html_static_path = [str(WEST_BASE / 'doc' / '_static')]

intersphinx_mapping = {
    "zephyr": ("https://docs.zephyrproject.org/latest/", None),
}

pygments_style = "sphinx"
highlight_language = "none"

def setup(app):
    # theme customizations
    app.add_css_file("css/custom.css")
    app.add_js_file("js/custom.js")
