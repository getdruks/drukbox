from typing import ClassVar

from core.exceptions import AppException


class HostStateError(AppException):
    status_code = 409
    error_code = "HOST_STATE"


class IdempotencyKeyConflictError(AppException):
    status_code = 409
    error_code = "IDEMPOTENCY_KEY_CONFLICT"


class HostTeardownError(AppException):
    status_code = 503
    error_code = "HOST_TEARDOWN"


class HostCapacityError(AppException):
    status_code = 503
    error_code = "CAPACITY"
    headers: ClassVar[dict[str, str]] = {"Retry-After": "30"}


class ProvisioningFailedError(AppException):
    status_code = 502
    error_code = "PROVISIONING_FAILED"


class HostUnreachableError(AppException):
    status_code = 502
    error_code = "HOST_UNREACHABLE"


class RunTooLongError(AppException):
    status_code = 422
    error_code = "RUN_TOO_LONG"


class RunSyncError(AppException):
    status_code = 502
    error_code = "RUN_SYNC"


class HostKeyScanError(Exception):
    pass
