"""Static files with content hashes in their names, imports between scripts included."""
from whitenoise.storage import CompressedManifestStaticFilesStorage


class HashedStaticFiles(CompressedManifestStaticFilesStorage):
    # Rewrite `import ... from "./core.js"` to the hashed name too, so a script never loads an
    # older copy of the scripts it uses.
    support_js_module_import_aggregation = True
    # A file missing from the manifest falls back to its plain name instead of breaking the page.
    manifest_strict = False
