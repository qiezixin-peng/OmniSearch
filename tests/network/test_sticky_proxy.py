from pathlib import Path
from time import time

import yaml

from omnisearch.network.sticky_proxy import StickyProxyManager


def test_prepare_runtime_creates_isolated_loopback_config(tmp_path: Path) -> None:
    clash = tmp_path / "clash"
    clash.mkdir()
    (clash / "clash-verge.yaml").write_text(
        yaml.safe_dump(
            {
                "mixed-port": 7897,
                "external-controller-pipe": r"\\.\pipe\mihomo",
                "allow-lan": True,
                "tun": {"enable": True},
                "dns": {"enable": True, "listen": "0.0.0.0:7874"},
                "proxies": [{"name": "node", "type": "direct"}],
                "proxy-groups": [{"name": "Proxy", "type": "select", "proxies": ["node"]}],
                "rules": ["MATCH,Proxy"],
            }
        ),
        encoding="utf-8",
    )
    manager = StickyProxyManager(
        runtime_dir=tmp_path / "runtime",
        clash_config_dir=clash,
        mihomo_executable=tmp_path / "mihomo.exe",
    )

    manager.runtime_dir.mkdir(parents=True)
    manager._prepare_runtime(clash / "clash-verge.yaml")

    generated = yaml.safe_load((manager.runtime_dir / "config.yaml").read_text(encoding="utf-8"))
    assert generated["mixed-port"] == 17897
    assert generated["external-controller"] == "127.0.0.1:19097"
    assert generated["bind-address"] == "127.0.0.1"
    assert generated["allow-lan"] is False
    assert generated["tun"]["enable"] is False
    assert generated["dns"]["listen"] == "127.0.0.1:17853"
    assert "external-controller-pipe" not in generated


def test_ensure_rotates_away_from_dead_initial_route(tmp_path: Path, monkeypatch) -> None:
    clash = tmp_path / "clash"
    clash.mkdir()
    (clash / "clash-verge.yaml").write_text("proxies: []\n", encoding="utf-8")
    executable = tmp_path / "mihomo.exe"
    executable.touch()
    manager = StickyProxyManager(
        runtime_dir=tmp_path / "runtime",
        clash_config_dir=clash,
        mihomo_executable=executable,
    )
    rotations: list[bool] = []
    monkeypatch.setattr(manager, "_healthy", lambda: True)
    monkeypatch.setattr(manager, "_egress_healthy", lambda **_kwargs: False)
    monkeypatch.setattr(manager, "_stop", lambda: None)
    monkeypatch.setattr(manager, "_prepare_runtime", lambda _source: None)
    monkeypatch.setattr(manager, "_start", lambda: None)
    monkeypatch.setattr(
        manager,
        "_rotate_unlocked",
        lambda: rotations.append(True) or "working-node",
    )

    manager.ensure()

    assert rotations == [True]


def test_provider_challenge_quarantine_is_persistent_and_scoped(tmp_path: Path) -> None:
    manager = StickyProxyManager(
        runtime_dir=tmp_path / "runtime",
        clash_config_dir=tmp_path / "clash",
        mihomo_executable=tmp_path / "mihomo.exe",
        route_quarantine_seconds=600,
    )

    manager._quarantine_route("google", "node-a")

    assert manager._active_quarantined_routes("google") == {"node-a"}
    assert manager._active_quarantined_routes("duckduckgo") == set()
    quarantine = manager.runtime_dir / "route-quarantine.json"
    assert quarantine.is_file()
    assert quarantine.stat().st_mtime <= time()
