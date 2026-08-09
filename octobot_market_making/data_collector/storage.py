import datetime
import gzip
import json
import pathlib
import typing


class GzipJsonlWriter:
    """Append records to venue-partitioned, time-rotated gzip JSONL files."""

    def __init__(self, run_dir: pathlib.Path, rotation_minutes: int, compression_level: int):
        self.run_dir = pathlib.Path(run_dir)
        self.rotation_minutes = rotation_minutes
        self.compression_level = compression_level
        self._files: dict[tuple[str, str], typing.TextIO] = {}
        self.records_written = 0

    def write(self, record: dict) -> pathlib.Path:
        received_ns = int(record["received_ts_ns"])
        received_at = datetime.datetime.fromtimestamp(
            received_ns / 1_000_000_000, datetime.timezone.utc
        )
        minute = received_at.minute - received_at.minute % self.rotation_minutes
        bucket = received_at.replace(minute=minute, second=0, microsecond=0)
        bucket_key = bucket.strftime("%Y%m%dT%H%MZ")
        venue = str(record["venue"])
        key = venue, bucket_key
        path = (
            self.run_dir
            / "raw"
            / f"venue={venue}"
            / f"date={bucket:%Y-%m-%d}"
            / f"events-{bucket_key}.jsonl.gz"
        )
        output = self._files.get(key)
        if output is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            output = gzip.open(
                path,
                mode="at",
                encoding="utf-8",
                compresslevel=self.compression_level,
            )
            self._files[key] = output
            self._close_old_buckets(venue, bucket_key)
        output.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
        output.write("\n")
        self.records_written += 1
        return path

    def flush(self) -> None:
        for output in self._files.values():
            output.flush()

    def close(self) -> None:
        for output in self._files.values():
            output.close()
        self._files.clear()

    def _close_old_buckets(self, venue: str, current_bucket: str) -> None:
        for key in tuple(self._files):
            if key[0] == venue and key[1] != current_bucket:
                self._files.pop(key).close()

    def __enter__(self) -> "GzipJsonlWriter":
        return self

    def __exit__(self, *_args) -> None:
        self.close()
