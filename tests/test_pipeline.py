from __future__ import annotations

import argparse
import http.cookiejar
import io
import json
import stat
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import pipeline  # noqa: E402
import review  # noqa: E402
from bilibili_session import BilibiliSession, BilibiliSessionError  # noqa: E402


FIXTURE = Path(__file__).parent / "fixtures" / "bilibili-feed.json"


def sample_cookie() -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0,
        name="SESSDATA",
        value="test-secret-not-a-real-session",
        port=None,
        port_specified=False,
        domain=".bilibili.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=False,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )


class PipelineTests(unittest.TestCase):
    def test_cookie_session_round_trip_is_user_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / ".config" / "bilibili" / "cookies.json"
            jar = http.cookiejar.CookieJar()
            jar.set_cookie(sample_cookie())
            BilibiliSession(
                jar, account={"mid": "123", "name": "test-user"}
            ).save(cookie_path)

            self.assertEqual(stat.S_IMODE(cookie_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(cookie_path.parent.stat().st_mode), 0o700)
            loaded = BilibiliSession.load(cookie_path)
            self.assertEqual(loaded.account, {"mid": "123", "name": "test-user"})
            self.assertEqual([cookie.name for cookie in loaded.cookie_jar], ["SESSDATA"])

    def test_missing_cookie_stops_before_fetching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = pipeline.read_json(pipeline.CONFIG_PATH)
            config["bilibili"]["cookieFile"] = str(Path(directory) / "missing.json")
            with self.assertRaises(BilibiliSessionError):
                BilibiliSession.load(Path(config["bilibili"]["cookieFile"]))
            with self.assertRaises(pipeline.PipelineError) as caught:
                pipeline.fetch_bilibili_dynamics(config)
            self.assertEqual(caught.exception.code, "bilibili_auth_missing")

    def test_bilibili_network_request_retries_five_times_one_second_apart(self) -> None:
        session = BilibiliSession.empty()
        failure = urllib.error.URLError("dns unavailable")
        with (
            mock.patch.object(session.opener, "open", side_effect=failure) as open_request,
            mock.patch("bilibili_session.time.sleep") as sleep,
            self.assertRaises(BilibiliSessionError) as caught,
        ):
            session._request_bytes(
                "https://api.bilibili.com/test",
                label="Bilibili test request",
                headers={},
            )

        self.assertEqual(caught.exception.code, "bilibili_network_error")
        self.assertIn("after 6 attempts (5 retries)", str(caught.exception))
        self.assertEqual(open_request.call_count, 6)
        self.assertEqual(sleep.call_args_list, [mock.call(1.0)] * 5)

    def test_bilibili_http_412_is_not_retried(self) -> None:
        session = BilibiliSession.empty()
        failure = urllib.error.HTTPError(
            "https://api.bilibili.com/test",
            412,
            "Precondition Failed",
            {},
            io.BytesIO(b'{"message":"blocked"}'),
        )
        with (
            mock.patch.object(session.opener, "open", side_effect=failure) as open_request,
            mock.patch("bilibili_session.time.sleep") as sleep,
            self.assertRaises(BilibiliSessionError) as caught,
        ):
            session._request_bytes(
                "https://api.bilibili.com/test",
                label="Bilibili test request",
                headers={},
            )

        self.assertEqual(caught.exception.code, "bilibili_blocked")
        self.assertEqual(open_request.call_count, 1)
        sleep.assert_not_called()

    def test_bilibili_auth_and_risk_codes_have_specific_failures(self) -> None:
        with self.assertRaises(pipeline.PipelineError) as expired:
            pipeline.parse_bilibili_payload({"code": -101, "message": "账号未登录"})
        self.assertEqual(expired.exception.code, "bilibili_auth_expired")
        with self.assertRaises(pipeline.PipelineError) as blocked:
            pipeline.parse_bilibili_payload({"code": -412, "message": "请求被拦截"})
        self.assertEqual(blocked.exception.code, "bilibili_blocked")

    def test_auth_failure_runs_qr_recovery_then_retries_sync_fetch(self) -> None:
        config = pipeline.read_json(pipeline.CONFIG_PATH)
        recovered_dynamics = [{"dynamicId": "recovered"}]
        with (
            mock.patch.object(
                pipeline,
                "fetch_bilibili_dynamics",
                side_effect=[
                    pipeline.PipelineError(
                        "bilibili_auth_expired", "Bilibili login has expired"
                    ),
                    recovered_dynamics,
                ],
            ) as fetch,
            mock.patch.object(pipeline, "login_with_qr") as login,
            redirect_stdout(io.StringIO()),
        ):
            result = pipeline.fetch_bilibili_dynamics_with_auth_recovery(config)

        self.assertEqual(result, recovered_dynamics)
        self.assertEqual(fetch.call_count, 2)
        login.assert_called_once_with(
            cookie_file=Path(config["bilibili"]["cookieFile"]).expanduser(),
            qr_output=pipeline.DEFAULT_QR_OUTPUT,
            timeout=180,
            poll_interval=2.0,
        )

    def test_qr_recovery_failure_preserves_the_safe_error(self) -> None:
        config = pipeline.read_json(pipeline.CONFIG_PATH)
        with (
            mock.patch.object(
                pipeline,
                "fetch_bilibili_dynamics",
                side_effect=pipeline.PipelineError(
                    "bilibili_auth_missing", "Bilibili login is not configured"
                ),
            ),
            mock.patch.object(
                pipeline,
                "login_with_qr",
                side_effect=BilibiliSessionError(
                    "bilibili_qr_login_timeout", "Bilibili QR login timed out"
                ),
            ),
            redirect_stdout(io.StringIO()),
            self.assertRaises(pipeline.PipelineError) as caught,
        ):
            pipeline.fetch_bilibili_dynamics_with_auth_recovery(config)

        self.assertEqual(caught.exception.code, "bilibili_qr_login_timeout")
        self.assertEqual(str(caught.exception), "Bilibili QR login timed out")

    def test_non_auth_failure_does_not_start_qr_recovery(self) -> None:
        config = pipeline.read_json(pipeline.CONFIG_PATH)
        with (
            mock.patch.object(
                pipeline,
                "fetch_bilibili_dynamics",
                side_effect=pipeline.PipelineError(
                    "bilibili_network_error", "network unavailable"
                ),
            ),
            mock.patch.object(pipeline, "login_with_qr") as login,
            self.assertRaises(pipeline.PipelineError),
        ):
            pipeline.fetch_bilibili_dynamics_with_auth_recovery(config)

        login.assert_not_called()

    def test_fixture_parsing(self) -> None:
        payload = pipeline.read_json(FIXTURE)
        items, has_more, offset = pipeline.parse_bilibili_payload(payload)
        self.assertFalse(has_more)
        self.assertEqual(offset, "")
        dynamic = pipeline.normalize_dynamic(items[1])
        self.assertEqual(dynamic["dynamicId"], "200000000000000001")
        self.assertEqual(dynamic["summary"], "第一条测试动态")
        self.assertIn("+08:00", dynamic["publishedAtBeijing"])

    def test_every_fixture_dynamic_gets_a_four_am_location(self) -> None:
        config_locations = pipeline.read_json(pipeline.LOCATIONS_PATH)["locations"]
        payload = pipeline.read_json(FIXTURE)
        items, _, _ = pipeline.parse_bilibili_payload(payload)
        for raw in items:
            dynamic = pipeline.normalize_dynamic(raw)
            selected = pipeline.choose_four_am_location(
                dynamic["dynamicId"], dynamic["publishedTimestamp"], config_locations
            )
            local_time = datetime.fromisoformat(selected["localTimeAtDynamic"])
            self.assertEqual(local_time.hour, 4)

    def test_explicit_four_am_place_mention_overrides_random_location(self) -> None:
        locations = pipeline.read_json(pipeline.LOCATIONS_PATH)["locations"]
        dynamic = {
            "dynamicId": "iceland-mentioned",
            "publishedTimestamp": int(
                datetime(2026, 8, 6, 4, 45, tzinfo=timezone.utc).timestamp()
            ),
            "summary": "现在是冰岛的凌晨四点多，小路正在规划明天要做的事。",
        }
        location, mention = pipeline.choose_location_for_dynamic(dynamic, locations)
        self.assertEqual(location["id"], "reykjavik")
        self.assertEqual(location["selectionReason"], "dynamic_mentioned_place")
        self.assertEqual(mention["text"], "冰岛")
        self.assertEqual(mention["resolution"], "catalog_alias")

    def test_country_name_resolves_but_event_preview_does_not(self) -> None:
        locations = pipeline.read_json(pipeline.LOCATIONS_PATH)["locations"]
        place = pipeline.extract_mentioned_place("现在是加纳共和国的凌晨四点多哦？")
        self.assertEqual(place, "加纳共和国")
        matched = pipeline.match_mentioned_location(place, locations)
        self.assertIsNotNone(matched)
        self.assertEqual(matched[0]["id"], "accra")

        preview = {
            "dynamicId": "1234175706002358279",
            "publishedTimestamp": 1786192324,
            "summary": "我们法属波里尼西亚的甘比尔群岛 （好长）的凌晨四点见+",
        }
        location, mention = pipeline.choose_location_for_dynamic(preview, locations)
        self.assertEqual(location["id"], "adamstown")
        self.assertIsNone(mention)

    def test_unconfigured_place_mention_waits_for_resolution(self) -> None:
        locations = pipeline.read_json(pipeline.LOCATIONS_PATH)["locations"]
        dynamic = {
            "dynamicId": "unknown-mentioned",
            "publishedTimestamp": int(
                datetime(2026, 8, 6, 4, 45, tzinfo=timezone.utc).timestamp()
            ),
            "summary": "现在是火星基地的凌晨四点，小路正在散步。",
        }
        job = pipeline.create_job(dynamic, locations)
        self.assertEqual(job["status"], "needs_location_resolution")
        self.assertEqual(job["mentionedLocation"]["text"], "火星基地")
        self.assertEqual(job["mentionedLocation"]["resolution"], "unresolved")

    def test_named_place_without_coverage_does_not_relocate(self) -> None:
        job = {
            "id": "named-place",
            "source": {"publishedTimestamp": 0},
            "mentionedLocation": {"resolution": "catalog_alias"},
            "location": {"id": "named", "bbox": [0, 0, 1, 1], "providers": []},
        }
        failure = pipeline.PipelineError("no_street_candidates", "no coverage")
        with mock.patch.object(
            pipeline, "search_candidates", side_effect=failure
        ) as search:
            with self.assertRaises(pipeline.PipelineError):
                pipeline.search_candidates_with_relocation(job, {}, [])
        search.assert_called_once()

    def test_location_catalog_covers_every_utc_hour_in_winter_and_summer(self) -> None:
        locations = pipeline.read_json(pipeline.LOCATIONS_PATH)["locations"]
        for month in (1, 7):
            for hour in range(24):
                instant = datetime(2026, month, 15, hour, 20, tzinfo=timezone.utc)
                selected = pipeline.choose_four_am_location(
                    f"coverage-{month}-{hour}", int(instant.timestamp()), locations
                )
                self.assertEqual(datetime.fromisoformat(selected["localTimeAtDynamic"]).hour, 4)

    def test_creative_choices_are_stable_and_separately_seeded(self) -> None:
        first = pipeline.choose_generation_plan("123456")
        second = pipeline.choose_generation_plan("123456")
        self.assertEqual(first, second)
        self.assertIn(first["neckAccessory"], {"red_scarf", "traffic_light_pendant"})
        accessory_rng = pipeline.stable_random("123456", "neck-accessory").getstate()
        shot_rng = pipeline.stable_random("123456", "shot-scale").getstate()
        self.assertNotEqual(accessory_rng, shot_rng)

    def test_traffic_sign_annotation_count(self) -> None:
        feature = {
            "properties": {
                "annotations": [
                    {"semantics": [{"key": "osm|traffic_sign", "value": "stop"}]},
                    {"semantics": [{"key": "other", "value": "bench"}]},
                ]
            }
        }
        self.assertEqual(pipeline.traffic_sign_count(feature), 1)

    def test_static_open_photo_candidate_is_supported(self) -> None:
        job = {
            "id": "static-test",
            "location": {
                "bbox": [0, 0, 1, 1],
                "providers": [],
                "staticCandidates": [
                    {
                        "id": "commons-photo",
                        "providerId": "wikimedia-commons",
                        "providerName": "Wikimedia Commons",
                        "sourceUrl": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                        "imageUrl": "https://upload.wikimedia.org/example.jpg",
                        "thumbnailUrl": "https://upload.wikimedia.org/example-thumb.jpg",
                        "latitude": 1.0,
                        "longitude": 2.0,
                        "license": "CC BY 2.0",
                        "licenseUrl": "https://creativecommons.org/licenses/by/2.0/",
                        "creator": "Example",
                        "trafficSignDetections": 1,
                    }
                ],
            },
        }
        config = pipeline.read_json(pipeline.CONFIG_PATH)
        candidates = pipeline.search_candidates(job, config)
        self.assertEqual(candidates[0]["providerId"], "wikimedia-commons")
        self.assertEqual(candidates[0]["trafficSignDetections"], 1)

    def test_panorama_selection_uses_cached_perspective_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            background_dir = temp / "komichi" / "assets" / "backgrounds"
            candidate_dir = temp / "komichi" / "assets" / "candidates"
            backgrounds_path = temp / "komichi" / "data" / "backgrounds.json"
            pipeline.write_json(backgrounds_path, {"schemaVersion": 1, "backgrounds": []})
            cached = candidate_dir / "panorama-job" / "picture-360.jpg"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"normal-perspective-view")
            background_dir.mkdir(parents=True)
            output = background_dir / "panorama-job-picture-.jpg"
            output.write_bytes(b"equirectangular-panorama")
            job = {
                "id": "panorama-job",
                "location": {
                    "label": "Cayenne, French Guiana",
                    "timezone": "America/Cayenne",
                },
            }
            candidate = {
                "id": "picture-360",
                "providerId": "panoramax",
                "providerName": "Panoramax",
                "sourceUrl": "https://example.test/picture-360",
                "imageUrl": "https://example.test/picture-360/sd.jpg",
                "latitude": 4.9,
                "longitude": -52.3,
                "trafficSignDetections": 1,
                "fieldOfView": 360,
                "license": "etalab-2.0",
            }
            with (
                mock.patch.object(pipeline, "REPO_ROOT", temp),
                mock.patch.object(pipeline, "BACKGROUND_DIR", background_dir),
                mock.patch.object(pipeline, "CANDIDATE_IMAGE_DIR", candidate_dir),
                mock.patch.object(pipeline, "BACKGROUNDS_PATH", backgrounds_path),
            ):
                pipeline.select_candidate(job, candidate, confirmed_sign=False)

            self.assertEqual(output.read_bytes(), b"normal-perspective-view")
            self.assertEqual(job["background"]["viewSource"], "provider_perspective_thumbnail")
            self.assertEqual(
                pipeline.read_json(backgrounds_path)["backgrounds"][0]["viewSource"],
                "provider_perspective_thumbnail",
            )

    def test_named_place_can_waive_sign_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            background_dir = temp / "komichi" / "assets" / "backgrounds"
            backgrounds_path = temp / "komichi" / "data" / "backgrounds.json"
            pipeline.write_json(backgrounds_path, {"schemaVersion": 1, "backgrounds": []})
            job = {
                "id": "signless-job",
                "mentionedLocation": {"resolution": "catalog_alias"},
                "location": {"label": "Montevideo", "timezone": "America/Montevideo"},
            }
            candidate = {
                "id": "signless-picture",
                "providerId": "panoramax-osm",
                "providerName": "Panoramax",
                "sourceUrl": "https://example.test/signless-picture",
                "imageUrl": "https://example.test/signless-picture.jpg",
                "latitude": -34.9,
                "longitude": -56.1,
                "trafficSignDetections": 0,
                "fieldOfView": 70,
                "license": "CC-BY-SA-4.0",
            }
            with (
                mock.patch.object(pipeline, "REPO_ROOT", temp),
                mock.patch.object(pipeline, "BACKGROUND_DIR", background_dir),
                mock.patch.object(
                    pipeline, "CANDIDATE_IMAGE_DIR", temp / "unused-candidates"
                ),
                mock.patch.object(pipeline, "BACKGROUNDS_PATH", backgrounds_path),
                mock.patch.object(pipeline, "http_bytes", return_value=b"street-photo"),
            ):
                pipeline.select_candidate(
                    job,
                    candidate,
                    confirmed_sign=False,
                    allow_without_sign=True,
                )
            self.assertTrue(job["background"]["signRequirementWaived"])
            self.assertFalse(job["background"]["signVisuallyConfirmed"])

    def test_first_sync_creates_job_without_touching_git(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path = temp / "monitor-state.json"
            jobs_path = temp / "jobs.json"
            pipeline.write_json(
                state_path,
                {
                    "schemaVersion": 1,
                    "source": "bilibili",
                    "uid": "1512246445",
                    "initialized": False,
                    "seenDynamicIds": [],
                    "lastSuccessfulSyncAt": None,
                    "lastError": None,
                },
            )
            pipeline.write_json(jobs_path, {"schemaVersion": 1, "jobs": []})
            args = argparse.Namespace(feed_file=FIXTURE, backfill=1)
            with (
                mock.patch.object(pipeline, "MONITOR_STATE_PATH", state_path),
                mock.patch.object(pipeline, "JOBS_PATH", jobs_path),
                redirect_stdout(io.StringIO()),
            ):
                pipeline.command_sync(args)
            jobs = pipeline.read_json(jobs_path)["jobs"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["status"], "pending_background")
            self.assertEqual(datetime.fromisoformat(jobs[0]["location"]["localTimeAtDynamic"]).hour, 4)

    def test_first_sync_defaults_to_all_visible_feed_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path = temp / "monitor-state.json"
            jobs_path = temp / "jobs.json"
            pipeline.write_json(
                state_path,
                {
                    "schemaVersion": 1,
                    "source": "bilibili",
                    "uid": "1512246445",
                    "initialized": False,
                    "seenDynamicIds": [],
                    "lastSuccessfulSyncAt": None,
                    "lastError": None,
                },
            )
            pipeline.write_json(jobs_path, {"schemaVersion": 1, "jobs": []})
            args = argparse.Namespace(feed_file=FIXTURE, backfill=None)
            with (
                mock.patch.object(pipeline, "MONITOR_STATE_PATH", state_path),
                mock.patch.object(pipeline, "JOBS_PATH", jobs_path),
                redirect_stdout(io.StringIO()),
            ):
                pipeline.command_sync(args)
            self.assertEqual(len(pipeline.read_json(jobs_path)["jobs"]), 2)

    def test_backfill_missing_creates_seen_history_without_changing_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path = temp / "monitor-state.json"
            jobs_path = temp / "jobs.json"
            payload = pipeline.read_json(FIXTURE)
            raw_items, _, _ = pipeline.parse_bilibili_payload(payload)
            dynamics = [pipeline.normalize_dynamic(item) for item in raw_items]
            original_seen = [item["dynamicId"] for item in dynamics]
            pipeline.write_json(
                state_path,
                {
                    "schemaVersion": 1,
                    "source": "bilibili",
                    "uid": "1512246445",
                    "initialized": True,
                    "seenDynamicIds": original_seen,
                    "lastSuccessfulSyncAt": "2026-01-01T00:00:00Z",
                    "lastError": None,
                },
            )
            pipeline.write_json(
                jobs_path,
                {
                    "schemaVersion": 1,
                    "jobs": [
                        {
                            "id": f"bili-{dynamics[0]['dynamicId']}",
                            "source": dynamics[0],
                            "status": "pending_background",
                        }
                    ],
                },
            )
            args = argparse.Namespace(feed_file=FIXTURE, limit=2)
            with (
                mock.patch.object(pipeline, "MONITOR_STATE_PATH", state_path),
                mock.patch.object(pipeline, "JOBS_PATH", jobs_path),
                redirect_stdout(io.StringIO()),
            ):
                pipeline.command_backfill_missing(args)
            self.assertEqual(len(pipeline.read_json(jobs_path)["jobs"]), 2)
            self.assertEqual(
                pipeline.read_json(state_path)["seenDynamicIds"], original_seen
            )

    def test_failed_sync_records_error_without_advancing_cursor_or_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path = temp / "monitor-state.json"
            jobs_path = temp / "jobs.json"
            initial_state = {
                "schemaVersion": 1,
                "source": "bilibili",
                "uid": "1512246445",
                "initialized": False,
                "seenDynamicIds": [],
                "lastSuccessfulSyncAt": None,
                "lastError": None,
            }
            pipeline.write_json(state_path, initial_state)
            pipeline.write_json(jobs_path, {"schemaVersion": 1, "jobs": []})
            failure = pipeline.PipelineError(
                "bilibili_auth_expired", "Bilibili login has expired"
            )
            args = argparse.Namespace(feed_file=None, backfill=None)
            with (
                mock.patch.object(pipeline, "MONITOR_STATE_PATH", state_path),
                mock.patch.object(pipeline, "JOBS_PATH", jobs_path),
                mock.patch.object(
                    pipeline,
                    "fetch_bilibili_dynamics_with_auth_recovery",
                    side_effect=failure,
                ),
                self.assertRaises(pipeline.PipelineError),
            ):
                pipeline.command_sync(args)

            state = pipeline.read_json(state_path)
            self.assertFalse(state["initialized"])
            self.assertEqual(state["seenDynamicIds"], [])
            self.assertIsNone(state["lastSuccessfulSyncAt"])
            self.assertEqual(state["lastError"]["code"], "bilibili_auth_expired")
            self.assertEqual(pipeline.read_json(jobs_path)["jobs"], [])

    def test_post_metadata_contains_required_fields(self) -> None:
        job = {
            "id": "bili-1",
            "output": {"file": "assets/generated/bili-1-v1.png", "revision": 1},
            "background": {
                "latitude": 48.1,
                "longitude": 2.1,
                "provider": "Panoramax",
                "creator": "Street Photographer",
                "sourceUrl": "https://example.test/picture",
                "license": "CC-BY-SA-4.0",
                "licenseUrl": "https://example.test/license",
            },
            "source": {
                "dynamicId": "1",
                "url": "https://www.bilibili.com/opus/1",
                "summary": "hello",
                "publishedAtBeijing": "2026-01-01T12:00:00+08:00",
            },
            "location": {
                "label": "Paris, France",
                "localTimeAtDynamic": "2026-01-01T04:00:00+01:00",
                "timezone": "Europe/Paris",
            },
            "generation": {"shotScale": "full_body", "poseFamily": "walk", "neckAccessory": "red_scarf"},
        }
        post = review.post_from_job(job)
        self.assertEqual(post["publishedAtBeijing"], "2026-01-01T12:00:00+08:00")
        self.assertEqual(post["localTime"], "2026-01-01T04:00:00+01:00")
        self.assertEqual(post["latitude"], 48.1)
        self.assertEqual(post["longitude"], 2.1)
        self.assertEqual(post["streetCreator"], "Street Photographer")

    def test_human_approval_and_regeneration_are_local_state_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            jobs_path = temp / "jobs.json"
            posts_path = temp / "posts.json"
            generation_state_path = temp / "generation-state.json"
            output_relative = "assets/generated/bili-1-v1.png"
            output_path = temp / output_relative
            output_path.parent.mkdir(parents=True)
            output_path.write_bytes(b"candidate")
            job = {
                "id": "bili-1",
                "status": "awaiting_review",
                "output": {"file": output_relative, "revision": 1},
                "agentQa": {"passed": True, "notes": "two arms visible"},
                "background": {
                    "latitude": 48.1,
                    "longitude": 2.1,
                    "provider": "Panoramax",
                    "creator": "Street Photographer",
                    "sourceUrl": "https://example.test/picture",
                    "license": "CC-BY-SA-4.0",
                },
                "source": {
                    "dynamicId": "1",
                    "url": "https://www.bilibili.com/opus/1",
                    "summary": "hello",
                    "publishedAtBeijing": "2026-01-01T12:00:00+08:00",
                },
                "location": {
                    "label": "Paris, France",
                    "localTimeAtDynamic": "2026-01-01T04:00:00+01:00",
                    "timezone": "Europe/Paris",
                },
                "generation": {
                    "revision": 1,
                    "shotScale": "full_body",
                    "poseFamily": "walk",
                    "placementSide": "left",
                    "neckAccessory": "red_scarf",
                },
            }
            pipeline.write_json(jobs_path, {"schemaVersion": 1, "jobs": [job]})
            pipeline.write_json(posts_path, {"schemaVersion": 1, "generatedAt": None, "posts": []})
            pipeline.write_json(generation_state_path, {"schemaVersion": 1, "recent": []})
            with (
                mock.patch.object(pipeline, "REPO_ROOT", temp),
                mock.patch.object(pipeline, "JOBS_PATH", jobs_path),
                mock.patch.object(review, "POSTS_PATH", posts_path),
                mock.patch.object(review, "GENERATION_STATE_PATH", generation_state_path),
                redirect_stdout(io.StringIO()),
            ):
                review.command_approve(argparse.Namespace(job_id="bili-1", notes="looks good"))
                self.assertEqual(pipeline.read_json(jobs_path)["jobs"][0]["status"], "approved")
                self.assertEqual(len(pipeline.read_json(posts_path)["posts"]), 1)

                review.command_regenerate(argparse.Namespace(job_id="bili-1", notes="try another pose"))

            regenerated = pipeline.read_json(jobs_path)["jobs"][0]
            self.assertEqual(regenerated["status"], "pending_generation")
            self.assertEqual(regenerated["generation"]["revision"], 2)
            self.assertIsNone(regenerated["output"])
            self.assertEqual(len(regenerated["history"]), 1)
            self.assertTrue(output_path.is_file())
            self.assertEqual(pipeline.read_json(posts_path)["posts"], [])


if __name__ == "__main__":
    unittest.main()
