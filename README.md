# Komichi at 4 AM

This directory contains the local pipeline and GitHub Pages site for the Komichi fan project.

Live site: <https://komichi-4am.github.io/>

The pipeline now separates deterministic code, Agent work, and human publication approval:

1. `scripts/pipeline.py` detects unseen Bilibili dynamics, prioritizes any place explicitly named as being at 04:xx, otherwise chooses another 04:xx location, discovers Panoramax candidates, and owns job state;
2. the scheduled Agent generates and performs strict visual preflight, then records an `awaiting_review` image;
3. the user approves or requests regeneration through `scripts/review.py`;
4. only approved jobs enter `data/posts.json`, which powers the feed-style site;
5. no scheduled run stages, commits, or pushes Git changes.

Character QA treats the two vertically stacked tear moles on her anatomical left cheek (viewer-right in a frontal view) as a required identity feature. Her black pleated skirt is always an over-knee midi skirt with the hem clearly below the kneecaps in the upper-calf area, never knee-length or shorter. Each image also uses exactly one neck accessory—red scarf or traffic-light pendant—selected independently of shot scale; a pendant may be naturally occluded in a genuine side/back pose as long as no scarf or substitute accessory appears.

Explicit phrases such as `现在是冰岛的凌晨四点` and event-style phrases such as `我们法属波里尼西亚的甘比尔群岛……凌晨四点见` are matched against the aliases in `data/locations.json`. A resolved named place is authoritative: the background search stays there, prefers a real road sign, and may use a signless real street photo when no signed view is available. Unknown place names stop in `needs_location_resolution` instead of silently assigning another country. When an event-style mention conflicts with the publication timestamp, a confirmed place override must be recorded explicitly; the job must never be silently relocated to make the timestamp fit.

The Bilibili monitor uses a persistent login at `~/.config/bilibili/cookies.json`. Create it once by scanning a QR code:

```sh
python3 scripts/bilibili_login.py login
python3 scripts/bilibili_login.py status
```

The file lives outside the repository, is restricted to the current macOS user (`0600`), and is refreshed after successful runs. Cookie values are never printed. If the login is missing or expired, or Bilibili returns HTTP/code `412`, the workflow stops immediately: it does not retry through a browser, invent dynamics, or advance the monitor cursor.

On the first successful sync, every dynamic returned by the configured feed window receives a job. `backfill-missing` is the explicit recovery path for older feed entries that were previously marked seen without a job; it is idempotent and never rewinds the monitor cursor.

## Important files

- `automation/IMAGE_WORKFLOW.md`: durable instructions used by Codex.
- `automation/DAILY_WORKFLOW.md`: full scheduled-run instructions and the no-publish boundary.
- `automation/REVIEW_WORKFLOW.md`: explicit `提交` / `重新生成` behavior after human review.
- `config.json`: Bilibili account, street providers, and generation limits.
- `data/jobs.json`: authoritative generation and human-review queue.
- `data/posts.json`: approved-only website feed.
- `data/monitor-state.json`: Bilibili cursor and last error.
- `data/locations.json`: candidate locations and IANA timezones.
- `data/test-batch.json`: accepted v1 examples plus v2 regression jobs for tear-mole and accessory independence checks.
- `data/backgrounds.json`: source, location, license, and attribution metadata.
- `data/generation-state.json`: recent shot/pose history used to prevent repetition.
- `assets/generated/`: versioned generated review candidates and approved composites.

## Commands

```sh
python3 scripts/bilibili_login.py login
python3 scripts/bilibili_login.py status
python3 scripts/pipeline.py sync
python3 scripts/pipeline.py backfill-missing --limit 8
python3 scripts/pipeline.py status --json
python3 scripts/pipeline.py validate
python3 scripts/pipeline.py apply-mentioned-location JOB_ID [--location-id LOCATION_ID]
python3 scripts/pipeline.py prepare-background JOB_ID
python3 scripts/pipeline.py record-output JOB_ID /absolute/image.png --qa "preflight notes"
python3 scripts/review.py approve JOB_ID --notes "looks good"
python3 scripts/review.py regenerate JOB_ID --notes "missing arm"
```

The scheduled report asks for `提交 JOB_ID` or `重新生成 JOB_ID`. Only the first phrase is an explicit publication decision; approval updates local metadata first, then Git publication is handled as a separate, scoped step.
