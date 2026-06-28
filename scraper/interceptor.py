"""Passive Voyager/GraphQL response capture.

A single response listener (attached to the BrowserContext so popups and
service-worker traffic are caught too) routes each JSON response into a
logical bucket (see `config.API_ROUTES`). Each capture is stored as a
`(url, payload)` tuple so extractors and debugging can inspect the
originating request (queryId, threadUrn, etc.) without re-fetching.

Backward-compat: `bucket(name)` still returns a list of payload dicts
(no URLs) -- existing extractors don't need to change.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, Response

from . import config


class CapturedPayloads:
    """Keeps every JSON payload (with its URL), grouped by bucket name."""

    def __init__(self, dump_dir: Path | None = None) -> None:
        # bucket -> [(url, payload), ...]
        self._buckets: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self._dump_dir = dump_dir
        if dump_dir is not None:
            dump_dir.mkdir(parents=True, exist_ok=True)

    # ---- mutators ------------------------------------------------------
    def add(self, bucket: str, payload: dict[str, Any], url: str) -> None:
        self._buckets[bucket].append((url, payload))
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
        """Return just the payload dicts (backward-compat for extractors)."""
        return [p for _, p in self._buckets.get(name, ())]

    def bucket_with_urls(self, name: str) -> list[tuple[str, dict[str, Any]]]:
        """Return (url, payload) tuples for callers that need the request URL."""
        return list(self._buckets.get(name, ()))

    def buckets(self, *names: str) -> list[dict[str, Any]]:
        """Concat multiple buckets' payloads. Handy for cross-bucket extractors."""
        out: list[dict[str, Any]] = []
        for n in names:
            out.extend(p for _, p in self._buckets.get(n, ()))
        return out

    def all_payloads(self) -> list[dict[str, Any]]:
        """Every captured payload across all buckets."""
        out: list[dict[str, Any]] = []
        for entries in self._buckets.values():
            out.extend(p for _, p in entries)
        return out

    def reset(self) -> None:
        """Clear all buckets (used between tab visits)."""
        self._buckets.clear()

    def all_included(self, *buckets: str) -> list[dict[str, Any]]:
        """Flatten the `included[]` arrays of one or more buckets."""
        out: list[dict[str, Any]] = []
        for b in buckets:
            for _, payload in self._buckets.get(b, ()):
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


def attach(target: BrowserContext | Page, dump_dir: Path | None = None) -> CapturedPayloads:
    """Attach a response listener to a BrowserContext (preferred) or a Page.

    Context-scope catches popups and service-worker fetches that a page-scope
    listener misses. Passing a Page still works (backward-compat).
    """
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

    target.on("response", on_response)
    return payloads
