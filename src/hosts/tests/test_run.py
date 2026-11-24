import subprocess
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh
import pytest
from sqlalchemy import func, select

from core.database import async_session_factory
from core.exceptions import ResourceNotFoundError
from hosts import exceptions, runs
from hosts.exceptions import HostStateError
from hosts.models import Host, HostStatus
from hosts.runs import SYNC_BUDGET_SECONDS
from hosts.service import RUN_LEASE_GRACE_SECONDS, utc_now
from providers.exceptions import ProviderTransportError

HEADERS = {"Authorization": "Bearer service-token"}
BODY = {
    "provider": "stub",
    "repo": {"url": "https://github.com/acme/app", "commit": "abc123"},
    "command": "make test",
}


async def _activate(self, host):
    host.status = HostStatus.ACTIVE.value
    host.updated_at = utc_now()
    await self.session.commit()


@pytest.fixture
def provisioned(monkeypatch):
    monkeypatch.setattr("hosts.service.HostService.provision", _activate)


def _result(**changes):
    return runs.RunResult(
        **{
            "exit_code": 0,
            "timed_out": False,
            "output": "ok\n",
            "duration_seconds": 1.5,
        }
        | changes
    )


async def _host_count() -> int:
    async with async_session_factory() as session:
        return (await session.execute(select(func.count()).select_from(Host))).scalar_one()


