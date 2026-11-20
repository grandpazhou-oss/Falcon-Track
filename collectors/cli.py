"""Collect registered public pages into an atomic JSON result file.

Example: python -m collectors.cli --source ysczw --output /tmp/result.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import sys

from .sources import SOURCES


def bounded_int(low, high):
    def convert(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be in {low}..{high}")
        return number
    return convert


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Low-frequency, public-only fixed-source collector")
    parser.add_argument("--source", required=True, choices=sorted(SOURCES))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cursor", type=bounded_int(1, 10000), default=1)
    parser.add_argument("--max-pages", type=bounded_int(1, 30), default=1)
    parser.add_argument("--max-items", type=bounded_int(1, 100), default=5)
    parser.add_argument("--seen-file", type=Path)
    parser.add_argument("--refresh-items", type=bounded_int(0, 20), default=0)
    args = parser.parse_args(argv)
    result = {"source_key": args.source, "status": "failed", "items": [], "next_cursor": args.cursor, "stats": {}, "error": "Collector did not finish"}
    try:
        seen = []
        if args.seen_file:
            if args.seen_file.stat().st_size > 10_000_000:
                raise ValueError("Seen identifier file is too large")
            seen = json.loads(args.seen_file.read_text(encoding="utf-8"))
            if not isinstance(seen, list) or not all(isinstance(x, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,160}",x)) for x in seen):
                raise ValueError("Seen file must be a JSON array of safe stable source identifiers as strings")
        from scrapy.crawler import CrawlerProcess
        from .spider import PublicSourceSpider
        process = CrawlerProcess(settings={"LOG_LEVEL": "WARNING"})
        process.crawl(PublicSourceSpider, source_key=args.source, result=result,
                      cursor=args.cursor, max_pages=args.max_pages, max_items=args.max_items,
                      seen=seen, refresh_items=args.refresh_items)
        process.start()
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        atomic_json(args.output, result)
    # A blocked/failed run is an operational failure, never a successful empty scan.
    return 2 if result["status"] in {"failed", "blocked"} else 0


if __name__ == "__main__":
    sys.exit(main())
