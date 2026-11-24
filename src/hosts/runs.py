"""Run one command on a host: put the caller's tree on it, run, return the end of the output.

The key that a provider returns at creation reaches this module on the host object and
nowhere else, so the run happens in the request that created the host.
"""

import shlex
import time
from dataclasses import dataclass
from typing import Any, Self

import asyncssh
from pydantic import BaseModel, Field, model_validator

from hosts import exceptions, schemas
from hosts.models import Host

CONNECT_TIMEOUT_SECONDS = 30
KEEPALIVE_INTERVAL_SECONDS = 15
SYNC_TIMEOUT_SECONDS = 600.0
# The clone and the patch each get SYNC_TIMEOUT_SECONDS.
SYNC_BUDGET_SECONDS = 2 * SYNC_TIMEOUT_SECONDS
OUTPUT_TAIL_CHARS = 20_000
CHECKOUT_DIR = "work"
APPLY_PATCH = "git apply --whitespace=nowarn -"


class RunRepo(BaseModel):
    url: str = Field(min_length=1, description="URL to clone. Secrets reach private repos.")
    commit: str = Field(min_length=1, description="Commit to check out before the patch applies.")


class RunCreate(schemas.HostCreate):
    """A command to run on a host that exists only for it."""

    repo: RunRepo
    patch: str | None = Field(
        default=None,
        description=(
            "A `git diff --binary` that `git apply` lays over the commit: the caller's own work. "
            "Without `--binary`, a changed binary file cannot apply."
        ),
    )
    command: str = Field(
        min_length=1, description="A shell command. It runs in the root of the checkout."
    )
    timeout_seconds: float = Field(
        default=600.0, gt=0, le=14400, description="The command is killed after this long."
    )

    @model_validator(mode="after")
    def reject_expires_at(self) -> Self:
        if "expires_at" in self.model_fields_set:
            raise ValueError("a run's lease follows its timeout; expires_at is not accepted")
        return self


class RunOut(BaseModel):
    exit_code: int | None = Field(description="None when the command timed out.")
    timed_out: bool
    output: str = Field(description="The end of stdout and stderr, in the order they came.")
    duration_seconds: float


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    timed_out: bool
    output: str
    duration_seconds: float


def _connect_kwargs(host: Host) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "username": host.ssh_username,
        "known_hosts": asyncssh.import_known_hosts(host.known_hosts),
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "keepalive_interval": KEEPALIVE_INTERVAL_SECONDS,
    }
    if host.internal_ssh_host:
        kwargs.update(host=host.internal_ssh_host, port=22)
    else:
        kwargs.update(host=host.external_ssh_host, port=host.external_ssh_port)
    # A tailnet host takes no key: the tailnet vouches for the caller.
    if host.private_key:
        kwargs["client_keys"] = [asyncssh.import_private_key(host.private_key)]
    return kwargs


async def _sync(
    conn: asyncssh.SSHClientConnection, *, url: str, commit: str, patch: str | None
) -> None:
    clone = (
        f"git clone --quiet --no-checkout {shlex.quote(url)} {CHECKOUT_DIR}"
        f" && cd {CHECKOUT_DIR} && git checkout --quiet --detach {shlex.quote(commit)}"
    )
    # Each step names what it reads on stdin: nothing, or the patch.
    steps: list[tuple[str, str, dict[str, object]]] = [
        ("clone", clone, {"stdin": asyncssh.DEVNULL})
    ]
    if patch:
        steps.append(("patch", f"cd {CHECKOUT_DIR} && {APPLY_PATCH}", {"input": patch}))
    for name, command, stdin in steps:
        try:
            done = await conn.run(
                command,
                check=False,
                timeout=SYNC_TIMEOUT_SECONDS,
                stderr=asyncssh.STDOUT,
                encoding="utf-8",
                errors="replace",
                **stdin,  # pyright: ignore[reportArgumentType]
            )
        except asyncssh.TimeoutError as error:
            raise exceptions.RunSyncError(f"the {name} timed out") from error
        if done.returncode != 0:
            tail = str(done.stdout or "")[-OUTPUT_TAIL_CHARS:]
            raise exceptions.RunSyncError(f"the {name} failed: exit={done.returncode} {tail}")


async def run_on_host(
    host: Host, *, url: str, commit: str, patch: str | None, command: str, timeout: float
) -> RunResult:
    try:
        conn = await asyncssh.connect(**_connect_kwargs(host))
    except (asyncssh.Error, OSError) as error:
        raise exceptions.HostUnreachableError(f"could not connect to the host: {error}") from error
    try:
        await _sync(conn, url=url, commit=commit, patch=patch)
        started = time.monotonic()
        try:
            done = await conn.run(
                f"cd {CHECKOUT_DIR} && {command}",
                check=False,
                timeout=timeout,
                stdin=asyncssh.DEVNULL,
                stderr=asyncssh.STDOUT,
                encoding="utf-8",
                errors="replace",
            )
            # A command that a signal killed has no exit status. Read it as failure.
            # ast-grep-ignore: rule-2
            exit_code = -1 if done.returncode is None else done.returncode
            output, timed_out = done.stdout, False
        except asyncssh.TimeoutError as error:
            exit_code, output, timed_out = None, error.stdout, True
        return RunResult(
            exit_code=exit_code,
            timed_out=timed_out,
            output=str(output or "")[-OUTPUT_TAIL_CHARS:],
            duration_seconds=time.monotonic() - started,
        )
    except (asyncssh.Error, OSError) as error:
        # The host went away or the connection dropped after it answered. A command's own
        # timeout is the inner except, and a failed clone or patch is a RunSyncError.
        raise exceptions.HostUnreachableError(f"lost the host during the run: {error}") from error
    finally:
        conn.close()
        await conn.wait_closed()
