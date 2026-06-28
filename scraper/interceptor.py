"""Passive Voyager/GraphQL response capture.

A single `page.on("response", ...)` listener routes each JSON response into a
logical bucket (see `config.API_ROUTES`). Raw payloads are kept in-memory for
the extractors and also dumped to `api_dumps/<bucket>_<n>.json` so you can
inspect schemas offline.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Response

from . import config


class CapturedPayloads:
    """Keeps every JSON payload, grouped by bucket name."""

    def __init__(self, dump_dir: Path | None = None) -> None:
        self._buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._dump_dir = dump_dir
        if dump_dir is not None:
            dump_dir.mkdir(parents=True, exist_ok=True)

    # ---- mutators ------------------------------------------------------
    def add(self, bucket: str, payload: dict[str, Any], url: str) -> None:
        self._buckets[bucket].append(payload)
        if self._dump_dir is not None:
            idx = len(self._buckets[bucket])
            stamp = int(time.time())
            path = self._dump_dir / f"{bucket}_{stamp}_{idx}.json"
            try:
                path.write_text(
                    json.dumps({"_url": url, "payload": payload}, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"[interceptor] dump failed for {bucket}: {e}")

    # ---- accessors -----------------------------------------------------
    def bucket(self, name: str) -> list[dict[str, Any]]:
        return list(self._buckets.get(name, ()))

    def reset(self) -> None:
        """Clear all buckets (used between tab visits)."""
        self._buckets.clear()

    def all_included(self, *buckets: str) -> list[dict[str, Any]]:
        """Flatten the `included[]` arrays of one or more buckets."""
        out: list[dict[str, Any]] = []
        for b in buckets:
            for payload in self._buckets.get(b, ()):
                inc = payload.get("included")
                if isinstance(inc, list):
                    out.extend(inc)
        return out

    def __repr__(self) -> str:
        return "CapturedPayloads(" + ", ".join(
            f"{k}={len(v)}" for k, v in self._buckets.items()
        ) + ")"


def _classify(url: str) -> str | None:
    for fragment, bucket in config.API_ROUTES:
        if fragment in url:
            return bucket
    return None


def attach(page: Page, dump_dir: Path | None = None) -> CapturedPayloads:
    """Attach a response listener to `page`. Returns the live capture object."""
    payloads = CapturedPayloads(dump_dir=dump_dir)

    def on_response(response: Response) -> None:
        url = response.url
        bucket = _classify(url)
        if bucket is None:
            return
        ctype = (response.headers or {}).get("content-type", "")
        if "json" not in ctype.lower():
            return
        if response.status != 200:
            return
        try:
            data = response.json()
        except Exception:
            return
        if isinstance(data, dict):
            payloads.add(bucket, data, url)

    page.on("response", on_response)
    return payloads
