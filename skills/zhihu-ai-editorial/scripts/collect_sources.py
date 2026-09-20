#!/usr/bin/env python3
"""Incrementally collect public AI signals into a local, auditable run file."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


USER_AGENT = "zhihu-ai-editorial/0.1 (+local personal research)"
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "ref", "source",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def text_content(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def strip_html(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(value.split())


def canonical_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(k, v) for k, v in query if k.lower() not in TRACKING_KEYS]
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), path, urllib.parse.urlencode(query), "")
        )
    except ValueError:
        return value.strip()


def title_key(title: str) -> str:
    value = title.casefold()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    return value[:240]


def fingerprint(title: str, url: str) -> str:
    material = canonical_url(url) or title_key(title)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def fetch(url: str, cache: dict[str, Any], timeout: int) -> tuple[bytes | None, dict[str, Any]]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
    }
    if cache.get("etag"):
        headers["If-None-Match"] = cache["etag"]
    if cache.get("last_modified"):
        headers["If-Modified-Since"] = cache["last_modified"]
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), {
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "status": response.status,
            }
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, {**cache, "status": 304}
        raise


def child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node:
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names:
            value = text_content(child)
            if value:
                return value
    return ""


def feed_link(node: ET.Element) -> str:
    fallback = ""
    for child in node:
        if child.tag.rsplit("}", 1)[-1].lower() != "link":
            continue
        href = child.attrib.get("href") or text_content(child)
        rel = child.attrib.get("rel", "alternate")
        if href and rel == "alternate":
            return href
        if href and not fallback:
            fallback = href
    return fallback


def parse_feed(body: bytes, source: dict[str, Any], discovered: datetime) -> list[dict[str, Any]]:
    root = ET.fromstring(body)
    nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    items: list[dict[str, Any]] = []
    for node in nodes:
        title = child_text(node, ("title",))
        url = feed_link(node) or child_text(node, ("link", "guid", "id"))
        if not title or not url:
            continue
        summary = child_text(node, ("summary", "description", "content", "encoded"))
        published = parse_time(child_text(node, ("published", "updated", "pubdate", "date")))
        items.append(make_item(source, title, url, summary, published, discovered))
    return items


def make_item(
    source: dict[str, Any], title: str, url: str, summary: str,
    published: datetime | None, discovered: datetime,
    *, original_url: str | None = None, reason: str | None = None,
    upstream_score: Any = None, upstream_id: Any = None,
) -> dict[str, Any]:
    clean_url = canonical_url(url)
    original = canonical_url(original_url or clean_url)
    return {
        "fingerprint": fingerprint(title, original or clean_url),
        "source_id": source["id"],
        "source_name": source["name"],
        "source_tier": source.get("tier", "discovery"),
        "source_trust": source.get("trust", 0.5),
        "source_tags": source.get("tags", []),
        "title": " ".join(title.split()),
        "url": clean_url,
        "original_url": original,
        "summary": strip_html(summary)[:1600],
        "reason": strip_html(reason or "")[:800] or None,
        "published_at": iso(published),
        "discovered_at": iso(discovered),
        "upstream_score": upstream_score,
        "upstream_id": str(upstream_id) if upstream_id is not None else None,
    }


def parse_aihot(body: bytes, source: dict[str, Any], discovered: datetime) -> list[dict[str, Any]]:
    payload = json.loads(body)
    records = payload.get("items", []) if isinstance(payload, dict) else []
    result = []
    for record in records:
        title = record.get("title") or record.get("originalTitle")
        links = record.get("links") or {}
        url = links.get("aihot") or links.get("original")
        if not title or not url:
            continue
        result.append(make_item(
            source,
            title,
            url,
            record.get("summary") or "",
            parse_time(record.get("publishedAt")),
            parse_time(record.get("discoveredAt")) or discovered,
            original_url=links.get("original") or url,
            reason=record.get("reason"),
            upstream_score=record.get("score"),
            upstream_id=record.get("id"),
        ))
    return result


def relevance(item: dict[str, Any], terms: list[str]) -> float:
    content = " ".join([item.get("title", ""), item.get("summary", "")]).casefold()
    tags = " ".join(item.get("source_tags", [])).casefold()
    matched = sum(1 for term in terms if term.casefold() in content)
    tag_match = any(term.casefold() in tags for term in terms)
    trust = float(item.get("source_trust") or 0.5)
    tier_bonus = 0.10 if item.get("source_tier") == "fact" else 0.04
    tag_bonus = 0.04 if tag_match else 0.0
    return round(min(1.0, matched / 4.0) * 0.71 + trust * 0.15 + tier_bonus + tag_bonus, 4)


def contains_term(content: str, term: str) -> bool:
    """Match Chinese phrases directly and ASCII terms as standalone tokens."""
    needle = term.casefold().strip()
    if not needle:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9._+\-]*", needle):
        pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
        return re.search(pattern, content) is not None
    return needle in content


def source_item_allowed(item: dict[str, Any], source: dict[str, Any]) -> bool:
    """Apply an optional per-source allowlist to noisy general-interest feeds."""
    include_terms = source.get("include_terms") or []
    if not include_terms:
        return True
    if source.get("match_scope") == "title":
        content = item.get("title", "").casefold()
    else:
        content = " ".join([item.get("title", ""), item.get("summary", "")]).casefold()
    return any(contains_term(content, str(term)) for term in include_terms)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--window-hours", type=int, default=48)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--limit-per-source", type=int, default=80)
    parser.add_argument("--include-seen", action="store_true")
    args = parser.parse_args()

    config = load_json(args.config, {})
    if not isinstance(config.get("sources"), list):
        print("Invalid config: missing sources list", file=sys.stderr)
        return 2

    args.data_dir.mkdir(parents=True, exist_ok=True)
    source_cache_dir = args.data_dir / "source-cache"
    state_path = args.data_dir / "state.json"
    state = load_json(state_path, {"version": 1, "sources": {}, "seen": {}})
    state.setdefault("sources", {})
    state.setdefault("seen", {})
    now = utc_now()
    cutoff = now - timedelta(hours=max(args.window_hours, 1))
    collected: list[dict[str, Any]] = []
    source_status: list[dict[str, Any]] = []

    for source in config["sources"]:
        source_id = source.get("id")
        if not source_id or not source.get("url"):
            continue
        cached = state["sources"].get(source_id, {})
        source_cache_path = source_cache_dir / f"{source_id}.json"
        try:
            body, response_meta = fetch(source["url"], cached, args.timeout)
            if body is None:
                items = load_json(source_cache_path, [])
                if not items:
                    body, response_meta = fetch(source["url"], {}, args.timeout)
                    if source.get("kind") == "aihot":
                        items = parse_aihot(body or b"{}", source, now)
                    elif source.get("kind") == "feed":
                        items = parse_feed(body or b"", source, now)
                    else:
                        raise ValueError(f"unsupported source kind: {source.get('kind')}")
                    save_json(source_cache_path, items)
                outcome = "not_modified"
            elif source.get("kind") == "aihot":
                items = parse_aihot(body, source, now)
                save_json(source_cache_path, items)
                outcome = "ok"
            elif source.get("kind") == "feed":
                items = parse_feed(body, source, now)
                save_json(source_cache_path, items)
                outcome = "ok"
            else:
                raise ValueError(f"unsupported source kind: {source.get('kind')}")
            state["sources"][source_id] = {
                **cached,
                **response_meta,
                "last_checked_at": iso(now),
                "last_success_at": iso(now),
                "last_error": None,
            }
            eligible_items = [item for item in items if source_item_allowed(item, source)]
            source_status.append({
                "id": source_id,
                "name": source.get("name"),
                "status": outcome,
                "items": len(items),
                "eligible_items": len(eligible_items),
            })
        except Exception as exc:  # each source must fail independently
            state["sources"][source_id] = {
                **cached,
                "last_checked_at": iso(now),
                "last_error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }
            source_status.append({
                "id": source_id, "name": source.get("name"), "status": "error",
                "error": str(exc)[:240], "items": 0,
            })
            continue

        for item in eligible_items[: max(args.limit_per_source, 1)]:
            item_time = parse_time(item.get("published_at")) or parse_time(item.get("discovered_at")) or now
            # Future-dated feed entries are retained only within a small clock-skew allowance.
            if item_time < cutoff or item_time > now + timedelta(hours=6):
                continue
            fp = item["fingerprint"]
            previously_seen = fp in state["seen"]
            if not previously_seen:
                state["seen"][fp] = {
                    "first_seen_at": iso(now),
                    "last_seen_at": iso(now),
                    "title": item["title"],
                    "url": item["original_url"] or item["url"],
                }
            else:
                state["seen"][fp]["last_seen_at"] = iso(now)
            item["is_new"] = not previously_seen
            item["relevance_hint"] = relevance(item, config.get("topic_terms", []))
            if args.include_seen or not previously_seen:
                collected.append(item)

    unique: dict[str, dict[str, Any]] = {}
    for item in sorted(
        collected,
        key=lambda x: (x.get("relevance_hint", 0), x.get("published_at") or ""),
        reverse=True,
    ):
        unique.setdefault(item["fingerprint"], item)
    items = list(unique.values())

    retention_cutoff = now - timedelta(days=366)
    state["seen"] = {
        key: value for key, value in state["seen"].items()
        if (parse_time(value.get("last_seen_at")) or now) >= retention_cutoff
    }
    state["last_run_at"] = iso(now)
    state["last_run_new_count"] = sum(1 for item in items if item.get("is_new"))
    save_json(state_path, state)

    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    run_path = args.data_dir / "runs" / f"{run_id}.json"
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": iso(now),
        "window_hours": args.window_hours,
        "new_item_count": sum(1 for item in items if item.get("is_new")),
        "source_status": source_status,
        "zhihu_queries": config.get("zhihu_queries", []),
        "items": items,
    }
    save_json(run_path, payload)
    print(str(run_path.resolve()))
    print(json.dumps({
        "new_items": payload["new_item_count"],
        "sources_ok": sum(1 for item in source_status if item["status"] in {"ok", "not_modified"}),
        "sources_failed": sum(1 for item in source_status if item["status"] == "error"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
