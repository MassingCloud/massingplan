"""Security primitives, the CLI, and the three adapter seams.

The seam tests are the ones that matter: they defend the promise that standalone
is the product rather than a mode, and that deleting every optional adapter
leaves a working install.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from massingplan import security
from massingplan.cli import main
from massingplan.services import entitlement, identity, storage

# -- security --------------------------------------------------------------


def test_a_generated_key_carries_its_prefix_and_only_its_hash_is_storable() -> None:
    key, key_hash = security.generate_api_key()
    assert key.startswith(security.API_KEY_PREFIX)
    assert key not in key_hash
    assert security.verify_api_key(key, key_hash)
    assert not security.verify_api_key(key + "x", key_hash)


def test_two_keys_never_collide() -> None:
    keys = {security.generate_api_key()[0] for _ in range(200)}
    assert len(keys) == 200


def test_a_webhook_signature_round_trips_and_rejects_a_tampered_body() -> None:
    payload = b'{"event":"schedule.recalculated"}'
    signature = security.sign(payload, "shared-secret")
    assert security.verify_signature(payload, signature, "shared-secret")
    assert not security.verify_signature(payload + b" ", signature, "shared-secret")
    assert not security.verify_signature(payload, signature, "other-secret")


def test_a_timestamped_signature_expires() -> None:
    """Without the timestamp inside the signed material, a captured body replays
    forever -- the signature stays valid because the body did not change.
    """
    payload = b"body"
    header = security.sign_with_timestamp(payload, "s", now=1_000_000)
    assert security.verify_timestamped(payload, header, "s", now=1_000_000)
    assert security.verify_timestamped(payload, header, "s", now=1_000_200)
    assert not security.verify_timestamped(payload, header, "s", now=1_000_400)


def test_a_timestamp_far_in_the_future_is_rejected_too() -> None:
    """Only checking one side lets a forged future stamp replay indefinitely."""
    header = security.sign_with_timestamp(b"body", "s", now=2_000_000)
    assert not security.verify_timestamped(b"body", header, "s", now=1_000_000)


def test_a_malformed_signature_header_is_false_not_an_exception() -> None:
    for header in ("", "garbage", "t=abc,v1=xx", "v1=only", "t=1"):
        assert security.verify_timestamped(b"body", header, "s", now=1) is False


def test_the_timestamp_is_covered_by_the_signature() -> None:
    """Editing the stamp to keep an old request alive must break the signature."""
    header = security.sign_with_timestamp(b"body", "s", now=1_000_000)
    forged = header.replace("t=1000000", "t=1000300")
    assert not security.verify_timestamped(b"body", forged, "s", now=1_000_300)


# -- adapter seams ---------------------------------------------------------


def test_the_default_entitlement_allows_everything() -> None:
    """Standalone is the product, not a trial. Metering a self-hoster who has
    the source is theatre, and code that exists to be bypassed rots.
    """
    current = entitlement.resolve("standalone").current()
    assert current.entitled and current.status == "active"
    assert current.allows("anything_at_all")
    assert current.seats["limit"] == entitlement.UNLIMITED


def test_the_entitlement_shape_matches_the_massing_cloud_convention() -> None:
    """Field for field, so a massing.cloud adapter fills the same dataclass and
    nothing above the seam changes.
    """
    assert set(entitlement.resolve("standalone").current().to_dict()) == {
        "tier",
        "entitled",
        "status",
        "expires_at",
        "seats",
        "limits",
    }


def test_an_absent_limit_means_unmetered_not_denied() -> None:
    """Treating an unknown key as a denial makes every new feature invisible to
    every existing deployment until its config is updated.
    """
    e = entitlement.Entitlement(tier="t", entitled=True, status="active", limits={"seats": 0})
    assert e.allows("a_feature_nobody_configured")
    assert not e.allows("seats")


def test_selecting_a_missing_adapter_fails_with_an_actionable_message() -> None:
    for resolve, backend, hint in (
        (entitlement.resolve, "massing_cloud", "massingplan[oidc]"),
        (identity.resolve, "oidc", "massingplan[oidc]"),
        (storage.resolve, "s3", "massingplan[s3]"),
    ):
        with pytest.raises(entitlement.AdapterUnavailableError) as excinfo:
            resolve(backend)
        assert hint in str(excinfo.value)


def test_an_unknown_backend_name_lists_the_valid_ones() -> None:
    with pytest.raises(entitlement.AdapterUnavailableError, match="standalone"):
        entitlement.resolve("nonsense")


def test_no_optional_adapter_is_imported_by_a_default_install() -> None:
    """The `no-adapters` CI job deletes these files and re-runs the suite. This
    is the same promise, checked in-process.
    """
    import sys

    leaked = [
        name
        for name in sys.modules
        if name.endswith((".massing_cloud", ".oidc", ".s3")) and name.startswith("massingplan.")
    ]
    assert leaked == []


def test_the_local_identity_provider_authenticates_by_key(tmp_path: Path) -> None:
    key, key_hash = security.generate_api_key()
    provider = identity.LocalIdentityProvider({"planner": key_hash})
    principal = provider.authenticate({"api_key": key})
    assert principal is not None
    assert principal.subject == "planner"
    assert principal.has_role("scheduler")
    assert provider.authenticate({"api_key": "mpln_wrong"}) is None
    assert provider.authenticate({}) is None


def test_describing_the_identity_provider_leaks_no_secret() -> None:
    _key, key_hash = security.generate_api_key()
    described = json.dumps(identity.LocalIdentityProvider({"a": key_hash}).describe())
    assert key_hash not in described


# -- storage ---------------------------------------------------------------


def test_local_storage_round_trips_and_records_a_digest(tmp_path: Path) -> None:
    backend = storage.LocalStorage(tmp_path)
    pointer = backend.put("imports/tower.xer", b"ERMHDR\t...", content_type="text/plain")
    assert pointer.size == 10
    assert backend.get(pointer) == b"ERMHDR\t..."
    backend.delete(pointer)
    assert not (tmp_path / "imports/tower.xer").exists()


def test_a_pointer_is_never_a_url(tmp_path: Path) -> None:
    """A pointer that is a URL is a capability, and one that reaches a log is an
    unauthenticated download link with no expiry.
    """
    pointer = storage.LocalStorage(tmp_path).put("k", b"x")
    assert "http" not in json.dumps(pointer.to_dict())


def test_a_traversing_key_is_refused(tmp_path: Path) -> None:
    backend = storage.LocalStorage(tmp_path / "root")
    with pytest.raises(ValueError, match="escapes the storage root"):
        backend.put("../../etc/passwd", b"x")


def test_corruption_is_caught_on_read_not_weeks_later(tmp_path: Path) -> None:
    """Silent corruption in a stored schedule would surface as a parse error
    much later, pointing at the parser.
    """
    backend = storage.LocalStorage(tmp_path)
    pointer = backend.put("k", b"original")
    (tmp_path / "k").write_bytes(b"tampered")
    with pytest.raises(OSError, match="does not match its recorded digest"):
        backend.get(pointer)


# -- the CLI ---------------------------------------------------------------


def test_the_declared_console_script_entry_point_exists() -> None:
    """`pyproject` declares `massingplan = massingplan.cli:main`. If that import
    path is wrong, `pip install` produces a console script that crashes.
    """
    import tomllib

    root = Path(__file__).resolve().parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["scripts"]
    module, _, attr = declared["massingplan"].partition(":")
    imported = __import__(module, fromlist=[attr])
    assert callable(getattr(imported, attr))


def test_check_reports_the_resolved_config_and_exits_zero(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "entitlement" in out and "standalone" in out
    assert "engine" in out


def test_check_never_prints_the_secret_key(capsys) -> None:  # type: ignore[no-untyped-def]
    import os

    os.environ["MASSINGPLAN_SECRET_KEY"] = "super-secret-value"
    try:
        main(["check"])
        assert "super-secret-value" not in capsys.readouterr().out
    finally:
        del os.environ["MASSINGPLAN_SECRET_KEY"]


def test_demo_prints_a_json_assessment(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["health"]["grade"] in "ABCDF"


def test_schedule_prints_a_table_from_a_json_network(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    network = tmp_path / "net.json"
    network.write_text(
        json.dumps(
            {
                "data_date": "2026-06-01",
                "activities": [
                    {"id": "A", "duration_days": 5},
                    {"id": "B", "duration_days": 3, "predecessors": ["A"]},
                ],
            }
        )
    )
    assert main(["schedule", str(network)]) == 0
    out = capsys.readouterr().out
    assert "2026-06-05" in out and "2026-06-08" in out


def test_schedule_reports_a_bad_network_and_exits_non_zero(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    network = tmp_path / "bad.json"
    network.write_text(
        json.dumps(
            {
                "activities": [
                    {"id": "A", "duration_days": 1, "predecessors": ["B"]},
                    {"id": "B", "duration_days": 1, "predecessors": ["A"]},
                ]
            }
        )
    )
    assert main(["schedule", str(network)]) == 1
    assert "circular logic" in capsys.readouterr().err


def test_assess_exits_non_zero_when_a_check_fails(tmp_path: Path) -> None:
    """So it works as a build gate rather than only as a report."""
    network = tmp_path / "net.json"
    network.write_text(
        json.dumps(
            {
                "data_date": "2026-06-01",
                "activities": [
                    {"id": "A", "duration_days": 5},
                    {"id": "ORPHAN", "duration_days": 5},
                    {
                        "id": "B",
                        "duration_days": 3,
                        "predecessors": [{"id": "A", "type": "SS", "lag_days": 4}],
                    },
                ],
            }
        )
    )
    assert main(["assess", str(network)]) == 1
