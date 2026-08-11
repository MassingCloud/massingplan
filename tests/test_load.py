"""Concurrency, over real HTTP, against a real server.

Everything else in this suite drives the app through Flask's test client, which
is a function call: one request at a time, in the calling thread, with no
socket, no WSGI server and no thread pool. That is the right tool for asserting
what a response *says* and the wrong one for asserting anything about what
happens when two of them overlap -- which is the only state the app is ever
actually in.

Three properties, and the middle one is the reason this file exists.

1.  **Nothing 500s under concurrent load.** A per-process global that was safe
    single-threaded shows up here and nowhere else.

2.  **Two tenants hammering the same endpoints never see each other's data.**
    Org scoping is asserted throughout the suite, but always sequentially. If
    request state leaks between threads -- a session bound to the wrong
    identity, a scoped session not actually scoped, a cached principal -- the
    sequential tests all still pass and this one does not. That is a
    confidentiality property, not a performance one, and concurrency is the
    only way to reach it.

3.  **Password hashing is bounded in flight.** argon2id at 64MiB is the most
    expensive thing the app does, and a rate limit over a window does not bound
    the number arriving at the same instant. See the note where the latency
    assertion would have been for why there is no wall-clock threshold here.

Marked `performance` so it stays out of the default run: it binds a port,
starts threads, and takes seconds rather than milliseconds. It has its own CI
job, which is the only place it executes.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar

import pytest
from werkzeug.serving import make_server

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.models import Organization
from massingplan.services import accounts
from massingplan.services import repository as repo

# `conftest.py` lowers argon2 to 8KiB for the whole suite, autouse, so nothing
# here measures real hashing time -- and nothing here needs to. The property
# under test is the *bound* on concurrent hashes, not their cost, and it is
# asserted against a stub so that proving "sixteen are held to four" does not
# allocate the gigabyte the bound exists to prevent.
#
# Worth stating because the number that started this was measured outside
# pytest, where the cost is the shipped 64MiB: one sign-in took 35 seconds on a
# memory-pressured machine. That is the real per-request cost this file's
# assertions are about, and it is invisible from inside the suite.

pytestmark = pytest.mark.performance

PASSWORD = "a-long-enough-passphrase"
WORKERS = 8
ROUNDS = 6

XER = (
    "%T\tPROJECT\n%F\tproj_id\tproj_short_name\n%R\t1\t{code}\n"
    "%T\tTASK\n%F\ttask_id\tproj_id\ttask_code\ttask_name\ttarget_drtn_hr_cnt\n"
    + "".join(f"%R\t{10 + n}\t1\tA{1000 + n}\tActivity {n}\t40\n" for n in range(60))
    + "%E\n"
)


class Client:
    """A urllib session with its own cookie jar.

    Deliberately not `requests`: this file is about proving the server behaves,
    and a dependency-free client keeps the offline job honest. One jar per
    client is the whole point -- a shared jar would hide exactly the identity
    bleed this file is looking for.
    """

    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def get(self, path: str) -> tuple[int, str]:
        try:
            with self.opener.open(self.base + path, timeout=30) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # 4xx/5xx are answers, not failures
            return exc.code, exc.read().decode("utf-8", "replace")

    def post(self, path: str, fields: dict[str, str]) -> tuple[int, str]:
        body = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(  # noqa: S310 - http:// to our own test server
            self.base + path,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


@pytest.fixture
def server(tmp_path) -> Iterator[tuple[str, dict[str, str]]]:  # type: ignore[no-untyped-def]
    """A real WSGI server on a real port, in a thread, torn down after.

    `threaded=True` because a single-threaded server would serialise the
    requests and quietly turn every assertion below into a sequential test that
    happens to use sockets.
    """
    application = create_app(
        Settings(
            env="testing",
            secret_key="load-test-key",
            database_url=f"sqlite:///{tmp_path / 'load.db'}",
            rate_limit_enabled=False,
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False

    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
        rival = Organization(id="0" * 31 + "9", name="Rival", slug="rival-load")
        session.add(rival)
        session.flush()
        accounts.register(
            session,
            email="alpha@example.com",
            password=PASSWORD,
            organization_id=repo.DEFAULT_ORG_ID,
        )
        accounts.register(
            session, email="beta@example.com", password=PASSWORD, organization_id=rival.id
        )

    httpd = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", {}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


#: What the sign-in page has and no signed-in page does. Every assertion in
#: this file is worthless if the clients are not actually authenticated -- an
#: anonymous client sees no projects at all, so the cross-tenant test would pass
#: by seeing nothing. `urllib` follows the redirect, and a *failed* sign-in also
#: answers 200, so the status code cannot tell those apart. The body can.
SIGN_IN_MARKER = 'name="password"'


def _sign_in(base: str, email: str) -> Client:
    client = Client(base)
    status, _ = client.post("/auth/sign-in", {"email": email, "password": PASSWORD})
    assert status in (200, 302), status
    status, body = client.get("/projects")
    assert status == 200, status
    assert SIGN_IN_MARKER not in body, (
        f"{email} is not signed in -- /projects answered with the sign-in form. "
        "Every assertion in this file would pass vacuously from here."
    )
    return client


def test_the_harness_can_tell_a_signed_out_client_from_a_signed_in_one(server) -> None:  # type: ignore[no-untyped-def]
    """The negative control, first, because everything below depends on it.

    Without this, a broken sign-in makes the whole file green: anonymous
    clients see no projects, so "neither tenant saw the other's data" holds
    trivially. Asserted rather than assumed, having watched a guard in this
    repo turn out to be the shape of one.
    """
    base, _ = server
    anonymous = Client(base)
    status, signed_out_body = anonymous.get("/projects")
    assert status == 200
    assert SIGN_IN_MARKER in signed_out_body, "an anonymous client reached the project list"

    # The discriminator, asserted in both directions on the same page. If the
    # marker ever appears on a signed-in page too, `_sign_in` stops being able
    # to tell, and it fails open -- into a suite that passes on nothing.
    _status, signed_in_body = _sign_in(base, "alpha@example.com").get("/projects")
    assert SIGN_IN_MARKER not in signed_in_body

    # And a credential that cannot work must not yield a usable client, by
    # whichever check catches it first -- the 401 on the POST or the marker on
    # the page after it.
    with pytest.raises(AssertionError):
        _sign_in(base, "nobody@example.com")


def _upload(client: Client, code: str) -> str:
    """Multipart by hand, because the point is to exercise the real parser."""
    boundary = "----massingplanload"
    payload = XER.format(code=code)
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{code}.xer"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
        f"{payload}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - http:// to our own test server
        client.base + "/upload",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with client.opener.open(request, timeout=60) as response:
        final = response.geturl()
    return final.rstrip("/").rsplit("/", 1)[-1]


def test_nothing_five_hundreds_under_concurrent_load(server) -> None:  # type: ignore[no-untyped-def]
    """Eight clients, six rounds, across every page a signed-in user touches.

    A 5xx here and nowhere else is a per-process global: a module-level cache, a
    shared engine handed across threads, a request-scoped value that is not.
    """
    base, _ = server
    owner = _sign_in(base, "alpha@example.com")
    project_id = _upload(owner, "LOAD")
    owner.post(
        f"/projects/{project_id}/linear/locations",
        {"locations": "\n".join(f"L{n}" for n in range(1, 9))},
    )
    owner.post(
        f"/projects/{project_id}/linear/trades",
        {"key": "Frame", "rate": "95", "quantities": "380", "buffer_days": "1"},
    )

    paths = [
        "/projects",
        f"/projects/{project_id}",
        f"/projects/{project_id}/linear",
        "/healthz",
    ]

    def hammer(index: int) -> list[tuple[str, int]]:
        client = _sign_in(base, "alpha@example.com")
        seen = []
        for _ in range(ROUNDS):
            for path in paths:
                status, _body = client.get(path)
                seen.append((path, status))
        return seen

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(hammer, range(WORKERS)))

    failures = [(path, status) for batch in results for path, status in batch if status >= 500]
    assert not failures, f"{len(failures)} server errors under load: {failures[:5]}"

    total = sum(len(batch) for batch in results)
    assert total == WORKERS * ROUNDS * len(paths)


def test_two_tenants_under_load_never_see_each_other(server) -> None:  # type: ignore[no-untyped-def]
    """The property no sequential test can reach.

    Org scoping is asserted all through this suite, and always one request at a
    time. If identity leaks between overlapping requests -- a session bound to
    the wrong subject, a scoped session that is not actually scoped per thread,
    a principal cached on something shared -- every one of those tests still
    passes. This one is the only place that state exists.
    """
    base, _ = server
    alpha = _sign_in(base, "alpha@example.com")
    beta = _sign_in(base, "beta@example.com")
    alpha_project = _upload(alpha, "ALPHA")
    beta_project = _upload(beta, "BETAA")

    def as_alpha(_n: int) -> list[str]:
        client = _sign_in(base, "alpha@example.com")
        out = []
        for _ in range(ROUNDS):
            _status, body = client.get("/projects")
            out.append(body)
            status, _ = client.get(f"/projects/{beta_project}")
            out.append(f"cross:{status}")
        return out

    def as_beta(_n: int) -> list[str]:
        client = _sign_in(base, "beta@example.com")
        out = []
        for _ in range(ROUNDS):
            _status, body = client.get("/projects")
            out.append(body)
            status, _ = client.get(f"/projects/{alpha_project}")
            out.append(f"cross:{status}")
        return out

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        alphas = list(pool.map(as_alpha, range(WORKERS // 2)))
        betas = list(pool.map(as_beta, range(WORKERS // 2)))

    for batch in alphas:
        for entry in batch:
            if entry.startswith("cross:"):
                assert entry == "cross:404", "alpha reached beta's project"
            else:
                assert "BETAA" not in entry, "beta's project appeared in alpha's list"
    for batch in betas:
        for entry in batch:
            if entry.startswith("cross:"):
                assert entry == "cross:404", "beta reached alpha's project"
            else:
                assert "ALPHA" not in entry, "alpha's project appeared in beta's list"


def test_password_hashing_is_bounded_in_flight_not_only_per_window() -> None:
    """The finding this file was written to catch, and the one I had backwards.

    `LIMITS["auth.sign_in"]` is twenty per fifteen minutes, under a comment that
    called credential endpoints "cheap for the server". They are the opposite:
    argon2id at 64MiB is the most expensive thing the app does. A rate limit
    over a window does not bound *simultaneity* -- twenty attempts arriving
    together all pass a limit of twenty, and all twenty allocate 64MiB at the
    same instant. Distinct source addresses lift even that.

    So the bound has to be on hashes in flight. This asserts it holds: however
    many threads ask at once, no more than `MAX_CONCURRENT_HASHES` overlap.
    """
    observed_peak = 0
    live = 0
    guard = threading.Lock()
    real_hasher = accounts._hasher

    class _Stub:
        """Stands in for the argon2 hasher.

        No real hashing: the property under test is the semaphore around it,
        and running sixteen genuine 64MiB hashes to assert that they are
        bounded to four would allocate the very thing the bound exists to
        prevent. The sleep is what makes overlap observable.
        """

        @staticmethod
        def verify(_stored: str, _password: str) -> bool:
            return True

    def counting_hasher():  # type: ignore[no-untyped-def]
        nonlocal observed_peak, live
        with guard:
            live += 1
            observed_peak = max(observed_peak, live)
        try:
            time.sleep(0.05)  # long enough for overlap to be observable
            return _Stub()
        finally:
            with guard:
                live -= 1

    accounts._hasher = counting_hasher  # type: ignore[assignment]
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda _n: accounts.verify_password("x" * 12, "not-a-hash"), range(16)))
    finally:
        accounts._hasher = real_hasher  # type: ignore[assignment]

    assert observed_peak <= accounts.MAX_CONCURRENT_HASHES, (
        f"{observed_peak} password hashes overlapped against a bound of "
        f"{accounts.MAX_CONCURRENT_HASHES}. Each holds "
        f"{accounts.MEMORY_COST_KIB // 1024}MiB, so an unbounded count is a "
        "memory amplifier pointed at the server by anyone who can reach the "
        "sign-in form."
    )
    assert observed_peak > 1, (
        "no two hashes overlapped at all, so this asserted nothing. Either the "
        "bound is 1 or the harness stopped exercising concurrency."
    )


# There is no wall-clock latency assertion in this file, and that is a
# measurement rather than an omission.
#
# The first version compared p95 across eight clients against a solo baseline
# and failed at 62x, which looks exactly like requests serialising on a lock.
# It was not. `_sign_in` runs argon2id, the harness called it inside every
# worker thread, and the timings were measuring concurrent password hashing.
# With sign-in hoisted out and the clients released together, the same
# measurement across 1, 2, 4 and 8 workers gives 1.2x, 2.2x, 0.6x and 1.4x --
# flat, and noisy enough that four workers came out *faster* than one.
#
# A noise floor around 4x cannot host a threshold that catches the ~8x a real
# pool-of-one would produce: above the noise it catches nothing, below it it
# flaps, and a flaky timing test teaches people to rerun the job. What survives
# concurrency deterministically -- no 5xx, no cross-tenant bleed, a health
# check that still answers, a bounded number of hashes in flight -- is asserted
# above instead. The 62x was not wasted: chasing it is what found the missing
# concurrency bound.


def test_the_health_endpoint_answers_while_the_app_is_busy(server) -> None:  # type: ignore[no-untyped-def]
    """`/healthz` is what a load balancer polls to decide whether to keep
    sending traffic. If it queues behind the expensive pages, a burst of real
    work takes the instance out of rotation and turns a slow minute into an
    outage.
    """
    base, _ = server
    owner = _sign_in(base, "alpha@example.com")
    project_id = _upload(owner, "BUSY")

    stop = threading.Event()

    def churn(_n: int) -> int:
        client = _sign_in(base, "alpha@example.com")
        count = 0
        while not stop.is_set() and count < 40:
            client.get(f"/projects/{project_id}")
            count += 1
        return count

    probe = Client(base)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(churn, n) for n in range(WORKERS)]
        try:
            timings = []
            for _ in range(10):
                began = time.perf_counter()
                status, body = probe.get("/healthz")
                timings.append(time.perf_counter() - began)
                assert status == 200, status
                assert json.loads(body)["status"] == "ok"
        finally:
            stop.set()
            for future in futures:
                future.result(timeout=60)

    worst = max(timings)
    assert worst < 5.0, (
        f"/healthz took {worst * 1000:.0f}ms while the app was busy. A health "
        "check that queues behind real work removes the instance from rotation "
        "precisely when it is under load."
    )
