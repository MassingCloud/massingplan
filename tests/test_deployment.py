"""The deployment stack, checked as configuration rather than trusted as text.

Every assertion here corresponds to a way a container can look fine and be
wrong: a moving base image, a root user, a `*` in a trusted-proxy list, an
entrypoint that boots without a secret key.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (ROOT / "deploy" / "docker-entrypoint.sh").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
CI_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
CI = yaml.safe_load(CI_TEXT)


# -- the image -------------------------------------------------------------


def test_the_base_image_is_pinned_by_digest_not_by_tag() -> None:
    """`python:3.12-slim` is a moving target: the image CI passed on and the
    image that ships are otherwise different builds, invisibly.
    """
    froms = re.findall(r"^FROM\s+(\S+)", DOCKERFILE, re.MULTILINE)
    assert froms, "no FROM lines"
    for image in froms:
        assert "@sha256:" in image, f"{image} is pinned by tag, not digest"


def test_every_build_stage_uses_the_same_base() -> None:
    """A build stage on a different base compiles wheels against a libc the
    runtime does not have, and it fails at import rather than at build.
    """
    bases = {
        re.sub(r"\s+AS\s+\w+$", "", line)
        for line in re.findall(r"^FROM\s+(.+)$", DOCKERFILE, re.MULTILINE)
    }
    assert len(bases) == 1, f"stages disagree on the base image: {bases}"


def test_the_runtime_carries_no_compiler() -> None:
    """Shipping the toolchain that built the wheels puts gcc in the production
    attack surface for no benefit after the build.
    """
    runtime = DOCKERFILE.split("AS build")[-1].split("\nFROM ")[-1]
    assert "build-essential" not in runtime
    assert "gcc" not in runtime


def test_it_runs_as_a_non_root_user() -> None:
    assert re.search(r"^USER\s+appuser", DOCKERFILE, re.MULTILINE)
    assert "--uid 10001" in DOCKERFILE
    # Ownership is set *before* the VOLUME, so Docker seeds the named volume
    # with a directory appuser can write to rather than a root-only one.
    assert DOCKERFILE.index("chown -R appuser") < DOCKERFILE.index("VOLUME")


def test_the_healthcheck_is_liveness_not_readiness() -> None:
    """A liveness probe that hits `/readyz` kills the container during a brief
    database blip -- which does not bring the database back and does lose
    in-flight requests.
    """
    healthcheck = re.search(r"HEALTHCHECK[\s\S]*?CMD (.+)", DOCKERFILE)
    assert healthcheck is not None
    assert "/healthz" in healthcheck.group(1)
    assert "/readyz" not in healthcheck.group(1)


# -- the entrypoint --------------------------------------------------------


def test_the_entrypoint_is_valid_posix_sh() -> None:
    assert (
        subprocess.run(["sh", "-n", str(ROOT / "deploy" / "docker-entrypoint.sh")]).returncode == 0
    )


@pytest.mark.skipif(
    sys.platform == "win32" and not Path("/usr/bin/sh").exists(), reason="needs a POSIX shell"
)
def test_it_refuses_production_without_a_secret_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Four workers each generating their own key invalidate each other's
    sessions, and the symptom is users logged out at random.
    """
    result = subprocess.run(
        ["sh", str(ROOT / "deploy" / "docker-entrypoint.sh"), "true"],
        env={"MASSINGPLAN_ENV": "production", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 1
    assert "MASSINGPLAN_SECRET_KEY is not set" in result.stderr


def test_the_migration_step_retries() -> None:
    """Postgres accepts TCP connections before it will serve a query, so a
    single attempt loses the race about one boot in five.
    """
    assert "until alembic upgrade head" in ENTRYPOINT
    assert "attempt" in ENTRYPOINT
    assert "MASSINGPLAN_SKIP_MIGRATIONS" in ENTRYPOINT


# -- compose ---------------------------------------------------------------


def test_the_app_waits_for_the_database_to_be_healthy_not_merely_started() -> None:
    assert COMPOSE["services"]["app"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert "pg_isready" in COMPOSE["services"]["db"]["healthcheck"]["test"][-1]


def test_the_compose_secret_is_obviously_a_placeholder() -> None:
    """A plausible-looking default is one somebody ships."""
    secret = COMPOSE["services"]["app"]["environment"]["MASSINGPLAN_SECRET_KEY"]
    assert "change-me" in secret


def test_the_database_has_a_named_volume_so_a_rebuild_does_not_erase_it() -> None:
    assert "massingplan_db" in COMPOSE["volumes"]


# -- gunicorn --------------------------------------------------------------


def test_gunicorn_does_not_trust_every_proxy() -> None:
    """`*` lets any client spoof its own source address -- which is what the
    audit log records.
    """
    namespace: dict[str, object] = {}
    exec(  # noqa: S102 - reading our own config the way gunicorn does
        (ROOT / "deploy" / "gunicorn.conf.py").read_text(encoding="utf-8"), namespace
    )
    assert namespace["forwarded_allow_ips"] != "*"
    # A 2,000-iteration Monte Carlo is CPU-bound; the 30s default kills it
    # mid-simulation and returns nothing.
    assert int(namespace["timeout"]) >= 120  # type: ignore[call-overload]
    assert int(namespace["max_requests"]) > 0  # type: ignore[call-overload]


# -- CI --------------------------------------------------------------------


def test_ci_runs_the_gates_this_repo_claims_to_have() -> None:
    expected = {
        "lint",
        "test",
        "postgres",
        "migrations",
        "timeaxis-kernel",
        "determinism",
        "purity",
        "offline",
        "no-adapters",
        "imports",
        "security",
        "docker",
        "codeql",
    }
    assert expected <= set(CI["jobs"]), f"missing CI jobs: {sorted(expected - set(CI['jobs']))}"


def test_the_postgres_job_fails_if_its_tests_silently_skipped() -> None:
    """`-q` prints "3 passed, 2 skipped" just as happily either way. Without the
    guard the job is green whether or not the database was ever reached.
    """
    # Asserted against the raw file, not a re-dump: `yaml.dump` re-quotes the
    # shell inside a `run:` block and the strings stop matching.
    assert "did not silently skip" in CI_TEXT
    assert "MASSINGPLAN_TEST_POSTGRES_URL" in CI_TEXT


def test_the_docker_job_checks_readiness_not_just_a_200() -> None:
    assert "/readyz" in CI_TEXT
    assert "booted in production mode with no secret key" in CI_TEXT


def test_the_readiness_grep_actually_matches_what_readyz_returns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Assert the pattern *matches*, not that it exists in the file.

    This test used to be `assert '"status": "ready"' in CI_TEXT` — which passed
    for as long as the workflow contained that string, and said nothing about
    whether it matched. It did not: Flask serialises compactly outside debug, so
    the body is `{"status":"ready"}` with no space, and the docker job failed
    against a container that was working perfectly.

    A test that checks a grep pattern is spelled a certain way is a test of the
    spelling. This one runs the pattern over the real response.
    """
    import re

    from massingplan import database
    from massingplan.app import create_app
    from massingplan.config import Settings

    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'ready.db'}",
        )
    )
    database.create_all()
    body = application.test_client().get("/readyz").get_data(as_text=True)

    # The same pattern the workflow greps with, as an ERE.
    pattern = re.search(r"grep -Eq '([^']+)'", CI_TEXT)
    assert pattern is not None, "the docker job no longer greps /readyz"
    assert re.search(pattern.group(1), body), (
        f"the workflow greps for {pattern.group(1)!r}, which does not match "
        f"the actual /readyz body {body!r}"
    )
