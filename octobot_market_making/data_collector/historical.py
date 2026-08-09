"""Resumable public historical-data backfill for market-making research."""

import argparse
import concurrent.futures
import dataclasses
import datetime
import hashlib
from html.parser import HTMLParser
import json
import os
import pathlib
import shutil
import time
import typing
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree


SYMBOLS = ("BCHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "LTCUSDT")
BINANCE_BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BINANCE_DOWNLOAD = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/"
BYBIT_DOWNLOAD = "https://public.bybit.com/trading/"
HTX_LIST = "https://www.htx.com/data"
HTX_DOWNLOAD = "https://www.htx.com/data/"
USER_AGENT = "octobot-market-data/1"

# Individual trades and high-frequency book data are retained. Aggregate trades are also
# kept because they are a convenient common representation for cross-venue research.
BINANCE_MONTHLY_SPECS = (
    ("aggTrades", None),
    ("bookTicker", None),
    ("fundingRate", None),
    ("trades", None),
    ("klines", "1m"),
    ("indexPriceKlines", "1m"),
    ("markPriceKlines", "1m"),
    ("premiumIndexKlines", "1m"),
)
BINANCE_DAILY_ONLY_SPECS = (("bookDepth", None), ("metrics", None))
BINANCE_RECENT_DAILY_SPECS = tuple(
    spec for spec in BINANCE_MONTHLY_SPECS if spec[0] != "fundingRate"
)


@dataclasses.dataclass(frozen=True)
class RemoteFile:
    source: str
    url: str
    relative_path: pathlib.PurePosixPath
    size: typing.Optional[int] = None
    checksum_url: typing.Optional[str] = None


@dataclasses.dataclass(frozen=True)
class DownloadResult:
    status: str
    relative_path: str
    bytes_downloaded: int = 0
    error: typing.Optional[str] = None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, typing.Optional[str]]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def _request(
    url: str,
    *,
    headers: typing.Optional[dict[str, str]] = None,
    method: str = "GET",
):
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=request_headers, method=method), timeout=60
    )


def _remote_size(url: str) -> typing.Optional[int]:
    with _request(url, method="HEAD") as response:
        content_length = response.headers.get("Content-Length")
    return int(content_length) if content_length else None


def _s3_files(prefix: str) -> list[RemoteFile]:
    files = []
    continuation_token = None
    while True:
        query = {"list-type": "2", "max-keys": 1000, "prefix": prefix}
        if continuation_token:
            query["continuation-token"] = continuation_token
        url = f"{BINANCE_BUCKET}?{urllib.parse.urlencode(query)}"
        with _request(url) as response:
            root = ElementTree.parse(response).getroot()
        namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        for content in root.findall("s3:Contents", namespace):
            key = content.findtext("s3:Key", namespaces=namespace)
            size = content.findtext("s3:Size", namespaces=namespace)
            if not key or not key.endswith(".zip"):
                continue
            download_url = urllib.parse.urljoin(
                BINANCE_DOWNLOAD, urllib.parse.quote(key, safe="/")
            )
            files.append(
                RemoteFile(
                    "binance",
                    download_url,
                    pathlib.PurePosixPath("binance") / key,
                    int(size) if size is not None else None,
                    f"{download_url}.CHECKSUM",
                )
            )
        continuation_token = root.findtext(
            "s3:NextContinuationToken", namespaces=namespace
        )
        if not continuation_token:
            return files


def _binance_prefix(period: str, dataset: str, symbol: str, interval: typing.Optional[str]) -> str:
    prefix = f"data/futures/um/{period}/{dataset}/{symbol}/"
    return f"{prefix}{interval}/" if interval else prefix


def _recent_dates(days: int) -> list[datetime.date]:
    yesterday = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=1)
    return [yesterday - datetime.timedelta(days=offset) for offset in range(days)]


def _recent_binance_file(
    dataset: str,
    interval: typing.Optional[str],
    symbol: str,
    day: datetime.date,
) -> RemoteFile:
    prefix = _binance_prefix("daily", dataset, symbol, interval)
    suffix = f"-{interval}" if interval else f"-{dataset}"
    key = f"{prefix}{symbol}{suffix}-{day:%Y-%m-%d}.zip"
    download_url = urllib.parse.urljoin(BINANCE_DOWNLOAD, key)
    return RemoteFile(
        "binance",
        download_url,
        pathlib.PurePosixPath("binance") / key,
        checksum_url=f"{download_url}.CHECKSUM",
    )


def discover_binance(recent_days: int) -> list[RemoteFile]:
    files = []
    for symbol in SYMBOLS:
        for dataset, interval in BINANCE_MONTHLY_SPECS:
            files.extend(
                _s3_files(_binance_prefix("monthly", dataset, symbol, interval))
            )
        for dataset, interval in BINANCE_DAILY_ONLY_SPECS:
            files.extend(_s3_files(_binance_prefix("daily", dataset, symbol, interval)))
        for dataset, interval in BINANCE_RECENT_DAILY_SPECS:
            files.extend(
                _recent_binance_file(dataset, interval, symbol, day)
                for day in _recent_dates(recent_days)
            )
    return files


