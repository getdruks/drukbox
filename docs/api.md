# API

The OpenAPI schema is the canonical reference: a running service serves
interactive docs at `/docs` and the raw schema at `/openapi.json`. This
page is the orientation.

## Authentication

Every route except `GET /healthz` and the OpenAPI pages takes
`Authorization: Bearer <token>`. The token is an admin key from
`SERVICE_TOKENS` or a service account token. Only admin keys manage
service accounts. A missing token returns `401`, a rejected one `403`.

## Service accounts

`POST /service-accounts` with `{"name": "ci"}` returns
`{"name": "ci", "token": "drkb_..."}` once. Drukbox stores only the name
and the SHA-256 fingerprint of the token. Names are 1–64 lowercase
letters, digits, or hyphens. A duplicate name returns `409`.
`DELETE /service-accounts/ci` revokes the token on the next request, or
returns `404` for an unknown name. The `admin` account exists from the
migration, has no token, and cannot be removed. Admin keys act as it.

## Endpoints

- `POST /service-accounts` · `DELETE /service-accounts/{name}` —
  admin keys only

- `POST /hosts` · `GET /hosts` · `GET /hosts/{id}` · `DELETE /hosts/{id}`
- `POST /hosts/runs` — run one command on a host that exists for it
- `POST /templates` · `GET /templates` · `GET /templates/{id}` ·
  `DELETE /templates/{id}`
- `POST /http-proxies` · `DELETE /http-proxies/{name}` ·
  `POST|DELETE /http-proxies/{name}/hosts/{host_id}`
- `POST /hosts/{host_id}/secrets/{service}/refresh` — refresh one secret
- `GET /doctor` — read-only dependency diagnostics
- `GET /healthz` — unauthenticated liveness probe

Host responses carry `service_account`: the service account that created
or claimed the host, `admin` for an admin key, or `null` for an unclaimed
warm host. Callers cannot set it. An `Idempotency-Key` belongs to the
service account that first used it. Another one reusing it gets `409`.

`POST /hosts` returns `503` with `CAPACITY` and `Retry-After` when the
provider has `HOST_LIMITS` live hosts, or when the caller's service account
has `HOST_LIMIT_PER_SERVICE_ACCOUNT` live hosts. A live host is not in
`error` and has not expired. The admin key has no service account limit.

`POST /hosts` returns `422` with `ISSUER_REFUSED` when the issuer refuses a
secret until a person acts. The detail is the issuer's reason, for example
that the GitHub App is not on the repository. A retry gets the same answer
until then.

## Run a command

`POST /hosts/runs` creates a host, puts a repo on it, runs one command, and
deletes the host before it answers. The body takes the host fields of
`POST /hosts` (`provider`, `template` or `image`, `env`, `secrets`,
`instance_type`, `disk_gb`) and these:

- `repo`: `url` to clone and the `commit` to check out. A private repo needs a
  secret that the sandbox reaches through the secrets exchange.
- `patch`: optional. A `git diff --binary` that `git apply` lays over the commit, so
  the caller's uncommitted and untracked files are on the host.
- `command`: a shell command that runs in the root of the checkout.
- `timeout_seconds`: 600 by default, 14400 at most. The command is killed after it.

The answer is `200` with `exit_code`, `timed_out`, `output` (the last 20000
characters of stdout and stderr together) and `duration_seconds`. A command that
fails still gets `200` and its exit code. `exit_code` is `null` when the command
timed out, and `-1` when a signal killed it.

The host never comes from the warm pool, because a pool member's private key
went out with the response that created it. `expires_at` is refused. The lease
starts when the run does, after the host is up, and lasts 20 minutes for the
clone and the patch, plus the timeout, plus five minutes. The janitor reaps the
host at the end of it if the request dies. A host that cannot be deleted at
once stays until then, and the caller still gets its result. So does a run
whose host was deleted meanwhile.

A provider that stops its VMs a fixed time after it creates them refuses a run
that cannot fit in that time.

- `502` with `PROVISIONING_FAILED`: The host could not be created.
- `502` with `HOST_UNREACHABLE`: drukbox could not open SSH to the host, or lost
  it during the run. It connects with the key that the provider returned at
  creation. A provider that
  returns none, such as exe.dev, needs a tailnet that lets drukbox's own node
  SSH to the host.
- `502` with `RUN_SYNC`: The clone or the patch failed. The detail has the end of
  its output.
- `422` with `RUN_TOO_LONG`: The provider stops its VMs sooner than provisioning,
  the sync, the timeout and the grace add up to. The provider counts a VM's life
  from its creation, so the check allows `PROVISIONING_GRACE_SECONDS` for it.

## Refresh a host secret

`POST /hosts/{host_id}/secrets/{service}/refresh` makes the exchange drop
its value for that secret and fetch a new one. A provider that stores the
value receives it at once. The response is `204` with no body. The API
sends no `Authorization` header to the exchange.

- `404` with `NOT_FOUND`: The host or the secret does not exist.
- `409` with `SECRET_STATIC`: The secret has a static value.
- `503` with `SECRET_REFRESH` and `Retry-After`: The exchange did not
  answer, or the issuer or provider did not supply a value.

## The secrets exchange

The exchange is a second process, `python -m secrets_exchange`, on loopback
with no bearer token. The API and proxy share its network namespace.
Remote callers use the API refresh route. See [Architecture](architecture.md)
for the flow.

- `GET /upstreams` — the hosts the proxy terminates TLS for
- `GET /authorize` — the proxy's question: the header and the real credential
  for a placeholder. `422` with the issuer's reason when it refuses the secret.
- `POST /refresh/{host_id}/{service}` — an issuer's order: forget the held
  value and fetch a new one now. `200` after the fetch, and after the push
  where the provider holds the value. `503` with `Retry-After` when the issuer
  gave nothing usable. `404` for an unknown host or service, `409` for a
  static entry. The order carries no body.
- `GET /healthz` — liveness probe
