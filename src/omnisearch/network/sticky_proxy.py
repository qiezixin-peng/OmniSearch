import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import yaml


class StickyProxyUnavailable(RuntimeError):
    pass


class StickyProxyManager:
    """Create a private Mihomo listener whose selection cannot change mid-session.

    Clash Verge may keep selecting the lowest-latency node globally. We snapshot
    its merged configuration and selection cache into a separate Mihomo data
    directory. The private process therefore keeps the selected outbound stable
    without changing the user's global proxy policy.
    """

    def __init__(
        self,
        *,
        runtime_dir: Path,
        clash_config_dir: Path,
        mihomo_executable: Path,
        proxy_port: int = 17897,
        controller_port: int = 19097,
        enabled: bool = True,
    ) -> None:
        self.runtime_dir = Path(runtime_dir) / "sticky-proxy"
        self.clash_config_dir = Path(clash_config_dir).expanduser().resolve()
        self.mihomo_executable = Path(mihomo_executable).expanduser().resolve()
        self.proxy_port = proxy_port
        self.controller_port = controller_port
        self.enabled = enabled

    def resolve(self, reference: str) -> dict[str, str]:
        if reference != "omnisearch://sticky-proxy":
            raise StickyProxyUnavailable(f"unknown managed proxy reference: {reference}")
        self.ensure()
        return {"server": f"http://127.0.0.1:{self.proxy_port}"}

    def ensure(self) -> None:
        if not self.enabled:
            raise StickyProxyUnavailable("the sticky proxy is disabled")
        if self._healthy():
            return
        if not self.mihomo_executable.is_file():
            raise StickyProxyUnavailable("the Mihomo executable is unavailable")
        source = self.clash_config_dir / "clash-verge.yaml"
        if not source.is_file():
            raise StickyProxyUnavailable("the Clash Verge merged configuration is unavailable")

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        lock = self.runtime_dir / "startup.lock"
        deadline = time.monotonic() + 10
        while True:
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(descriptor)
                break
            except FileExistsError:
                if self._healthy():
                    return
                if time.monotonic() >= deadline:
                    try:
                        if time.time() - lock.stat().st_mtime > 30:
                            lock.unlink()
                            continue
                    except FileNotFoundError:
                        continue
                    raise StickyProxyUnavailable(
                        "timed out waiting for sticky proxy startup"
                    ) from None
                time.sleep(0.2)
        try:
            if self._healthy():
                return
            self._prepare_runtime(source)
            self._start()
            for _ in range(50):
                if self._healthy():
                    return
                time.sleep(0.1)
            raise StickyProxyUnavailable("the private Mihomo process did not become healthy")
        finally:
            lock.unlink(missing_ok=True)

    def _prepare_runtime(self, source: Path) -> None:
        config = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise StickyProxyUnavailable("the Clash Verge configuration is invalid")
        for key in (
            "port",
            "socks-port",
            "redir-port",
            "tproxy-port",
            "external-controller-pipe",
            "external-controller-unix",
            "external-controller-tls",
            "listeners",
        ):
            config.pop(key, None)
        config.update(
            {
                "mixed-port": self.proxy_port,
                "external-controller": f"127.0.0.1:{self.controller_port}",
                "secret": "",
                "allow-lan": False,
                "bind-address": "127.0.0.1",
                "log-level": "warning",
            }
        )
        tun = config.get("tun")
        if isinstance(tun, dict):
            tun["enable"] = False
        dns = config.get("dns")
        if isinstance(dns, dict) and dns.get("listen"):
            dns["listen"] = "127.0.0.1:17853"

        target = self.runtime_dir / "config.yaml"
        target.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        cache = self.clash_config_dir / "cache.db"
        if cache.is_file():
            shutil.copy2(cache, self.runtime_dir / "cache.db")
        for name in ("Country.mmdb", "ASN.mmdb", "geoip.dat", "geosite.dat"):
            source_data = self.clash_config_dir / name
            target_data = self.runtime_dir / name
            if source_data.is_file() and not target_data.exists():
                try:
                    os.link(source_data, target_data)
                except OSError:
                    shutil.copy2(source_data, target_data)

    def _start(self) -> None:
        log_path = self.runtime_dir / "mihomo.log"
        log = log_path.open("ab")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                [
                    str(self.mihomo_executable),
                    "-d",
                    str(self.runtime_dir),
                    "-f",
                    str(self.runtime_dir / "config.yaml"),
                ],
                cwd=self.runtime_dir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
                close_fds=True,
            )
            (self.runtime_dir / "mihomo.pid").write_text(
                str(process.pid), encoding="ascii"
            )
        finally:
            log.close()

    def _healthy(self) -> bool:
        try:
            response = httpx.get(
                f"http://127.0.0.1:{self.controller_port}/version",
                timeout=0.5,
                trust_env=False,
            )
            return response.status_code == 200 and "version" in response.json()
        except (httpx.HTTPError, ValueError):
            return False

