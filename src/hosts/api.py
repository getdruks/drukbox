import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Response, status
from sqlalchemy.exc import SQLAlchemyError

from host_secrets.schemas import SECRET_NAME_PATTERN
from hosts import runs, schemas
from hosts.auth import require_auth
from hosts.deps import get_host_service
from hosts.exceptions import HostTeardownError
from hosts.models import Host
from hosts.service import HostService
from networking.tailscale import NetworkError
from providers.exceptions import ProviderError, UnknownProviderError, UnsupportedSizingError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hosts", tags=["hosts"], dependencies=[Depends(require_auth)])

HostServiceDep = Annotated[HostService, Depends(get_host_service)]


@router.post("", response_model=schemas.HostOut, status_code=status.HTTP_201_CREATED)
async def create_host(
    service: HostServiceDep,
    service_account: Annotated[str | None, Depends(require_auth)],
    payload: schemas.HostCreate | None = None,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            pattern=r"^[A-Za-z0-9_\-:.]+$",
            description=(
                "Caller-supplied retry key. Same key within "
                "IDEMPOTENCY_KEY_TTL_HOURS returns the same host. Charset: "
                "A-Z a-z 0-9 _ - : . (length 1-255)."
            ),
        ),
    ] = None,
) -> Host:
    host_create = payload or schemas.HostCreate()
    permanent = "expires_at" in host_create.model_fields_set and not host_create.expires_at

    try:
        return await service.get_or_create_host(
            service_account=service_account,
            env=host_create.env,
            secrets={name: entry.to_storage() for name, entry in host_create.secrets.items()},
            image=host_create.image,
            template=host_create.template,
            expires_at=host_create.expires_at,
            permanent=permanent,
            idempotency_key=idempotency_key,
            provider=host_create.provider,
            instance_type=host_create.instance_type,
            disk_gb=host_create.disk_gb,
        )
    except (UnknownProviderError, UnsupportedSizingError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        logger.exception("unexpected database error during host provisioning")
        raise HTTPException(
            status_code=503,
            detail="host provisioning could not be completed",
        ) from exc


@router.post("/runs", response_model=runs.RunOut)
async def run_command(
    payload: runs.RunCreate,
    service: HostServiceDep,
    service_account: Annotated[str | None, Depends(require_auth)],
) -> runs.RunResult:
    """Run one command on a host that exists for it: sync the tree, run, delete the host."""
    try:
        return await service.run_command(
            service_account=service_account,
            env=payload.env,
            secrets={name: entry.to_storage() for name, entry in payload.secrets.items()},
            image=payload.image,
            template=payload.template,
            provider=payload.provider,
            instance_type=payload.instance_type,
            disk_gb=payload.disk_gb,
            url=payload.repo.url,
            commit=payload.repo.commit,
            patch=payload.patch,
            command=payload.command,
            timeout=payload.timeout_seconds,
        )
    except (UnknownProviderError, UnsupportedSizingError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        logger.exception("unexpected database error during a run")
        raise HTTPException(status_code=503, detail="the run could not be completed") from exc


@router.get("", response_model=list[schemas.HostOut])
async def list_hosts(service: HostServiceDep) -> list[Host]:
    return await service.list_hosts()


@router.get("/{host_id}", response_model=schemas.HostOut)
async def get_host(host_id: uuid.UUID, service: HostServiceDep) -> Host:
    if host := await service.get_host(host_id):
        return host
    raise HTTPException(status_code=404, detail="host not found")


@router.post("/{host_id}/renew", response_model=schemas.HostOut)
async def renew_host(
    host_id: uuid.UUID,
    service: HostServiceDep,
    expires_at: Annotated[
        schemas.ExpiresAt,
        Body(
            embed=True,
            description=(
                "Omit or null: extend by LEASE_DEFAULT_TTL from now. "
                "Renewal never makes a host permanent."
            ),
        ),
    ] = None,
) -> Host:
    return await service.renew_host(host_id, expires_at=expires_at)


@router.post("/{host_id}/secrets/{service}/refresh", status_code=status.HTTP_204_NO_CONTENT)
async def refresh_secret(
    host_id: uuid.UUID,
    secret: Annotated[str, Path(alias="service", pattern=SECRET_NAME_PATTERN)],
    service: HostServiceDep,
) -> Response:
    await service.refresh_secret(host_id, secret)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_host(host_id: uuid.UUID, service: HostServiceDep) -> Response:
    try:
        await service.delete_host(host_id)
    except (ProviderError, NetworkError) as exc:
        logger.exception("unexpected error deleting sandbox host")
        raise HostTeardownError("host teardown could not be completed") from exc
    except SQLAlchemyError as exc:
        logger.exception("unexpected database error during host teardown")
        raise HostTeardownError("host teardown could not be completed") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
