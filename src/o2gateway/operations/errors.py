from __future__ import annotations


class CloudError(Exception):
    status_code = 502


class CloudSessionMissing(CloudError):
    status_code = 503


class CloudSessionExpired(CloudError):
    status_code = 503


class CloudNotFound(CloudError):
    status_code = 404


class CloudAlreadyExists(CloudError):
    status_code = 409


class CloudForbidden(CloudError):
    status_code = 403


class CloudQuotaExceeded(CloudError):
    status_code = 507


class CloudRateLimited(CloudError):
    status_code = 429


class CloudMediaNotValidated(CloudError):
    """El proveedor rechaza operar sobre un media aún en ventana de validación (MED-1017)."""

    status_code = 423


class CloudRangeNotSatisfiable(CloudError):
    status_code = 416


class CloudTimeout(CloudError):
    status_code = 504


class CloudUnsupported(CloudError):
    status_code = 501

