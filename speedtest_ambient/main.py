from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from itertools import chain
import json
from operator import methodcaller
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any, NamedTuple, Optional

from ambient import Ambient

_Dict = dict[str, Any]

_GLOBAL_V4_ADDRESS = "192.0.2.0"
_GLOBAL_V6_ADDRESS = "2001:db8::"

_TIMEZONE_OFFSET = timedelta(hours=9)
_TIMEZONE = timezone(_TIMEZONE_OFFSET)


def _command(*args: str):
    return subprocess.run(
        args,
        capture_output=True,
        check=True,
        text=True,
    )


def run():
    global config

    with open(Path.home() / ".config" / "speedtest-ambient.toml", "rb") as f:
        config = tomllib.load(f)
    ip_addresses = _get_ip_addresses()
    results = map(_speed_test, ip_addresses)
    _ambient(results)


def _get_ip_addresses() -> Iterator[str]:
    p = _command("ip", "--json", "address", "show", "scope", "global")
    ifs: list[_Dict] = json.loads(p.stdout)
    aigetter: Callable[[_Dict], list[_Dict]] = methodcaller("get", "addr_info", [])
    v4addresses, v6addresses = _separate_family(chain.from_iterable(map(aigetter, ifs)))
    return _sieve_addresses(v4addresses, v6addresses)


def _sieve_addresses(
    v4addresses: Iterable[str], v6addresses: Iterable[str]
) -> Iterator[str]:
    yield from _sieve_addresses_family(v4addresses, v4=True)
    yield from _sieve_addresses_family(v6addresses, v4=False)


def _sieve_addresses_family(addresses: Iterable[str], v4: bool) -> Iterator[str]:
    if not addresses:
        return
    p = _command(
        "ip", "route", "get", (_GLOBAL_V4_ADDRESS if v4 else _GLOBAL_V6_ADDRESS)
    )
    for address in addresses:
        if re.search(
            rf"""
                \b
                {address}
                \b
            """,
            p.stdout,
            re.VERBOSE,
        ):
            yield address


def _separate_family(addr_infos: Iterable[_Dict]) -> tuple[list[str], list[str]]:
    v4addresses, v6addresses = [], []
    for addr_info in addr_infos:
        match addr_info.get("family"), addr_info.get("local"):
            case "inet", addr:
                v4addresses.append(addr)
            case "inet6", addr:
                v6addresses.append(addr)
    return v4addresses, v6addresses


class _SpeedTestResult(NamedTuple):
    timestamp: datetime
    latency_ms: float
    jitter_ms: float
    download_bytesps: int
    upload_bytesps: int
    packet_loss: Optional[float]

    def to_ambient(self) -> dict[str, Any]:
        timestamp_timezone = self.timestamp.astimezone(_TIMEZONE)
        created = timestamp_timezone.strftime("%Y-%m-%d %H:%M:%S")
        return dict(
            ((f"d{i}", d) for i, d in enumerate(self._to_ambient_data(), 1)),
            created=created,
        )

    def _to_ambient_data(self) -> Iterator[Any]:
        yield self.latency_ms
        yield self.jitter_ms
        yield self.download_bytesps * 8 / 1000 / 1000
        yield self.upload_bytesps * 8 / 1000 / 1000
        if self.packet_loss is not None:
            yield self.packet_loss


def _speed_test(ip_address: str) -> _SpeedTestResult:
    p = _command("speedtest", "--format", "json", "--ip", ip_address)
    result: dict[str, Any] = json.loads(p.stdout)
    return _SpeedTestResult(
        datetime.fromisoformat(result["timestamp"]),
        result["ping"]["latency"],
        result["ping"]["jitter"],
        result["download"]["bandwidth"],
        result["upload"]["bandwidth"],
        result.get("packetLoss"),
    )


def _ambient(results: Iterable[_SpeedTestResult]):
    config_it = iter(config["ambient"]["channels"])
    for result in results:
        config_am = next(config_it)
        am = Ambient(config_am["id"], config_am["write_key"])
        response = am.send(result.to_ambient())
        response.raise_for_status()
