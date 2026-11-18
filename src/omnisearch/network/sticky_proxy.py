import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

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
        self._last_egress_check = 0.0
        self._rotation_cursor = 0
        self._rotation_lock = threading.Lock()

    def resolve(self, reference: str) -> dict[str, str]:
        if reference != "omnisearch://sticky-proxy":
            raise StickyProxyUnavailable(f"unknown managed proxy reference: {reference}")
        self.ensure()
        return {"server": f"http://127.0.0.1:{self.proxy_port}"}

    def ensure(self) -> None:
        if not self.enabled:
            raise StickyProxyUnavailable("the sticky proxy is disabled")
        if self._healthy() and self._egress_healthy():
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
                if self._healthy() and self._egress_healthy():
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
            if self._healthy() and self._egress_healthy():
                return
            if self._healthy():
                self._stop()
            for _attempt in range(2):
                self._prepare_runtime(source)
                self._start()
                for _ in range(50):
                    if self._healthy():
                        break
                    time.sleep(0.1)
                if self._healthy() and self._egress_healthy(force=True):
                    return
                self._stop()
            raise StickyProxyUnavailable(
                "the private Mihomo process has no healthy internet egress"
            )
        finally:
            lock.unlink(missing_ok=True)

    def rotate(self, reason: str = "provider_challenge") -> str | None:
        """Rotate only the private selector after a route-specific challenge."""
        del reason  # reserved for future telemetry without leaking query data
        with self._rotation_lock:
            self.ensure()
            try:
                response = httpx.get(
                    f"http://127.0.0.1:{self.controller_port}/proxies",
                    timeout=2.0,
                    trust_env=False,
                )
                response.raise_for_status()
                proxies = response.json().get("proxies", {})
            except (httpx.HTTPError, ValueError) as error:
                raise StickyProxyUnavailable("cannot inspect private proxy routes") from error
            group_name = next(
                (
                    name
                    for name, item in proxies.items()
                    if item.get("type") == "Selector" and "TAGSS" in name
                ),
                None,
            )
            if group_name is None:
                raise StickyProxyUnavailable("private proxy selector was not found")
            group = proxies[group_name]
            current = group.get("now")
            blocked_labels = ("官网", "流量", "剩余", "到期", "重置", "套餐")
            candidates = [
                name
                for name in group.get("all", [])
                if name != current
                and not any(label in name for label in blocked_labels)
                and (proxies.get(name) or {}).get("type")
                not in {"Selector", "URLTest", "Fallback", "LoadBalance", "Direct", "Reject"}
            ]
            if not candidates:
                raise StickyProxyUnavailable("private proxy selector has no alternate nodes")
            start = self._rotation_cursor % len(candidates)
            ordered = candidates[start:] + candidates[:start]
            for offset, candidate in enumerate(ordered[:6], start=1):
                try:
                    result = httpx.put(
                        f"http://127.0.0.1:{self.controller_port}/proxies/"
                        f"{quote(group_name, safe='')}",
                        json={"name": candidate},
                        timeout=2.0,
                        trust_env=False,
                    )
                    result.raise_for_status()
                except httpx.HTTPError:
                    continue
                self._last_egress_check = 0.0
                if self._egress_healthy(force=True, timeout=2.0):
                    self._rotation_cursor = start + offset
                    return candidate
            raise StickyProxyUnavailable("no alternate private proxy node has healthy egress")

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

    def _stop(self) -> None:
        pid_path = self.runtime_dir / "mihomo.pid"
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, ValueError):
            return
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            check=False,
        )
        pid_path.unlink(missing_ok=True)
        self._last_egress_check = 0.0

    def _egress_healthy(self, *, force: bool = False, timeout: float = 3.0) -> bool:
        now = time.monotonic()
        if not force and now - self._last_egress_check < 10:
            return True
        try:
            response = httpx.get(
                "https://www.google.com/generate_204",
                proxy=f"http://127.0.0.1:{self.proxy_port}",
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            )
            healthy = response.status_code in {200, 204}
        except httpx.HTTPError:
            healthy = False
        if healthy:
            self._last_egress_check = now
        return healthy

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

