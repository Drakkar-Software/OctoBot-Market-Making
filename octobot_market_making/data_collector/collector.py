import asyncio
import collections
import contextlib
import datetime
import json
import pathlib
import platform
import random
import socket
import time
import typing
import uuid

from octobot_market_making.data_collector import adapters
from octobot_market_making.data_collector.config import CollectorConfig
from octobot_market_making.data_collector import rest
from octobot_market_making.data_collector.storage import GzipJsonlWriter


SCHEMA_VERSION = 2


class PublicMarketDataCollector:
    def __init__(self, config: CollectorConfig):
        self.config = config
        timestamp = f"{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}"
        self.run_id = f"{timestamp}-{uuid.uuid4().hex[:8]}"
        self.run_dir = config.output_dir / f"run={self.run_id}"
        self._queue: asyncio.Queue[typing.Optional[dict]] = asyncio.Queue(maxsize=config.queue_size)
        self._stop = asyncio.Event()
        self._local_sequences = {venue: 0 for venue in (*config.venues, "collector")}
        self.records_written = 0

    async def run(self, duration: typing.Optional[float] = None) -> pathlib.Path:
        try:
            import websockets  # pylint: disable=import-outside-toplevel
        except ImportError as error:
            raise RuntimeError(
                "The public data collector requires the packages in data_requirements.txt"
            ) from error

        self.run_dir.mkdir(parents=True, exist_ok=False)
        self._write_manifest()
        venue_adapters = adapters.build_adapters(
            self.config.venues, self.config.symbols, self.config.quote_assets
        )
        writer_task = asyncio.create_task(self._writer_loop(), name="market-data-writer")
        connection_tasks = [
            asyncio.create_task(
                self._connection_loop(adapter, websockets),
                name=f"{adapter.name}-{adapter.connection_label}-market-data",
            )
            for adapter in venue_adapters
        ]
        rest_requests = rest.build_requests(
            self.config.venues,
            self.config.symbols,
            self.config.metrics_interval_seconds,
            self.config.metadata_interval_seconds,
            self.config.quote_assets,
        )
        requests_per_venue = collections.Counter(request.venue for request in rest_requests)
        request_index_by_venue: collections.Counter[str] = collections.Counter()
        rest_tasks = []
        for request in rest_requests:
            venue_index = request_index_by_venue[request.venue]
            request_index_by_venue[request.venue] += 1
            spread_window = min(60, request.interval_seconds)
            initial_delay = (
                spread_window * venue_index / max(1, requests_per_venue[request.venue])
            )
            rest_tasks.append(
                asyncio.create_task(
                    self._rest_loop(request, initial_delay_seconds=initial_delay),
                    name=f"{request.venue}-{request.stream}-{request.symbol}",
                )
            )
        health_task = asyncio.create_task(self._health_loop(), name="collector-health")
        try:
            if duration is None:
                await self._stop.wait()
            else:
                await asyncio.sleep(duration)
        finally:
            self._stop.set()
            for task in connection_tasks + rest_tasks + [health_task]:
                task.cancel()
            await asyncio.gather(
                *connection_tasks, *rest_tasks, health_task, return_exceptions=True
            )
            await self._queue.put(None)
            await writer_task
        return self.run_dir

    def stop(self) -> None:
        self._stop.set()

    async def _connection_loop(self, adapter: adapters.VenueAdapter, websockets_module) -> None:
        retry = 0
        while not self._stop.is_set():
            connection_id = uuid.uuid4().hex
            try:
                async with websockets_module.connect(
                    adapter.url,
                    ping_interval=adapter.websocket_ping_interval,
                    ping_timeout=adapter.websocket_ping_timeout,
                    close_timeout=5,
                    max_size=None,
                    max_queue=1024,
                ) as websocket:
                    retry = 0
                    await self._put_system(
                        adapter.name, connection_id, "connected", {"url": adapter.url}
                    )
                    for subscription in adapter.subscriptions:
                        await websocket.send(json.dumps(subscription, separators=(",", ":")))
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(adapter, websocket), name=f"{adapter.name}-heartbeat"
                    )
                    try:
                        while True:
                            if adapter.receive_timeout is None:
                                raw_message = await websocket.recv()
                            else:
                                raw_message = await asyncio.wait_for(
                                    websocket.recv(), timeout=adapter.receive_timeout
                                )
                            received_ts_ns = time.time_ns()
                            received_monotonic_ns = time.monotonic_ns()
                            decoded = adapter.decode(raw_message)
                            for reply in decoded.replies:
                                await websocket.send(reply)
                            if decoded.payload is not None:
                                await self._put_market_message(
                                    adapter.name,
                                    connection_id,
                                    decoded.payload,
                                    received_ts_ns,
                                    received_monotonic_ns,
                                )
                    finally:
                        heartbeat_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await heartbeat_task
            except asyncio.CancelledError:
                raise
            except Exception as error:  # reconnect and preserve the incident in the raw dataset
                retry += 1
                delay = min(30.0, 0.5 * 2 ** min(retry, 6)) + random.random() * 0.25
                await self._put_system(
                    adapter.name,
                    connection_id,
                    "disconnected",
                    {"error": f"{type(error).__name__}: {error}", "retry_in_seconds": delay},
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def _heartbeat_loop(self, adapter: adapters.VenueAdapter, websocket) -> None:
        if adapter.heartbeat_interval is None or adapter.heartbeat_payload is None:
            await self._stop.wait()
            return
        while not self._stop.is_set():
            await asyncio.sleep(adapter.heartbeat_interval)
            await websocket.send(adapter.heartbeat_payload())

    async def _health_loop(self) -> None:
        interval_seconds = 60
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_seconds)
                return
            except asyncio.TimeoutError:
                pass
            elapsed = time.monotonic() - started
            await self._put_system(
                "collector",
                "local",
                "health",
                {
                    "queue_size": self._queue.qsize(),
                    "queue_capacity": self._queue.maxsize,
                    "records_written": self.records_written,
                    "event_loop_lag_ms": max(0.0, (elapsed - interval_seconds) * 1_000),
                },
            )

    async def _rest_loop(
        self, request: rest.RestRequest, initial_delay_seconds: float = 0
    ) -> None:
        if initial_delay_seconds:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=initial_delay_seconds)
            except asyncio.TimeoutError:
                pass
        consecutive_failures = 0
        while not self._stop.is_set():
            started_ts_ns = time.time_ns()
            next_delay = request.interval_seconds
            try:
                payload = await asyncio.to_thread(rest.fetch_json, request)
                finished_ts_ns = time.time_ns()
                await self._put_rest_message(request, payload, started_ts_ns, finished_ts_ns)
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                consecutive_failures += 1
                next_delay = min(
                    request.interval_seconds,
                    15 * 2 ** min(consecutive_failures - 1, 4),
                )
                await self._put_system(
                    request.venue,
                    "rest",
                    "rest_error",
                    {
                        "stream": request.stream,
                        "symbol": request.symbol,
                        "url": request.url,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=next_delay)
            except asyncio.TimeoutError:
                pass

    async def _put_rest_message(
        self,
        request: rest.RestRequest,
        payload: dict,
        started_ts_ns: int,
        finished_ts_ns: int,
    ) -> None:
        self._local_sequences[request.venue] += 1
        base_asset, quote_asset = adapters.market_identity(request.venue, request.symbol)
        await self._queue.put(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "rest",
                "venue": request.venue,
                "stream": request.stream,
                "symbol": request.symbol,
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                "connection_id": "rest",
                "local_sequence": self._local_sequences[request.venue],
                "received_ts_ns": finished_ts_ns,
                "received_monotonic_ns": time.monotonic_ns(),
                "exchange_ts_ms": None,
                "request_started_ts_ns": started_ts_ns,
                "request_duration_ms": (finished_ts_ns - started_ts_ns) / 1_000_000,
                "payload": payload,
            }
        )

    async def _put_market_message(
        self,
        venue: str,
        connection_id: str,
        payload: dict,
        received_ts_ns: int,
        received_monotonic_ns: int,
    ) -> None:
        stream, symbol, exchange_ts_ms = adapters.message_metadata(venue, payload)
        base_asset, quote_asset = adapters.market_identity(venue, symbol)
        self._local_sequences[venue] += 1
        await self._queue.put(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "market",
                "venue": venue,
                "stream": stream,
                "symbol": symbol,
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                "connection_id": connection_id,
                "local_sequence": self._local_sequences[venue],
                "received_ts_ns": received_ts_ns,
                "received_monotonic_ns": received_monotonic_ns,
                "exchange_ts_ms": exchange_ts_ms,
                "payload": payload,
            }
        )

    async def _put_system(self, venue: str, connection_id: str, event: str, details: dict) -> None:
        self._local_sequences[venue] += 1
        await self._queue.put(
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "system",
                "venue": venue,
                "stream": event,
                "symbol": None,
                "connection_id": connection_id,
                "local_sequence": self._local_sequences[venue],
                "received_ts_ns": time.time_ns(),
                "received_monotonic_ns": time.monotonic_ns(),
                "exchange_ts_ms": None,
                "payload": details,
            }
        )

    async def _writer_loop(self) -> None:
        writer = GzipJsonlWriter(
            self.run_dir,
            rotation_minutes=self.config.rotation_minutes,
            compression_level=self.config.compression_level,
        )
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    flush_in = max(0.05, 1 - (time.monotonic() - last_flush))
                    record = await asyncio.wait_for(self._queue.get(), timeout=flush_in)
                except asyncio.TimeoutError:
                    writer.flush()
                    last_flush = time.monotonic()
                    continue
                if record is None:
                    break
                writer.write(record)
                self.records_written = writer.records_written
                self._queue.task_done()
                if time.monotonic() - last_flush >= 1:
                    writer.flush()
                    last_flush = time.monotonic()
            writer.flush()
        finally:
            self.records_written = writer.records_written
            writer.close()

    def _write_manifest(self) -> None:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "config": self.config.as_dict(),
            "markets": {
                venue: [
                    {"base": market.base, "quote": market.quote, "symbol": market.symbol}
                    for market in adapters.market_specs(
                        venue, self.config.symbols, self.config.quote_assets
                    )
                ]
                for venue in self.config.venues
            },
            "host": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
        }
        with (self.run_dir / "manifest.json").open("w", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, indent=2)
            manifest_file.write("\n")
