"""Strict production assets with a narrow Jazzmin 3.0.5 compatibility bridge."""

from urllib.parse import urljoin

from whitenoise.storage import CompressedManifestStaticFilesStorage


class MeridianManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    def url(self, name, force=False):
        # Jazzmin's base template passes this directory to {% static %} for
        # data-theme-base. Manifests contain files, not directories. Only this
        # exact URL prefix bypasses lookup; every actual asset remains hashed
        # and subject to strict manifest validation. Remove this compatibility
        # bridge when Jazzmin uses get_static_prefix for its directory URL.
        if name == 'vendor/bootswatch':
            return urljoin(self.base_url, name)
        return super().url(name, force=force)