def discover_bybit() -> list[RemoteFile]:
    files = []
    for symbol in SYMBOLS:
        directory_url = urllib.parse.urljoin(BYBIT_DOWNLOAD, f"{symbol}/")
        with _request(directory_url) as response:
            body = response.read().decode("utf-8", errors="replace")
        parser = _LinkParser()
        parser.feed(body)
        for link in parser.links:
            filename = pathlib.PurePosixPath(urllib.parse.urlparse(link).path).name
            if not filename.startswith(symbol) or not filename.endswith(".csv.gz"):
                continue
            files.append(
                RemoteFile(
                    "bybit",
                    urllib.parse.urljoin(directory_url, link),
                    pathlib.PurePosixPath("bybit/trading") / symbol / filename,
                )
            )
    return files


def _htx_remote_file(key: str, size: typing.Optional[int]) -> RemoteFile:
    download_url = urllib.parse.urljoin(
        HTX_DOWNLOAD, urllib.parse.quote(key, safe="/")
    )
    return RemoteFile(
        "htx",
        download_url,
        pathlib.PurePosixPath("htx") / key,
        size,
        f"{download_url.removesuffix('.zip')}.CHECKSUM",
    )


def _htx_files(prefix: str) -> list[RemoteFile]:
    files = []
    marker = None
    while True:
        query = {"delimiter": "/", "max-keys": 1000, "prefix": prefix}
        if marker:
            query["marker"] = marker
        url = f"{HTX_LIST}?{urllib.parse.urlencode(query)}"
        with _request(url) as response:
            root = ElementTree.parse(response).getroot()
        namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        for content in root.findall("s3:Contents", namespace):
            key = content.findtext("s3:Key", namespaces=namespace)
            size = content.findtext("s3:Size", namespaces=namespace)
            if not key or not key.endswith(".zip"):
                continue
            files.append(
                _htx_remote_file(key, int(size) if size is not None else None)
            )
        marker = root.findtext("s3:NextMarker", namespaces=namespace)
        if not marker:
            return files


def discover_htx() -> list[RemoteFile]:
    files = []
    for symbol in SYMBOLS:
        contract = symbol.removesuffix("USDT") + "-USDT"
        files.extend(
            _htx_files(f"data/trades/linear-swap/daily/{contract}/")
        )
        files.extend(
            _htx_files(f"data/klines/linear-swap/daily/{contract}/1min/")
        )
    return files


def _remote_checksum(url: str) -> str:
    with _request(url) as response:
        text = response.read().decode("ascii").strip()
    checksum = text.split()[0]
    if len(checksum) != 64:
        raise ValueError(f"Unexpected checksum response from {url}")
    return checksum.lower()


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        while chunk := input_file.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _download_once(remote: RemoteFile, output_dir: pathlib.Path) -> DownloadResult:
    destination = output_dir / remote.relative_path
    if destination.is_file() and (remote.size is None or destination.stat().st_size == remote.size):
        return DownloadResult("skipped", str(remote.relative_path))

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.part")
    if destination.exists() and not partial.exists():
        destination.replace(partial)
    partial_size = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={partial_size}-"} if partial_size else {}
    expected_size = remote.size

    if not (expected_size is not None and partial_size == expected_size):
        try:
            with _request(remote.url, headers=headers) as response:
                response_status = getattr(response, "status", 200)
                append = bool(partial_size and response_status == 206)
                if not append:
                    partial_size = 0
                content_length = response.headers.get("Content-Length")
                if expected_size is None and content_length:
                    expected_size = partial_size + int(content_length)
                with partial.open("ab" if append else "wb") as output_file:
                    while chunk := response.read(1024 * 1024):
                        output_file.write(chunk)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return DownloadResult("missing", str(remote.relative_path))
            if error.code != 416:
                raise
            server_size = _remote_size(remote.url)
            if server_size == partial_size:
                expected_size = server_size
            else:
                partial.write_bytes(b"")
                return _download_once(
                    dataclasses.replace(remote, size=server_size), output_dir
                )

    actual_size = partial.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise IOError(
            f"Size mismatch for {remote.relative_path}: {actual_size} != {expected_size}"
        )
    if remote.checksum_url:
        expected_checksum = _remote_checksum(remote.checksum_url)
        actual_checksum = _sha256(partial)
        if actual_checksum != expected_checksum:
            # A complete-sized partial can still contain corrupted bytes from an
            # interrupted range response. Empty it so the next retry starts cleanly.
            partial.write_bytes(b"")
            raise IOError(
                f"SHA256 mismatch for {remote.relative_path}: "
                f"{actual_checksum} != {expected_checksum}"
            )
    partial.replace(destination)
    return DownloadResult("downloaded", str(remote.relative_path), actual_size)


