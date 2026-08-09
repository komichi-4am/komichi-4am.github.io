#!/usr/bin/env python3
"""Deterministic queue preparation for the Komichi 4 AM project.

This script deliberately does not generate images and never invokes Git. It owns
source monitoring, timezone/location selection, Panoramax candidate discovery,
job state transitions, and recording Agent-generated review candidates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bilibili_login import DEFAULT_QR_OUTPUT, login_with_qr
from bilibili_session import BilibiliSession, BilibiliSessionError


KOMICHI_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = KOMICHI_ROOT
CONFIG_PATH = KOMICHI_ROOT / "config.json"
LOCATIONS_PATH = KOMICHI_ROOT / "data" / "locations.json"
MONITOR_STATE_PATH = KOMICHI_ROOT / "data" / "monitor-state.json"
JOBS_PATH = KOMICHI_ROOT / "data" / "jobs.json"
BACKGROUNDS_PATH = KOMICHI_ROOT / "data" / "backgrounds.json"
CANDIDATE_DATA_DIR = KOMICHI_ROOT / "data" / "candidates"
CANDIDATE_IMAGE_DIR = KOMICHI_ROOT / "assets" / "candidates"
BACKGROUND_DIR = KOMICHI_ROOT / "assets" / "backgrounds"
AUTH_RECOVERY_CODES = frozenset(
    {"bilibili_auth_missing", "bilibili_auth_expired", "bilibili_blocked"}
)
AUTH_RECOVERY_TIMEOUT_SECONDS = 180
AUTH_RECOVERY_POLL_INTERVAL_SECONDS = 2.0


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise PipelineError("missing_file", f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError("invalid_json", f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError("invalid_json", f"Expected an object in {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_random(identifier: str, purpose: str) -> random.Random:
    digest = hashlib.sha256(f"{identifier}:{purpose}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def relative_to_repo(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def default_headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
        ),
    }
    if referer:
        headers["Referer"] = referer
    return headers


def http_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 25,
) -> bytes:
    request = urllib.request.Request(url, headers=headers or default_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise PipelineError(
            "http_error", f"HTTP {exc.code} for {url}: {body or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise PipelineError("network_error", f"Network error for {url}: {exc.reason}") from exc


def http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 25,
) -> dict[str, Any]:
    payload = http_bytes(url, headers=headers, timeout=timeout)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError("invalid_response", f"Expected JSON from {url}") from exc
    if not isinstance(value, dict):
        raise PipelineError("invalid_response", f"Expected a JSON object from {url}")
    return value


def nested_text(item: dict[str, Any]) -> str:
    dynamic = item.get("modules", {}).get("module_dynamic", {})
    major = dynamic.get("major") or {}
    candidates = [
        ((major.get("opus") or {}).get("summary") or {}).get("text"),
        (dynamic.get("desc") or {}).get("text"),
        (major.get("archive") or {}).get("title"),
        (major.get("article") or {}).get("title"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return " ".join(candidate.split())[:280]
    return ""


def normalize_dynamic(item: dict[str, Any]) -> dict[str, Any]:
    dynamic_id = str(item.get("id_str") or item.get("id") or "").strip()
    author = item.get("modules", {}).get("module_author", {})
    try:
        published_ts = int(author.get("pub_ts"))
    except (TypeError, ValueError) as exc:
        raise PipelineError("invalid_bilibili_item", "Dynamic is missing module_author.pub_ts") from exc
    if not dynamic_id:
        raise PipelineError("invalid_bilibili_item", "Dynamic is missing id_str")
    published_utc = datetime.fromtimestamp(published_ts, timezone.utc)
    published_beijing = published_utc.astimezone(ZoneInfo("Asia/Shanghai"))
    return {
        "dynamicId": dynamic_id,
        "type": item.get("type"),
        "url": f"https://www.bilibili.com/opus/{dynamic_id}",
        "publishedTimestamp": published_ts,
        "publishedAtUtc": published_utc.isoformat().replace("+00:00", "Z"),
        "publishedAtBeijing": published_beijing.isoformat(),
        "summary": nested_text(item),
    }


def parse_bilibili_payload(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool, str]:
    code = payload.get("code")
    if code != 0:
        message = str(payload.get("message") or "unknown Bilibili error")
        if code == -101:
            raise PipelineError(
                "bilibili_auth_expired",
                "Bilibili login has expired. Run "
                "python3 scripts/bilibili_login.py login again; "
                "the monitor cursor was not advanced.",
            )
        if code == -412:
            raise PipelineError(
                "bilibili_blocked",
                "Bilibili rejected the authenticated feed request with code -412. "
                "QR authentication recovery is required; the monitor cursor was not advanced.",
            )
        raise PipelineError("bilibili_error", f"Bilibili error {code}: {message}")
    data = payload.get("data") or {}
    items = data.get("items") or []
    if not isinstance(items, list):
        raise PipelineError("invalid_bilibili_response", "Bilibili data.items is not a list")
    return items, bool(data.get("has_more")), str(data.get("offset") or "")


def fetch_bilibili_dynamics(
    config: dict[str, Any], feed_file: Path | None = None
) -> list[dict[str, Any]]:
    if feed_file:
        items, _, _ = parse_bilibili_payload(read_json(feed_file))
        return [normalize_dynamic(item) for item in items]

    bili = config["bilibili"]
    cookie_path = Path(
        bili.get("cookieFile", "~/.config/bilibili/cookies.json")
    ).expanduser()
    try:
        session = BilibiliSession.load(cookie_path)
        session.check_login()
    except BilibiliSessionError as exc:
        raise PipelineError(exc.code, str(exc)) from exc

    all_items: list[dict[str, Any]] = []
    offset = ""
    try:
        for _ in range(int(bili.get("maxPages", 5))):
            query = {
                "host_mid": str(bili["uid"]),
                "timezone_offset": "-480",
                "platform": "web",
                "features": "itemOpusStyle",
            }
            if offset:
                query["offset"] = offset
            url = f"{bili['feedUrl']}?{urllib.parse.urlencode(query)}"
            payload = session.get_json(
                url,
                label="Bilibili dynamic feed",
                referer=bili["profileUrl"],
                origin="https://space.bilibili.com",
            )
            items, has_more, next_offset = parse_bilibili_payload(payload)
            all_items.extend(items)
            if not has_more or not next_offset or next_offset == offset:
                break
            offset = next_offset
        if bili.get("refreshSession", True):
            session.refresh_home_session()
        session.save(cookie_path)
    except BilibiliSessionError as exc:
        # Persist harmless Set-Cookie refreshes received before a later request failed.
        # The original request error remains authoritative if this best-effort save fails.
        try:
            session.save(cookie_path)
        except BilibiliSessionError:
            pass
        raise PipelineError(exc.code, str(exc)) from exc
    return [normalize_dynamic(item) for item in all_items]


def fetch_bilibili_dynamics_with_auth_recovery(
    config: dict[str, Any], feed_file: Path | None = None
) -> list[dict[str, Any]]:
    try:
        return fetch_bilibili_dynamics(config, feed_file)
    except PipelineError as exc:
        if feed_file is not None or exc.code not in AUTH_RECOVERY_CODES:
            raise
        trigger_code = exc.code

    cookie_path = Path(
        config["bilibili"].get("cookieFile", "~/.config/bilibili/cookies.json")
    ).expanduser()
    print(
        json.dumps(
            {
                "status": "bilibili_auth_recovery_started",
                "triggerCode": trigger_code,
                "cookieFile": str(cookie_path),
                "cookieValuesPrinted": False,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        login_with_qr(
            cookie_file=cookie_path,
            qr_output=DEFAULT_QR_OUTPUT,
            timeout=AUTH_RECOVERY_TIMEOUT_SECONDS,
            poll_interval=AUTH_RECOVERY_POLL_INTERVAL_SECONDS,
        )
    except BilibiliSessionError as exc:
        raise PipelineError(exc.code, str(exc)) from exc

    return fetch_bilibili_dynamics(config, feed_file)


def choose_four_am_location(
    dynamic_id: str, published_ts: int, locations: list[dict[str, Any]]
) -> dict[str, Any]:
    published_utc = datetime.fromtimestamp(published_ts, timezone.utc)
    eligible: list[tuple[dict[str, Any], datetime]] = []
    for location in locations:
        local_time = published_utc.astimezone(ZoneInfo(location["timezone"]))
        if local_time.hour == 4:
            eligible.append((location, local_time))
    if not eligible:
        raise PipelineError(
            "no_four_am_location",
            f"No configured location is at 04:xx for dynamic {dynamic_id}",
        )
    location, local_time = stable_random(dynamic_id, "four-am-location").choice(eligible)
    return location_snapshot(location, local_time)


PLACE_MENTION_PATTERNS = (
    re.compile(
        r"(?:现在|此刻)\s*(?:是|在)\s*[「『“\"]?"
        r"(?P<place>[^，。！？,\n]{1,40}?)[」』”\"]?\s*的?\s*凌晨\s*四点"
    ),
    re.compile(
        r"(?P<place>[^，。！？,\s\n]{1,30})\s*(?:现在|此刻)\s*"
        r"(?:是|在)\s*凌晨\s*四点"
    ),
)


def extract_mentioned_place(text: str) -> str | None:
    for pattern in PLACE_MENTION_PATTERNS:
        match = pattern.search(text or "")
        if match:
            place = match.group("place").strip(" \t\r\n「」『』“”\"'")
            if place:
                return place
    return None


def normalize_place_name(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def match_mentioned_location(
    place: str, locations: list[dict[str, Any]]
) -> tuple[dict[str, Any], str] | None:
    query = normalize_place_name(place)
    for location in locations:
        aliases = [
            location.get("id", ""),
            location.get("label", ""),
            *str(location.get("label", "")).split(","),
            *(location.get("aliases") or []),
        ]
        for alias in aliases:
            if query and query == normalize_place_name(str(alias)):
                return location, str(alias)
    return None


def choose_location_for_dynamic(
    dynamic: dict[str, Any], locations: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    fallback = choose_four_am_location(
        dynamic["dynamicId"], int(dynamic["publishedTimestamp"]), locations
    )
    place = extract_mentioned_place(str(dynamic.get("summary") or ""))
    if not place:
        return fallback, None
    matched = match_mentioned_location(place, locations)
    if not matched:
        return fallback, {
            "text": place,
            "resolution": "unresolved",
            "locationId": None,
            "matchedAlias": None,
        }
    location, alias = matched
    published_utc = datetime.fromtimestamp(
        int(dynamic["publishedTimestamp"]), timezone.utc
    )
    local_time = published_utc.astimezone(ZoneInfo(location["timezone"]))
    snapshot = location_snapshot(location, local_time)
    snapshot["selectionReason"] = "dynamic_mentioned_place"
    return snapshot, {
        "text": place,
        "resolution": "catalog_alias",
        "locationId": location["id"],
        "matchedAlias": alias,
    }


def location_snapshot(
    location: dict[str, Any], local_time: datetime
) -> dict[str, Any]:
    return {
        "id": location["id"],
        "label": location["label"],
        "timezone": location["timezone"],
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "bbox": location["bbox"],
        "providers": location["providers"],
        "staticCandidates": location.get("staticCandidates") or [],
        "localTimeAtDynamic": local_time.isoformat(),
    }


def weighted_choice(identifier: str, purpose: str, values: list[tuple[str, int]]) -> str:
    rng = stable_random(identifier, purpose)
    total = sum(weight for _, weight in values)
    marker = rng.randrange(total)
    cursor = 0
    for value, weight in values:
        cursor += weight
        if marker < cursor:
            return value
    return values[-1][0]


def choose_generation_plan(dynamic_id: str) -> dict[str, Any]:
    shot_scale = weighted_choice(
        dynamic_id,
        "shot-scale",
        [
            ("environmental_wide", 15),
            ("full_body", 30),
            ("knee_up", 25),
            ("waist_up", 20),
            ("close_up", 10),
        ],
    )
    poses = {
        "environmental_wide": ["crossing_at_dawn", "small_wave_by_sign"],
        "full_body": ["walking_look_back", "tiptoe_read_sign", "playful_balanced_step"],
        "knee_up": ["point_to_sign", "hold_warm_drink", "lean_around_signpost"],
        "waist_up": ["hold_scarf_in_breeze", "sleepy_wave", "curious_turn"],
        "close_up": ["peek_into_frame", "sleepy_grin", "surprised_four_oclock"],
    }
    pose = stable_random(dynamic_id, "pose-family").choice(poses[shot_scale])
    expression = stable_random(dynamic_id, "expression").choice(
        ["playful sleepy smile", "curious bright smile", "mischievous four-o'clock grin"]
    )
    return {
        "promptVersion": "komichi-composite-v4-place-first",
        "revision": 1,
        "shotScale": shot_scale,
        "poseFamily": pose,
        "expression": expression,
        "placementSide": stable_random(dynamic_id, "placement-side").choice(["left", "right"]),
        "neckAccessory": stable_random(dynamic_id, "neck-accessory").choice(
            ["red_scarf", "traffic_light_pendant"]
        ),
    }


def create_job(
    dynamic: dict[str, Any], locations: list[dict[str, Any]]
) -> dict[str, Any]:
    dynamic_id = dynamic["dynamicId"]
    location, mentioned_location = choose_location_for_dynamic(dynamic, locations)
    return {
        "id": f"bili-{dynamic_id}",
        "status": (
            "needs_location_resolution"
            if mentioned_location
            and mentioned_location.get("resolution") == "unresolved"
            else "pending_background"
        ),
        "createdAt": utc_now(),
        "source": dynamic,
        "mentionedLocation": mentioned_location,
        "location": location,
        "background": None,
        "generation": choose_generation_plan(dynamic_id),
        "output": None,
        "agentQa": None,
        "humanReview": {"decision": None, "decidedAt": None, "notes": None},
        "history": [],
    }


def create_missing_jobs(
    dynamics: list[dict[str, Any]],
    jobs_doc: dict[str, Any],
    locations: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    existing = {
        str(job.get("source", {}).get("dynamicId"))
        for job in jobs_doc.get("jobs", [])
        if job.get("source", {}).get("dynamicId")
    }
    missing = [item for item in dynamics if item["dynamicId"] not in existing]
    if limit is not None:
        missing = missing[: max(0, limit)]

    created: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for dynamic in sorted(missing, key=lambda item: item["publishedTimestamp"]):
        try:
            job = create_job(dynamic, locations)
        except PipelineError as exc:
            failures.append({"dynamicId": dynamic["dynamicId"], "error": str(exc)})
            continue
        jobs_doc.setdefault("jobs", []).append(job)
        existing.add(dynamic["dynamicId"])
        created.append(job)
    return created, failures


def command_sync(args: argparse.Namespace) -> None:
    config = read_json(CONFIG_PATH)
    state = read_json(MONITOR_STATE_PATH)
    jobs_doc = read_json(JOBS_PATH)
    locations = read_json(LOCATIONS_PATH).get("locations") or []
    try:
        dynamics = fetch_bilibili_dynamics_with_auth_recovery(config, args.feed_file)
    except PipelineError as exc:
        state["lastError"] = {"at": utc_now(), "code": exc.code, "message": str(exc)}
        write_json(MONITOR_STATE_PATH, state)
        raise

    seen = {str(value) for value in state.get("seenDynamicIds", [])}
    existing = {job["source"]["dynamicId"] for job in jobs_doc.get("jobs", [])}
    if state.get("initialized"):
        new_dynamics = [item for item in dynamics if item["dynamicId"] not in seen]
    else:
        backfill = args.backfill
        if backfill is None:
            configured = config["bilibili"].get("initialBackfill", "all")
            backfill = None if configured in {None, "all"} else int(configured)
        new_dynamics = dynamics if backfill is None else dynamics[: max(0, backfill)]

    created, failures = create_missing_jobs(new_dynamics, jobs_doc, locations)
    existing.update(job["source"]["dynamicId"] for job in created)

    successfully_accounted = {
        item["dynamicId"]
        for item in dynamics
        if item["dynamicId"] in existing or item not in new_dynamics
    }
    ordered_seen = list(dict.fromkeys([*successfully_accounted, *state.get("seenDynamicIds", [])]))
    state.update(
        {
            "initialized": True,
            "seenDynamicIds": ordered_seen[:500],
            "lastSuccessfulSyncAt": utc_now(),
            "lastError": None,
        }
    )
    write_json(JOBS_PATH, jobs_doc)
    write_json(MONITOR_STATE_PATH, state)
    print(
        json.dumps(
            {
                "fetched": len(dynamics),
                "created": len(created),
                "createdJobIds": [job["id"] for job in created],
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_backfill_missing(args: argparse.Namespace) -> None:
    """Create jobs for feed entries already covered by the monitor baseline."""

    config = read_json(CONFIG_PATH)
    state = read_json(MONITOR_STATE_PATH)
    jobs_doc = read_json(JOBS_PATH)
    locations = read_json(LOCATIONS_PATH).get("locations") or []
    try:
        dynamics = fetch_bilibili_dynamics_with_auth_recovery(config, args.feed_file)
    except PipelineError as exc:
        state["lastError"] = {"at": utc_now(), "code": exc.code, "message": str(exc)}
        write_json(MONITOR_STATE_PATH, state)
        raise

    created, failures = create_missing_jobs(
        dynamics, jobs_doc, locations, limit=args.limit
    )
    state["lastError"] = None
    write_json(JOBS_PATH, jobs_doc)
    write_json(MONITOR_STATE_PATH, state)
    print(
        json.dumps(
            {
                "fetched": len(dynamics),
                "created": len(created),
                "createdJobIds": [job["id"] for job in created],
                "failures": failures,
                "monitorCursorChanged": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def provider_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {provider["id"]: provider for provider in config["streetView"]["providers"]}


def traffic_sign_count(feature: dict[str, Any]) -> int:
    count = 0
    for annotation in feature.get("properties", {}).get("annotations") or []:
        semantics = annotation.get("semantics") or []
        if any(item.get("key") == "osm|traffic_sign" for item in semantics):
            count += 1
    return count


def candidate_from_feature(
    provider: dict[str, Any], feature: dict[str, Any]
) -> dict[str, Any] | None:
    assets = feature.get("assets") or {}
    image_asset = assets.get("sd") or assets.get("hd")
    thumb_asset = assets.get("thumb") or image_asset
    coordinates = feature.get("geometry", {}).get("coordinates") or []
    if not image_asset or len(coordinates) < 2:
        return None
    properties = feature.get("properties") or {}
    orientation = properties.get("pers:interior_orientation") or {}
    contributors = [
        item.get("name")
        for item in (feature.get("providers") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    return {
        "id": str(feature.get("id")),
        "providerId": provider["id"],
        "providerName": provider["name"],
        "apiBaseUrl": provider["apiBaseUrl"],
        "sourceUrl": f"{provider['apiBaseUrl']}/pictures/{feature.get('id')}",
        "imageUrl": image_asset.get("href"),
        "thumbnailUrl": thumb_asset.get("href"),
        "latitude": coordinates[1],
        "longitude": coordinates[0],
        "capturedAt": properties.get("datetime"),
        "license": properties.get("license"),
        "creator": ", ".join(contributors) or None,
        "fieldOfView": orientation.get("field_of_view"),
        "trafficSignDetections": traffic_sign_count(feature),
    }


def search_candidates(job: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    providers = provider_map(config)
    bbox = ",".join(str(value) for value in job["location"]["bbox"])
    limit = int(config["streetView"].get("searchLimit", 50))
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    for provider_id in job["location"]["providers"]:
        provider = providers.get(provider_id)
        if not provider:
            continue
        url = f"{provider['apiBaseUrl']}/search?{urllib.parse.urlencode({'bbox': bbox, 'limit': limit})}"
        try:
            payload = http_json(url)
        except PipelineError as exc:
            errors.append(f"{provider_id}: {exc}")
            continue
        for feature in payload.get("features") or []:
            candidate = candidate_from_feature(provider, feature)
            if candidate:
                candidates.append(candidate)
    for static_candidate in job["location"].get("staticCandidates") or []:
        if not isinstance(static_candidate, dict):
            continue
        candidate = dict(static_candidate)
        candidate.setdefault("trafficSignDetections", 0)
        candidate.setdefault("fieldOfView", None)
        candidates.append(candidate)
    if not candidates:
        suffix = f" ({'; '.join(errors)})" if errors else ""
        raise PipelineError("no_street_candidates", f"No Panoramax candidates for {job['id']}{suffix}")

    def score(candidate: dict[str, Any]) -> tuple[int, int, float]:
        fov = candidate.get("fieldOfView")
        panorama_penalty = 1 if isinstance(fov, (int, float)) and fov > 140 else 0
        tie = stable_random(job["id"], candidate["id"]).random()
        return (-int(candidate["trafficSignDetections"]), panorama_penalty, tie)

    candidates.sort(key=score)
    return candidates[: int(config["streetView"].get("candidateLimit", 12))]


def search_candidates_with_relocation(
    job: dict[str, Any], config: dict[str, Any], locations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if (job.get("mentionedLocation") or {}).get("resolution") in {
        "catalog_alias",
        "manual_catalog",
    }:
        # A place named by Komichi is authoritative. Lack of coverage is a
        # reportable stop, not permission to silently move her elsewhere.
        return search_candidates(job, config)
    try:
        return search_candidates(job, config)
    except PipelineError as exc:
        if exc.code != "no_street_candidates":
            raise

    published_ts = int(job["source"]["publishedTimestamp"])
    published_utc = datetime.fromtimestamp(published_ts, timezone.utc)
    alternatives: list[tuple[dict[str, Any], datetime]] = []
    for location in locations:
        if location["id"] == job["location"]["id"]:
            continue
        local_time = published_utc.astimezone(ZoneInfo(location["timezone"]))
        if local_time.hour == 4:
            alternatives.append((location, local_time))
    stable_random(job["id"], "covered-location-fallback").shuffle(alternatives)

    errors: list[str] = []
    original_location = job["location"]
    for location, local_time in alternatives:
        job["location"] = location_snapshot(location, local_time)
        try:
            candidates = search_candidates(job, config)
        except PipelineError as exc:
            errors.append(f"{location['id']}: {exc.code}")
            continue
        job["locationFallback"] = {
            "from": original_location["id"],
            "reason": "original_location_has_no_street_coverage",
            "selectedAt": utc_now(),
        }
        return candidates
    job["location"] = original_location
    detail = f"; tried {', '.join(errors)}" if errors else ""
    raise PipelineError(
        "no_street_candidates",
        f"No covered 04:xx street-photo location for {job['id']}{detail}",
    )


def save_candidate_manifest(job: dict[str, Any], candidates: list[dict[str, Any]]) -> Path:
    manifest_path = CANDIDATE_DATA_DIR / f"{job['id']}.json"
    image_dir = CANDIDATE_IMAGE_DIR / job["id"]
    image_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    for candidate in candidates:
        local_thumb = image_dir / f"{candidate['id']}.jpg"
        candidate = dict(candidate)
        try:
            if not local_thumb.exists():
                local_thumb.write_bytes(http_bytes(candidate["thumbnailUrl"]))
            candidate["thumbnailPath"] = relative_to_repo(local_thumb)
        except PipelineError as exc:
            candidate["thumbnailError"] = str(exc)
        saved.append(candidate)
    write_json(
        manifest_path,
        {
            "schemaVersion": 1,
            "jobId": job["id"],
            "generatedAt": utc_now(),
            "candidates": saved,
        },
    )
    return manifest_path


LICENSE_URLS = {
    "etalab-2.0": "https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    "CC-BY-SA-4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
}


def select_candidate(
    job: dict[str, Any],
    candidate: dict[str, Any],
    *,
    confirmed_sign: bool,
    allow_without_sign: bool = False,
) -> None:
    if (
        candidate.get("trafficSignDetections", 0) < 1
        and not confirmed_sign
        and not allow_without_sign
    ):
        raise PipelineError(
            "sign_confirmation_required",
            "This candidate has no traffic-sign annotation. Inspect its thumbnail and "
            "rerun select-background with --confirm-sign only if a real sign is visible.",
        )
    filename = f"{job['id']}-{candidate['id'][:8]}.jpg"
    output_path = BACKGROUND_DIR / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cached_thumbnail = CANDIDATE_IMAGE_DIR / job["id"] / f"{candidate['id']}.jpg"
    field_of_view = candidate.get("fieldOfView")
    use_cached_perspective = bool(candidate.get("reuseThumbnailAsImage")) or (
        isinstance(field_of_view, (int, float)) and field_of_view > 140
    )
    if use_cached_perspective and cached_thumbnail.is_file():
        # Provider thumbnails are already projected as a normal camera view. The
        # original asset may be a 360° equirectangular panorama, which is not a
        # suitable base image for compositing a character.
        shutil.copy2(cached_thumbnail, output_path)
    elif not output_path.exists():
        output_path.write_bytes(http_bytes(candidate["imageUrl"]))
    background_id = f"{candidate['providerId']}-{candidate['id']}"
    job["background"] = {
        "id": background_id,
        "file": relative_to_repo(output_path),
        "provider": candidate["providerName"],
        "sourceId": candidate["id"],
        "sourceUrl": candidate["sourceUrl"],
        "imageUrl": candidate["imageUrl"],
        "license": candidate.get("license"),
        "licenseUrl": candidate.get("licenseUrl")
        or LICENSE_URLS.get(candidate.get("license")),
        "creator": candidate.get("creator"),
        "location": job["location"]["label"],
        "timezone": job["location"]["timezone"],
        "latitude": candidate["latitude"],
        "longitude": candidate["longitude"],
        "capturedAt": candidate.get("capturedAt"),
        "trafficSignDetections": candidate.get("trafficSignDetections", 0),
        "signVisuallyConfirmed": bool(confirmed_sign),
        "signRequirementWaived": bool(
            allow_without_sign and candidate.get("trafficSignDetections", 0) < 1
        ),
        "fieldOfView": field_of_view,
        "viewSource": (
            "provider_perspective_thumbnail"
            if use_cached_perspective
            else "provider_image_asset"
        ),
    }
    job["status"] = "pending_generation"
    job["updatedAt"] = utc_now()

    backgrounds = read_json(BACKGROUNDS_PATH)
    library = backgrounds.setdefault("backgrounds", [])
    existing_index = next(
        (index for index, item in enumerate(library) if item.get("id") == background_id),
        None,
    )
    if existing_index is None:
        library.append(job["background"])
    else:
        library[existing_index] = job["background"]
    write_json(BACKGROUNDS_PATH, backgrounds)


def find_job(jobs_doc: dict[str, Any], job_id: str) -> dict[str, Any]:
    for job in jobs_doc.get("jobs", []):
        if job.get("id") == job_id:
            return job
    raise PipelineError("job_not_found", f"Unknown job id: {job_id}")


def command_apply_mentioned_location(args: argparse.Namespace) -> None:
    jobs_doc = read_json(JOBS_PATH)
    locations = read_json(LOCATIONS_PATH).get("locations") or []
    job = find_job(jobs_doc, args.job_id)
    if job.get("status") == "approved":
        raise PipelineError(
            "approved_job_requires_regeneration",
            "Move the approved job into regeneration before changing its location.",
        )
    place = extract_mentioned_place(str(job.get("source", {}).get("summary") or ""))
    if not place:
        raise PipelineError(
            "no_location_mention",
            "The dynamic does not contain a supported explicit 04:00 place mention.",
        )
    matched: tuple[dict[str, Any], str] | None = None
    if args.location_id:
        location = next(
            (item for item in locations if item.get("id") == args.location_id), None
        )
        if location is None:
            raise PipelineError(
                "location_not_found", f"Unknown location id: {args.location_id}"
            )
        matched = (location, args.location_id)
        resolution = "manual_catalog"
    else:
        matched = match_mentioned_location(place, locations)
        resolution = "catalog_alias"
    if matched is None:
        raise PipelineError(
            "mentioned_location_unresolved",
            f"No configured location alias matches {place!r}.",
        )
    location, alias = matched
    published_utc = datetime.fromtimestamp(
        int(job["source"]["publishedTimestamp"]), timezone.utc
    )
    local_time = published_utc.astimezone(ZoneInfo(location["timezone"]))
    if local_time.hour != 4:
        raise PipelineError(
            "mentioned_location_not_four_am",
            f"{location['label']} is {local_time.isoformat()}, not 04:xx.",
        )
    snapshot = location_snapshot(location, local_time)
    snapshot["selectionReason"] = "dynamic_mentioned_place"
    job["location"] = snapshot
    job["mentionedLocation"] = {
        "text": place,
        "resolution": resolution,
        "locationId": location["id"],
        "matchedAlias": alias,
    }
    job["background"] = None
    job.pop("candidateManifest", None)
    job.pop("locationFallback", None)
    job["status"] = "pending_background"
    job["generation"]["promptVersion"] = "komichi-composite-v4-place-first"
    job["updatedAt"] = utc_now()
    write_json(JOBS_PATH, jobs_doc)
    print(
        json.dumps(
            {
                "jobId": job["id"],
                "status": job["status"],
                "mentionedPlace": place,
                "location": job["location"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_reset_timestamp_location(args: argparse.Namespace) -> None:
    jobs_doc = read_json(JOBS_PATH)
    locations = read_json(LOCATIONS_PATH).get("locations") or []
    job = find_job(jobs_doc, args.job_id)
    if job.get("status") == "approved":
        raise PipelineError(
            "approved_job_requires_regeneration",
            "Move the approved job into regeneration before resetting its location.",
        )
    snapshot = choose_four_am_location(
        job["source"]["dynamicId"],
        int(job["source"]["publishedTimestamp"]),
        locations,
    )
    job["location"] = snapshot
    job.pop("mentionedLocation", None)
    job["background"] = None
    job.pop("candidateManifest", None)
    job.pop("locationFallback", None)
    job["status"] = "pending_background"
    job["generation"]["promptVersion"] = "komichi-composite-v5-timestamp-location"
    job["updatedAt"] = utc_now()
    write_json(JOBS_PATH, jobs_doc)
    print(
        json.dumps(
            {
                "jobId": job["id"],
                "status": job["status"],
                "location": job["location"],
                "mentionedLocation": None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_prepare_background(args: argparse.Namespace) -> None:
    config = read_json(CONFIG_PATH)
    jobs_doc = read_json(JOBS_PATH)
    locations = read_json(LOCATIONS_PATH).get("locations") or []
    job = find_job(jobs_doc, args.job_id)
    if args.location_id:
        requested = next(
            (item for item in locations if item.get("id") == args.location_id), None
        )
        if requested is None:
            raise PipelineError(
                "location_not_found", f"Unknown location id: {args.location_id}"
            )
        published_utc = datetime.fromtimestamp(
            int(job["source"]["publishedTimestamp"]), timezone.utc
        )
        requested_local = published_utc.astimezone(ZoneInfo(requested["timezone"]))
        if requested_local.hour != 4:
            raise PipelineError(
                "location_not_four_am",
                f"{requested['label']} is {requested_local.isoformat()}, not 04:xx",
            )
        previous_id = job["location"]["id"]
        job["location"] = location_snapshot(requested, requested_local)
        job["locationFallback"] = {
            "from": previous_id,
            "reason": "explicit_covered_location_selection",
            "selectedAt": utc_now(),
        }
    mentioned_place_is_resolved = (
        (job.get("mentionedLocation") or {}).get("resolution")
        in {"catalog_alias", "manual_catalog"}
    )
    candidates = search_candidates_with_relocation(job, config, locations)
    manifest_path = save_candidate_manifest(job, candidates)
    if mentioned_place_is_resolved:
        # Candidates are already sorted by sign count. Stay in the named place
        # and accept a signless view only when no annotated sign view exists.
        non_panoramas = [
            candidate
            for candidate in candidates
            if not (
                isinstance(candidate.get("fieldOfView"), (int, float))
                and candidate["fieldOfView"] > 140
            )
        ]
        auto_candidates = non_panoramas or candidates
    else:
        auto_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("trafficSignDetections", 0) > 0
            and not (
                isinstance(candidate.get("fieldOfView"), (int, float))
                and candidate["fieldOfView"] > 140
            )
        ]
    used_source_ids = {
        str(item.get("sourceId"))
        for item in read_json(BACKGROUNDS_PATH).get("backgrounds", [])
        if item.get("sourceId")
    }
    auto_candidate = next(
        (item for item in auto_candidates if item["id"] not in used_source_ids),
        auto_candidates[0] if auto_candidates else None,
    )
    if auto_candidate:
        select_candidate(
            job,
            auto_candidate,
            confirmed_sign=False,
            allow_without_sign=mentioned_place_is_resolved,
        )
    else:
        job["status"] = "needs_background_review"
        job["candidateManifest"] = relative_to_repo(manifest_path)
        job["updatedAt"] = utc_now()
    write_json(JOBS_PATH, jobs_doc)
    print(
        json.dumps(
            {
                "jobId": job["id"],
                "status": job["status"],
                "background": job.get("background"),
                "candidateManifest": str(manifest_path.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_select_background(args: argparse.Namespace) -> None:
    jobs_doc = read_json(JOBS_PATH)
    job = find_job(jobs_doc, args.job_id)
    manifest = read_json(CANDIDATE_DATA_DIR / f"{job['id']}.json")
    candidate = next(
        (item for item in manifest.get("candidates", []) if item.get("id") == args.candidate_id),
        None,
    )
    if not candidate:
        raise PipelineError("candidate_not_found", f"Unknown candidate: {args.candidate_id}")
    if args.allow_without_sign and (
        (job.get("mentionedLocation") or {}).get("resolution")
        not in {"catalog_alias", "manual_catalog"}
    ):
        raise PipelineError(
            "sign_waiver_not_allowed",
            "A signless background is allowed only for a resolved place named in the dynamic.",
        )
    select_candidate(
        job,
        candidate,
        confirmed_sign=args.confirm_sign,
        allow_without_sign=args.allow_without_sign,
    )
    write_json(JOBS_PATH, jobs_doc)
    print(json.dumps({"jobId": job["id"], "background": job["background"]}, ensure_ascii=False, indent=2))


def command_record_output(args: argparse.Namespace) -> None:
    jobs_doc = read_json(JOBS_PATH)
    job = find_job(jobs_doc, args.job_id)
    source_path = Path(args.path).expanduser().resolve()
    if not source_path.is_file():
        raise PipelineError("missing_output", f"Generated image does not exist: {source_path}")
    revision = int(job.get("generation", {}).get("revision", 1))
    suffix = source_path.suffix.lower() if source_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
    target = KOMICHI_ROOT / "assets" / "generated" / f"{job['id']}-v{revision}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.resolve() != source_path:
        raise PipelineError("output_exists", f"Refusing to overwrite review candidate: {target}")
    if target.resolve() != source_path:
        shutil.copy2(source_path, target)
    generated_at = utc_now()
    job["status"] = "awaiting_review"
    job["updatedAt"] = generated_at
    job["output"] = {
        "revision": revision,
        "file": relative_to_repo(target),
        "absolutePath": str(target.resolve()),
        "generatedAt": generated_at,
    }
    job["agentQa"] = {"status": "preflight_pass", "notes": args.qa, "checkedAt": generated_at}
    job["humanReview"] = {"decision": None, "decidedAt": None, "notes": None}
    write_json(JOBS_PATH, jobs_doc)
    print(
        json.dumps(
            {
                "jobId": job["id"],
                "status": job["status"],
                "imagePath": str(target.resolve()),
                "metadataPath": str(JOBS_PATH.resolve()),
                "qa": args.qa,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_record_failure(args: argparse.Namespace) -> None:
    jobs_doc = read_json(JOBS_PATH)
    job = find_job(jobs_doc, args.job_id)
    checked_at = utc_now()
    job["status"] = "pending_generation"
    job["updatedAt"] = checked_at
    job["agentQa"] = {"status": "failed", "notes": args.qa, "checkedAt": checked_at}
    write_json(JOBS_PATH, jobs_doc)
    print(json.dumps({"jobId": job["id"], "status": job["status"], "qa": args.qa}, ensure_ascii=False, indent=2))


def status_payload(jobs_doc: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(job.get("status", "unknown") for job in jobs_doc.get("jobs", []))
    jobs: list[dict[str, Any]] = []
    for job in jobs_doc.get("jobs", []):
        output = job.get("output") or {}
        background = job.get("background") or {}
        jobs.append(
            {
                "id": job["id"],
                "status": job.get("status"),
                "dynamicId": job.get("source", {}).get("dynamicId"),
                "publishedAtBeijing": job.get("source", {}).get("publishedAtBeijing"),
                "location": job.get("location", {}).get("label"),
                "localTime": job.get("location", {}).get("localTimeAtDynamic"),
                "mentionedLocation": job.get("mentionedLocation"),
                "backgroundPath": str((REPO_ROOT / background["file"]).resolve()) if background.get("file") else None,
                "candidateManifest": str((REPO_ROOT / job["candidateManifest"]).resolve()) if job.get("candidateManifest") else None,
                "outputPath": output.get("absolutePath"),
                "generation": job.get("generation"),
                "agentQa": job.get("agentQa"),
            }
        )
    return {"counts": dict(counts), "jobs": jobs, "metadataPath": str(JOBS_PATH.resolve())}


def command_status(args: argparse.Namespace) -> None:
    payload = status_payload(read_json(JOBS_PATH))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print("Job counts:")
    for status, count in sorted(payload["counts"].items()):
        print(f"  {status}: {count}")
    for job in payload["jobs"]:
        print(f"{job['id']}: {job['status']} {job['outputPath'] or ''}".rstrip())


def command_validate(_: argparse.Namespace) -> None:
    jobs_doc = read_json(JOBS_PATH)
    posts_doc = read_json(KOMICHI_ROOT / "data" / "posts.json")
    jobs = jobs_doc.get("jobs") or []
    posts = posts_doc.get("posts") or []
    errors: list[str] = []
    job_ids = [str(job.get("id")) for job in jobs]
    if len(job_ids) != len(set(job_ids)):
        errors.append("data/jobs.json contains duplicate job ids")
    jobs_by_id = {job["id"]: job for job in jobs if job.get("id")}

    for job in jobs:
        job_id = str(job.get("id") or "<missing-id>")
        local_value = job.get("location", {}).get("localTimeAtDynamic")
        try:
            if datetime.fromisoformat(str(local_value)).hour != 4:
                errors.append(f"{job_id}: local time is not 04:xx")
        except ValueError:
            errors.append(f"{job_id}: invalid local time")
        background = job.get("background") or {}
        if background.get("file") and not (REPO_ROOT / background["file"]).is_file():
            errors.append(f"{job_id}: background file is missing")
        output = job.get("output") or {}
        if job.get("status") in {"awaiting_review", "approved", "rejected"}:
            if not output.get("file") or not (REPO_ROOT / output["file"]).is_file():
                errors.append(f"{job_id}: review output file is missing")

    post_ids: list[str] = []
    required_post_fields = {
        "image",
        "publishedAtBeijing",
        "location",
        "localTime",
        "timezone",
        "latitude",
        "longitude",
    }
    for post in posts:
        post_id = str(post.get("id") or "<missing-id>")
        post_ids.append(post_id)
        job = jobs_by_id.get(post_id)
        if not job or job.get("status") != "approved":
            errors.append(f"{post_id}: published post is not backed by an approved job")
        missing = sorted(field for field in required_post_fields if post.get(field) is None)
        if missing:
            errors.append(f"{post_id}: missing post fields {', '.join(missing)}")
        image = post.get("image")
        if image and not (KOMICHI_ROOT / image).is_file():
            errors.append(f"{post_id}: published image is missing")
    if len(post_ids) != len(set(post_ids)):
        errors.append("data/posts.json contains duplicate post ids")

    if errors:
        raise PipelineError("validation_failed", "; ".join(errors))
    print(
        json.dumps(
            {
                "ok": True,
                "jobs": len(jobs),
                "posts": len(posts),
                "awaitingReview": sum(
                    1 for job in jobs if job.get("status") == "awaiting_review"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Fetch unseen Bilibili dynamics and create jobs")
    sync_parser.add_argument("--feed-file", type=Path, help="Use a local Bilibili response fixture")
    sync_parser.add_argument("--backfill", type=int, help="Override first-run backfill count")
    sync_parser.set_defaults(func=command_sync)

    backfill_parser = subparsers.add_parser(
        "backfill-missing",
        help="Create jobs for feed items missing from the queue, including seen history",
    )
    backfill_parser.add_argument("--feed-file", type=Path, help="Use a local response fixture")
    backfill_parser.add_argument(
        "--limit", type=int, help="Maximum missing feed entries to create; default is all"
    )
    backfill_parser.set_defaults(func=command_backfill_missing)

    prepare_parser = subparsers.add_parser("prepare-background", help="Discover and prepare Panoramax candidates")
    prepare_parser.add_argument("job_id")
    prepare_parser.add_argument(
        "--location-id",
        help="Use a specific configured location after verifying it is still 04:xx",
    )
    prepare_parser.set_defaults(func=command_prepare_background)

    mention_parser = subparsers.add_parser(
        "apply-mentioned-location",
        help="Resolve an explicit 04:00 place mention to a configured location",
    )
    mention_parser.add_argument("job_id")
    mention_parser.add_argument(
        "--location-id",
        help="Use this configured location when the alias cannot be resolved automatically",
    )
    mention_parser.set_defaults(func=command_apply_mentioned_location)

    reset_location_parser = subparsers.add_parser(
        "reset-timestamp-location",
        help="Discard a mistaken place mention and restore deterministic 04:xx selection",
    )
    reset_location_parser.add_argument("job_id")
    reset_location_parser.set_defaults(func=command_reset_timestamp_location)

    select_parser = subparsers.add_parser("select-background", help="Select a visually reviewed candidate")
    select_parser.add_argument("job_id")
    select_parser.add_argument("candidate_id")
    select_parser.add_argument("--confirm-sign", action="store_true")
    select_parser.add_argument(
        "--allow-without-sign",
        action="store_true",
        help="Allow a signless view only when the dynamic named this location",
    )
    select_parser.set_defaults(func=command_select_background)

    output_parser = subparsers.add_parser("record-output", help="Record an Agent-generated review candidate")
    output_parser.add_argument("job_id")
    output_parser.add_argument("path")
    output_parser.add_argument("--qa", required=True)
    output_parser.set_defaults(func=command_record_output)

    failure_parser = subparsers.add_parser("record-failure", help="Record a failed generation attempt")
    failure_parser.add_argument("job_id")
    failure_parser.add_argument("--qa", required=True)
    failure_parser.set_defaults(func=command_record_failure)

    status_parser = subparsers.add_parser("status", help="Show queue status")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=command_status)

    validate_parser = subparsers.add_parser(
        "validate", help="Check queue, output, and published feed consistency"
    )
    validate_parser.set_defaults(func=command_validate)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except PipelineError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