async def test_a_run_answers_with_the_result_and_deletes_its_host(
    client, monkeypatch, stub_provider, provisioned
):
    run = AsyncMock(return_value=_result(exit_code=3, output="1 failed\n"))
    monkeypatch.setattr("hosts.service.run_on_host", run)

    response = await client.post(
        "/hosts/runs", headers=HEADERS, json=BODY | {"patch": "diff --git a/x b/x\n"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "exit_code": 3,
        "timed_out": False,
        "output": "1 failed\n",
        "duration_seconds": 1.5,
    }
    assert run.await_args
    assert run.await_args.kwargs == {
        "url": "https://github.com/acme/app",
        "commit": "abc123",
        "patch": "diff --git a/x b/x\n",
        "command": "make test",
        "timeout": 600.0,
    }
    assert len(stub_provider.deleted) == 1
    assert await _host_count() == 0


async def test_a_run_deletes_its_host_when_the_run_fails(
    client, monkeypatch, stub_provider, provisioned
):
    monkeypatch.setattr(
        "hosts.service.run_on_host",
        AsyncMock(side_effect=exceptions.RunSyncError("the clone failed: exit=128")),
    )

    response = await client.post("/hosts/runs", headers=HEADERS, json=BODY)

    assert response.status_code == 502
    assert response.json()["error_code"] == "RUN_SYNC"
    assert len(stub_provider.deleted) == 1
    assert await _host_count() == 0


async def test_a_run_survives_a_failed_teardown(client, monkeypatch, stub_provider, provisioned):
    """The janitor reaps the row at the end of the lease, so the caller keeps its result."""
    monkeypatch.setattr("hosts.service.run_on_host", AsyncMock(return_value=_result()))
    monkeypatch.setattr(stub_provider, "delete_vm", AsyncMock(side_effect=ProviderTransportError))

    response = await client.post("/hosts/runs", headers=HEADERS, json=BODY)

    assert response.status_code == 200
    assert await _host_count() == 1


async def test_a_runs_lease_starts_when_the_run_does(
    client, monkeypatch, stub_provider, provisioned
):
    """A slow provision must not eat into the lease of the command that follows it."""
    clock = [utc_now()]
    monkeypatch.setattr("hosts.service.utc_now", lambda: clock[0])

    async def slow_provision(self, host):
        clock[0] += timedelta(seconds=450)
        await _activate(self, host)

    monkeypatch.setattr("hosts.service.HostService.provision", slow_provision)
    seen = []

    async def run(host, **kwargs):
        seen.append((host.expires_at, clock[0]))
        return _result()

    monkeypatch.setattr("hosts.service.run_on_host", run)

    await client.post("/hosts/runs", headers=HEADERS, json=BODY | {"timeout_seconds": 120})

    expires_at, started = seen[0]
    lease = timedelta(seconds=SYNC_BUDGET_SECONDS + 120 + RUN_LEASE_GRACE_SECONDS)
    remaining = expires_at.replace(tzinfo=None) - started.replace(tzinfo=None)
    assert abs(remaining - lease) < timedelta(seconds=5)


async def test_a_run_survives_a_host_that_is_already_gone(
    client, monkeypatch, stub_provider, provisioned
):
    """A host that the janitor or a person deleted mid-run does not turn the result into a 404."""

    async def run(host, **kwargs):
        async with async_session_factory() as session:
            await session.delete(await session.get(Host, host.id))
            await session.commit()
        return _result(output="done\n")

    monkeypatch.setattr("hosts.service.run_on_host", run)

    response = await client.post("/hosts/runs", headers=HEADERS, json=BODY)

    assert response.status_code == 200
    assert response.json()["output"] == "done\n"


async def test_a_provider_that_stops_its_vms_early_refuses_a_run_that_cannot_fit(
    client, monkeypatch, stub_provider, provisioned
):
    monkeypatch.setattr(stub_provider, "max_lifetime", timedelta(minutes=45))
    run = AsyncMock(return_value=_result())
    monkeypatch.setattr("hosts.service.run_on_host", run)

    too_long = await client.post(
        "/hosts/runs", headers=HEADERS, json=BODY | {"timeout_seconds": 1500}
    )
    # 900 seconds: the lease alone (2400) fits in 45 minutes, but the VM's life counts from
    # its creation, and provisioning may take 600 more.
    no_room_to_provision = await client.post(
        "/hosts/runs", headers=HEADERS, json=BODY | {"timeout_seconds": 900}
    )
    fits = await client.post("/hosts/runs", headers=HEADERS, json=BODY | {"timeout_seconds": 60})

    assert too_long.status_code == 422
    assert too_long.json()["error_code"] == "RUN_TOO_LONG"
    assert no_room_to_provision.status_code == 422
    assert "with provisioning" in no_room_to_provision.json()["detail"]
    assert fits.status_code == 200
    run.assert_awaited_once()


@pytest.mark.parametrize("error", [ResourceNotFoundError("host not found"), HostStateError("x")])
async def test_a_host_lost_before_the_run_starts_is_unreachable_and_is_deleted(
    client, monkeypatch, stub_provider, provisioned, error
):
    monkeypatch.setattr("hosts.service.HostService.renew_host", AsyncMock(side_effect=error))
    run = AsyncMock(return_value=_result())
    monkeypatch.setattr("hosts.service.run_on_host", run)

    response = await client.post("/hosts/runs", headers=HEADERS, json=BODY)

    assert response.status_code == 502
    assert response.json()["error_code"] == "HOST_UNREACHABLE"
    run.assert_not_awaited()
    assert len(stub_provider.deleted) == 1


async def test_a_run_survives_a_host_that_cannot_be_deleted_in_its_state(
    client, monkeypatch, stub_provider, provisioned
):
    monkeypatch.setattr("hosts.service.run_on_host", AsyncMock(return_value=_result()))
    monkeypatch.setattr(
        "hosts.service.HostService.delete_host",
        AsyncMock(side_effect=HostStateError("host is still provisioning")),
    )

    response = await client.post("/hosts/runs", headers=HEADERS, json=BODY)

    assert response.status_code == 200


async def test_a_run_refuses_an_expires_at(client, stub_provider):
    response = await client.post(
        "/hosts/runs",
        headers=HEADERS,
        json=BODY | {"expires_at": (utc_now() + timedelta(hours=1)).isoformat()},
    )

    assert response.status_code == 422
    assert "expires_at" in response.text


class FakeConnection:
    def __init__(self, *results):
        self.results = list(results)
        self.commands = []
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None

    async def run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return self.results.pop(0)()


def _completed(returncode: int | None = 0, stdout=""):
    return lambda: SimpleNamespace(returncode=returncode, stdout=stdout)


def _raises(error):
    def run():
        raise error

    return run


def _host(**changes):
    fields = {
        "id": "h",
        "name": "sb-1",
        "external_ssh_host": "203.0.113.1",
        "external_ssh_port": 2222,
        "internal_ssh_host": None,
        "ssh_username": "ops",
        "known_hosts": "",
    }
    return Host(**fields | changes)


@pytest.fixture
def connect(monkeypatch):
    def install(connection):
        monkeypatch.setattr("hosts.runs.asyncssh.connect", AsyncMock(return_value=connection))

    return install


async def test_a_run_clones_applies_the_patch_and_runs_in_the_checkout(connect):
    connection = FakeConnection(_completed(), _completed(), _completed(1, "FAILED\n"))
    connect(connection)

    result = await runs.run_on_host(
        _host(),
        url="https://github.com/acme/app",
        commit="abc123",
        patch="the patch",
        command="make test",
        timeout=30,
    )

    clone, patch, command = connection.commands
    assert "git clone" in clone[0]
    assert "https://github.com/acme/app work" in clone[0]
    assert "git checkout --quiet --detach abc123" in clone[0]
    assert patch[0].endswith("git apply --whitespace=nowarn -")
    assert patch[1]["input"] == "the patch"
    assert command[0] == "cd work && make test"
    assert command[1]["timeout"] == 30
    assert (result.exit_code, result.output, result.timed_out) == (1, "FAILED\n", False)


async def test_a_url_or_commit_with_shell_syntax_stays_one_argument(connect):
    connection = FakeConnection(_completed(), _completed())
    connect(connection)

    await runs.run_on_host(
        _host(), url="https://x/$(id)", commit="a;b", patch=None, command="true", timeout=1
    )

    assert "'https://x/$(id)'" in connection.commands[0][0]
    assert "'a;b'" in connection.commands[0][0]


async def test_a_run_closes_its_connection_when_the_sync_fails(connect):
    connection = FakeConnection(_completed(128, "fatal\n"))
    connect(connection)

    with pytest.raises(exceptions.RunSyncError):
        await runs.run_on_host(_host(), url="u", commit="c", patch=None, command="x", timeout=1)

    assert connection.closed


async def test_a_run_without_a_patch_applies_none(connect):
    connection = FakeConnection(_completed(), _completed())
    connect(connection)

    await runs.run_on_host(_host(), url="u", commit="c", patch=None, command="true", timeout=30)

    assert [command for command, _ in connection.commands][1] == "cd work && true"


async def test_a_run_keeps_the_end_of_a_long_output(connect):
    connect(FakeConnection(_completed(), _completed(0, "a" * runs.OUTPUT_TAIL_CHARS + "tail")))

    result = await runs.run_on_host(
        _host(), url="u", commit="c", patch=None, command="x", timeout=30
    )

    assert len(result.output) == runs.OUTPUT_TAIL_CHARS
    assert result.output.endswith("tail")


async def test_a_command_that_times_out_returns_what_it_printed(connect):
    timeout = asyncssh.TimeoutError(None, "x", None, None, None, None, "partial\n", "")
    connect(FakeConnection(_completed(), _raises(timeout)))

    result = await runs.run_on_host(
        _host(), url="u", commit="c", patch=None, command="x", timeout=1
    )

    assert (result.exit_code, result.timed_out, result.output) == (None, True, "partial\n")


async def test_a_command_that_a_signal_killed_reads_as_failure(connect):
    connect(FakeConnection(_completed(), _completed(None, "")))

    result = await runs.run_on_host(
        _host(), url="u", commit="c", patch=None, command="x", timeout=1
    )

    assert result.exit_code == -1


async def test_a_failed_clone_names_its_output(connect):
    connect(FakeConnection(_completed(128, "fatal: repository not found\n")))

    with pytest.raises(exceptions.RunSyncError, match="clone failed: exit=128 fatal: repository"):
        await runs.run_on_host(_host(), url="u", commit="c", patch=None, command="x", timeout=1)


async def test_a_patch_that_does_not_apply_fails_the_run(connect):
    connect(FakeConnection(_completed(), _completed(1, "error: patch failed\n")))

    with pytest.raises(exceptions.RunSyncError, match="patch failed: exit=1"):
        await runs.run_on_host(_host(), url="u", commit="c", patch="p", command="x", timeout=1)


async def test_a_connection_lost_during_the_command_fails_the_run_as_unreachable(connect):
    connection = FakeConnection(_completed(), _raises(asyncssh.ConnectionLost("reset")))
    connect(connection)

    with pytest.raises(exceptions.HostUnreachableError, match="lost the host"):
        await runs.run_on_host(_host(), url="u", commit="c", patch=None, command="x", timeout=1)

    assert connection.closed


async def test_a_connection_lost_during_the_clone_fails_the_run_as_unreachable(connect):
    connect(FakeConnection(_raises(asyncssh.ConnectionLost("reset"))))

    with pytest.raises(exceptions.HostUnreachableError, match="lost the host"):
        await runs.run_on_host(_host(), url="u", commit="c", patch=None, command="x", timeout=1)


async def test_an_unreachable_host_fails_the_run(monkeypatch):
    monkeypatch.setattr(
        "hosts.runs.asyncssh.connect", AsyncMock(side_effect=ConnectionRefusedError("refused"))
    )

    with pytest.raises(exceptions.HostUnreachableError, match="refused"):
        await runs.run_on_host(_host(), url="u", commit="c", patch=None, command="x", timeout=1)


def test_a_tailnet_host_connects_on_22_without_a_key():
    kwargs = runs._connect_kwargs(_host(internal_ssh_host="sb-1.tail.ts.net"))

    assert (kwargs["host"], kwargs["port"]) == ("sb-1.tail.ts.net", 22)
    assert "client_keys" not in kwargs


def test_a_host_with_a_key_connects_to_its_external_address_with_it():
    host = _host()
    host.private_key = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()

    kwargs = runs._connect_kwargs(host)

    assert (kwargs["host"], kwargs["port"], kwargs["username"]) == ("203.0.113.1", 2222, "ops")
    assert len(kwargs["client_keys"]) == 1


def _git(*args, cwd, stdin=None):
    return subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_binary_file_applies_from_a_binary_patch_only(tmp_path):
    """The patch is the caller's `git diff`: only `--binary` carries a binary file's content."""
    source, work = tmp_path / "source", tmp_path / "work"
    source.mkdir()
    _git("init", "-q", cwd=source)
    (source / "a.txt").write_text("a\n")
    _git("add", ".", cwd=source)
    _git("commit", "-qm", "base", cwd=source)
    _git("clone", "-q", str(source), str(work), cwd=tmp_path)
    (source / "logo.bin").write_bytes(bytes(range(256)) * 8)
    _git("add", "--intent-to-add", "--all", cwd=source)
    plain = _git("diff", "HEAD", cwd=source).stdout
    binary = _git("diff", "--binary", "HEAD", cwd=source).stdout
    apply = runs.APPLY_PATCH.split()[1:]

    assert _git(*apply, cwd=work, stdin=plain).returncode != 0
    assert not (work / "logo.bin").exists()
    assert _git(*apply, cwd=work, stdin=binary).returncode == 0
    assert (work / "logo.bin").read_bytes() == (source / "logo.bin").read_bytes()