def download_file(remote: RemoteFile, output_dir: pathlib.Path) -> DownloadResult:
    last_error = None
    for attempt in range(3):
        try:
            return _download_once(remote, output_dir)
        except Exception as error:  # retain partial files and retry transient failures
            last_error = f"{type(error).__name__}: {error}"
            time.sleep(2**attempt)
    return DownloadResult("error", str(remote.relative_path), error=last_error)


def _write_status(output_dir: pathlib.Path, status: dict) -> None:
    path = output_dir / "backfill-status.json"
    temporary_path = path.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as output_file:
        json.dump(status, output_file, indent=2)
        output_file.write("\n")
    os.replace(temporary_path, path)


def run_cycle(
    output_dir: pathlib.Path,
    sources: tuple[str, ...],
    workers: int,
    recent_days: int,
    min_free_gb: int,
) -> dict:
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(f"[{started_at}] Discovering {','.join(sources)} archives", flush=True)
    discovered = []
    if "binance" in sources:
        discovered.extend(discover_binance(recent_days))
    if "bybit" in sources:
        discovered.extend(discover_bybit())
    if "htx" in sources:
        discovered.extend(discover_htx())
    unique_files = {str(item.relative_path): item for item in discovered}
    files = sorted(unique_files.values(), key=lambda item: str(item.relative_path), reverse=True)
    known_bytes = sum(item.size or 0 for item in files)
    print(
        f"Discovered {len(files)} files ({known_bytes / 1024**3:.1f} GiB with known sizes)",
        flush=True,
    )

    status = {
        "cycle_started_at": started_at,
        "sources": list(sources),
        "symbols": list(SYMBOLS),
        "discovered_files": len(files),
        "known_remote_bytes": known_bytes,
        "downloaded_files": 0,
        "downloaded_bytes": 0,
        "skipped_files": 0,
        "missing_recent_files": 0,
        "errors": [],
    }
    _write_status(output_dir, status)
    free_bytes = shutil.disk_usage(output_dir).free
    if free_bytes < min_free_gb * 1024**3:
        status["errors"].append(
            f"Free space {free_bytes / 1024**3:.1f} GiB is below minimum {min_free_gb} GiB"
        )
        status["cycle_finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _write_status(output_dir, status)
        return status

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    file_iterator = iter(files)
    pending: set[concurrent.futures.Future] = set()

    def submit_next() -> bool:
        try:
            remote = next(file_iterator)
        except StopIteration:
            return False
        pending.add(executor.submit(download_file, remote, output_dir))
        return True

    for _ in range(min(len(files), workers * 2)):
        submit_next()

    completed = 0
    try:
        while pending:
            done, _ = concurrent.futures.wait(
                pending, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                pending.remove(future)
                completed += 1
                result = future.result()
                if result.status == "downloaded":
                    status["downloaded_files"] += 1
                    status["downloaded_bytes"] += result.bytes_downloaded
                elif result.status == "skipped":
                    status["skipped_files"] += 1
                elif result.status == "missing":
                    status["missing_recent_files"] += 1
                else:
                    status["errors"].append(
                        {"path": result.relative_path, "error": result.error}
                    )
                    print(
                        f"Download error: {result.relative_path}: {result.error}",
                        flush=True,
                    )
                submit_next()
                if completed % 100 == 0 or completed == len(files):
                    print(
                        f"Progress {completed}/{len(files)}; "
                        f"downloaded={status['downloaded_files']}, "
                        f"existing={status['skipped_files']}, "
                        f"errors={len(status['errors'])}",
                        flush=True,
                    )
                    _write_status(output_dir, status)
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    status["cycle_finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _write_status(output_dir, status)
    return status


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--sources", default="binance,bybit,htx")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--recent-days", type=int, default=62)
    parser.add_argument("--min-free-gb", type=int, default=200)
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=0,
        help="Refresh archives after this interval; zero runs one cycle",
    )
    arguments = parser.parse_args()
    arguments.sources = tuple(
        source.strip().lower() for source in arguments.sources.split(",") if source.strip()
    )
    invalid_sources = set(arguments.sources) - {"binance", "bybit", "htx"}
    if invalid_sources:
        parser.error(f"Unsupported sources: {sorted(invalid_sources)}")
    if not 1 <= arguments.workers <= 16:
        parser.error("--workers must be between 1 and 16")
    if arguments.recent_days < 7:
        parser.error("--recent-days must be at least 7")
    return arguments


def main() -> None:
    arguments = _arguments()
    arguments.output.mkdir(parents=True, exist_ok=True)
    try:
        while True:
            run_cycle(
                arguments.output,
                arguments.sources,
                arguments.workers,
                arguments.recent_days,
                arguments.min_free_gb,
            )
            if arguments.poll_seconds <= 0:
                return
            time.sleep(arguments.poll_seconds)
    except KeyboardInterrupt:
        print("Historical backfill stopped", flush=True)


if __name__ == "__main__":
    main()
