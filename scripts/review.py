#!/usr/bin/env python3
"""Human review state transitions for Komichi-generated images.

Approving updates the local posts feed. Regenerating preserves the previous file
for comparison and creates a new version number. This script never invokes Git.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pipeline


POSTS_PATH = pipeline.KOMICHI_ROOT / "data" / "posts.json"
GENERATION_STATE_PATH = pipeline.KOMICHI_ROOT / "data" / "generation-state.json"


def web_path(repo_path: str) -> str:
    prefix = "komichi/"
    return repo_path[len(prefix) :] if repo_path.startswith(prefix) else repo_path


def post_from_job(job: dict[str, Any]) -> dict[str, Any]:
    output = job.get("output") or {}
    background = job.get("background") or {}
    source = job.get("source") or {}
    location = job.get("location") or {}
    generation = job.get("generation") or {}
    if not output.get("file") or not background:
        raise pipeline.PipelineError("incomplete_job", f"Job {job['id']} has no reviewable output/background")
    return {
        "id": job["id"],
        "image": web_path(output["file"]),
        "imageRevision": output.get("revision"),
        "bilibiliDynamicId": source.get("dynamicId"),
        "bilibiliDynamicUrl": source.get("url"),
        "bilibiliSummary": source.get("summary"),
        "publishedAtBeijing": source.get("publishedAtBeijing"),
        "location": location.get("label"),
        "localTime": location.get("localTimeAtDynamic"),
        "timezone": location.get("timezone"),
        "latitude": background.get("latitude", location.get("latitude")),
        "longitude": background.get("longitude", location.get("longitude")),
        "streetProvider": background.get("provider"),
        "streetCreator": background.get("creator"),
        "streetSourceUrl": background.get("sourceUrl"),
        "streetLicense": background.get("license"),
        "streetLicenseUrl": background.get("licenseUrl"),
        "shotScale": generation.get("shotScale"),
        "poseFamily": generation.get("poseFamily"),
        "neckAccessory": generation.get("neckAccessory"),
        "approvedAt": pipeline.utc_now(),
    }


def remove_post(posts: dict[str, Any], job_id: str) -> None:
    posts["posts"] = [post for post in posts.get("posts", []) if post.get("id") != job_id]


def command_approve(args: argparse.Namespace) -> None:
    jobs_doc = pipeline.read_json(pipeline.JOBS_PATH)
    job = pipeline.find_job(jobs_doc, args.job_id)
    if job.get("status") != "awaiting_review":
        raise pipeline.PipelineError(
            "invalid_review_state",
            f"Job {job['id']} is {job.get('status')}; only awaiting_review can be approved",
        )
    output_path = pipeline.REPO_ROOT / job["output"]["file"]
    if not output_path.is_file():
        raise pipeline.PipelineError("missing_output", f"Review image is missing: {output_path}")

    decided_at = pipeline.utc_now()
    job["status"] = "approved"
    job["updatedAt"] = decided_at
    job["humanReview"] = {"decision": "approved", "decidedAt": decided_at, "notes": args.notes}

    posts = pipeline.read_json(POSTS_PATH)
    remove_post(posts, job["id"])
    post = post_from_job(job)
    post["approvedAt"] = decided_at
    posts.setdefault("posts", []).append(post)
    posts["posts"].sort(key=lambda item: item.get("publishedAtBeijing") or "", reverse=True)
    posts["generatedAt"] = decided_at

    generation_state = pipeline.read_json(GENERATION_STATE_PATH)
    generation = job.get("generation") or {}
    generation_state.setdefault("recent", []).append(
        {
            "jobId": job["id"],
            "shotScale": generation.get("shotScale"),
            "poseFamily": generation.get("poseFamily"),
            "placementSide": generation.get("placementSide"),
            "neckAccessory": generation.get("neckAccessory"),
            "acceptedAt": decided_at,
        }
    )
    generation_state["recent"] = generation_state["recent"][-20:]

    pipeline.write_json(pipeline.JOBS_PATH, jobs_doc)
    pipeline.write_json(POSTS_PATH, posts)
    pipeline.write_json(GENERATION_STATE_PATH, generation_state)
    print(
        json.dumps(
            {
                "jobId": job["id"],
                "status": "approved",
                "imagePath": str(output_path.resolve()),
                "postsPath": str(POSTS_PATH.resolve()),
                "gitAction": "none",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def archive_current_output(job: dict[str, Any], decision: str, notes: str | None) -> None:
    if job.get("output"):
        job.setdefault("history", []).append(
            {
                "output": job.get("output"),
                "agentQa": job.get("agentQa"),
                "humanDecision": decision,
                "humanNotes": notes,
                "decidedAt": pipeline.utc_now(),
            }
        )


def command_regenerate(args: argparse.Namespace) -> None:
    jobs_doc = pipeline.read_json(pipeline.JOBS_PATH)
    job = pipeline.find_job(jobs_doc, args.job_id)
    if job.get("status") not in {"awaiting_review", "approved", "rejected"}:
        raise pipeline.PipelineError(
            "invalid_review_state",
            f"Job {job['id']} is {job.get('status')}; it has no completed human-review candidate",
        )
    archive_current_output(job, "regenerate", args.notes)
    generation = job.setdefault("generation", {})
    generation["revision"] = int(generation.get("revision", 1)) + 1
    job["status"] = "pending_generation"
    job["updatedAt"] = pipeline.utc_now()
    job["output"] = None
    job["agentQa"] = None
    job["humanReview"] = {"decision": "regenerate", "decidedAt": pipeline.utc_now(), "notes": args.notes}

    posts = pipeline.read_json(POSTS_PATH)
    remove_post(posts, job["id"])
    posts["generatedAt"] = pipeline.utc_now()
    pipeline.write_json(pipeline.JOBS_PATH, jobs_doc)
    pipeline.write_json(POSTS_PATH, posts)
    print(
        json.dumps(
            {
                "jobId": job["id"],
                "status": job["status"],
                "nextRevision": generation["revision"],
                "preservedPreviousFiles": True,
                "gitAction": "none",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_reject(args: argparse.Namespace) -> None:
    jobs_doc = pipeline.read_json(pipeline.JOBS_PATH)
    job = pipeline.find_job(jobs_doc, args.job_id)
    if job.get("status") not in {"awaiting_review", "approved"}:
        raise pipeline.PipelineError("invalid_review_state", f"Job {job['id']} is not reviewable")
    archive_current_output(job, "rejected", args.notes)
    decided_at = pipeline.utc_now()
    job["status"] = "rejected"
    job["updatedAt"] = decided_at
    job["humanReview"] = {"decision": "rejected", "decidedAt": decided_at, "notes": args.notes}
    posts = pipeline.read_json(POSTS_PATH)
    remove_post(posts, job["id"])
    posts["generatedAt"] = decided_at
    pipeline.write_json(pipeline.JOBS_PATH, jobs_doc)
    pipeline.write_json(POSTS_PATH, posts)
    print(json.dumps({"jobId": job["id"], "status": "rejected", "gitAction": "none"}, ensure_ascii=False, indent=2))


def command_list(args: argparse.Namespace) -> None:
    jobs = pipeline.read_json(pipeline.JOBS_PATH).get("jobs", [])
    rows = [
        {
            "id": job["id"],
            "status": job.get("status"),
            "outputPath": (job.get("output") or {}).get("absolutePath"),
            "humanReview": job.get("humanReview"),
        }
        for job in jobs
        if not args.status or job.get("status") == args.status
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("approve", command_approve), ("regenerate", command_regenerate), ("reject", command_reject)):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("job_id")
        subparser.add_argument("--notes")
        subparser.set_defaults(func=handler)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--status")
    list_parser.set_defaults(func=command_list)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except pipeline.PipelineError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
