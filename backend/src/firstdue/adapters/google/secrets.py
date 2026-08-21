"""Secret Manager.

The settings module has said since phase 1 that secrets come from "the
environment, or Secret Manager in deployed environments", and nothing read
Secret Manager. This is that.

The shape is deliberate: a setting names a **secret**, not a value. Anywhere a
credential is needed, the environment carries `..._SECRET_NAME` pointing at a
Secret Manager resource, and the value is fetched once at startup. Consequences:

* the value is never in an environment variable, so it is never in
  ``gcloud run services describe`` output, a crash dump, or a `docker inspect`;
* rotation is a new secret version rather than a redeploy;
* the value is never logged -- the resolver returns it and never records it, and
  the key names it is stored under are already caught by the log redactor.
"""

from __future__ import annotations

from typing import Any, Final

from firstdue.errors import ConfigurationError
from firstdue.observability.logging import get_logger

logger = get_logger(__name__)

LATEST: Final[str] = "latest"


class SecretResolver:
    """Fetches secret values, once, and caches them in memory only."""

    def __init__(self, *, project_id: str, client: Any | None = None) -> None:
        if not project_id:
            raise ConfigurationError("Secret Manager requires GCP_PROJECT_ID")
        self._project_id = project_id
        self._client = client
        self._cache: dict[str, str] = {}
        self.fetches = 0

    def _service(self) -> Any:  # pragma: no cover - live mode only
        if self._client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as exc:
                raise ConfigurationError(
                    "google-cloud-secret-manager is not installed; install the 'google' extra",
                    details={"package": "google-cloud-secret-manager"},
                ) from exc
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def resource_name(self, secret_name: str, version: str = LATEST) -> str:
        if "/" in secret_name:
            # Already a full resource path; honour it rather than double-prefixing.
            return secret_name
        return f"projects/{self._project_id}/secrets/{secret_name}/versions/{version}"

    def get(self, secret_name: str, *, version: str = LATEST) -> str:
        """Fetch one secret value.

        Raises:
            ConfigurationError: when the secret cannot be read. Startup fails
                loudly rather than the process running without a credential it
                needs and discovering that at request time.
        """
        key = f"{secret_name}@{version}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        name = self.resource_name(secret_name, version)
        try:  # pragma: no cover - live mode only
            response = self._service().access_secret_version(request={"name": name})
            value = str(response.payload.data.decode("utf-8"))
        except Exception as exc:
            # The secret *name* is safe to log; the value is what is not.
            logger.error(
                "secret_unavailable",
                extra={"secret_name": secret_name, "error_type": type(exc).__name__},
            )
            raise ConfigurationError(
                "a required secret could not be read from Secret Manager",
                details={"secret_name": secret_name},
            ) from exc

        self.fetches += 1
        self._cache[key] = value
        # Names and counts, never values.
        logger.info("secret_loaded", extra={"secret_name": secret_name, "version": version})
        return value

    def get_optional(self, secret_name: str | None, *, version: str = LATEST) -> str | None:
        """Fetch a secret that may legitimately not be configured."""
        if not secret_name:
            return None
        return self.get(secret_name, version=version)
