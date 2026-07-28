import os

from funding_arb_monitor.container import drop_privileges


def test_container_launcher_drops_root_privileges(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("groups", groups)))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("uid", uid)))

    drop_privileges()

    assert calls == [("groups", []), ("gid", 10_001), ("uid", 10_001)]


def test_container_launcher_keeps_an_existing_non_root_identity(monkeypatch) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 10_001)
    monkeypatch.setattr(
        os, "setuid", lambda uid: (_ for _ in ()).throw(AssertionError(uid))
    )

    drop_privileges()
