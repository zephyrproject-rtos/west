Theme files copied from Zephyr
==============================

The Sphinx theme customizations under ``doc/_static`` and
``doc/_templates`` are copies of the files in the Zephyr repository's
``doc/`` directory, so the west documentation looks like
docs.zephyrproject.org.

Last synchronized with Zephyr ``main`` at commit
``c2db4195624649af5714244aaecfcc2b7ede7812`` (2026-11-11).

Copied files
------------

======================== ========================
West                     Zephyr
======================== ========================
``doc/_static/css/*``    ``doc/_static/css/*``
``doc/_static/js/*``     ``doc/_static/js/*``
``doc/_templates/*``     ``doc/_templates/*``
======================== ========================

Deliberate differences
----------------------

- ``_static/css/custom.css``: a block of West-specific overrides is
  appended at the end of the file (sidebar logo size, sidebar entries
  containing inline code). The rest of the file is a verbatim copy.
- ``_templates/layout.html``: no ``zversions.html`` include (no version
  switcher yet) and no kapa.ai widget.
- ``_templates/breadcrumbs.html``: the "Open on GitHub" link uses the
  ``gh_blob_url`` page context variable set in ``conf.py``, instead of
  the Jinja filters provided by Zephyr's ``gh_utils`` Sphinx extension.
  The "Report an issue" link is dropped for the same reason.

Synchronizing
-------------

With a Zephyr checkout next to this repository:

.. code-block:: shell

   for f in _static/css/*.css _static/js/* _templates/*.html; do
       diff -u ../zephyr/doc/$f doc/$f
   done

Copy over upstream changes, keep the differences listed above, update
the commit reference in this file and rebuild the documentation.
