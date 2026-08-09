# Komichi image-generation workflow

Run from `/Users/zhang-ge-hao/codex-project/video-making/komichi-4am.github.io`.

## Scope

Process image jobs whose status is `pending_generation` (legacy `pending` test jobs may also be processed). Use the built-in image generation tool through the `imagegen` skill; do not call a repository API key or an ad-hoc image SDK.

Every generated image requires human review. Agent QA is only a preflight check. Never mark a newly generated image as published or approved, and never run `git add`, `git commit`, or `git push` from this workflow.

## Stable inputs

Character identity references:

1. `assets/reference/komichi-fullbody.png` — full-body proportions and outfit.
2. `assets/reference/komichi-face-smile.png` — face, hair, clips, eye color, and relaxed expression.
3. `assets/reference/komichi-face-surprised.png` — face and alternate expression.

Never send `fanwork-policy.png` to the image generator.

Production street-photo jobs are in `data/jobs.json`; shared source, license, and attribution records are in `data/backgrounds.json`. `data/test-batch.json` contains regression examples only and is not part of the daily queue. Treat each production job's selected background as the edit target and all three character images as identity references.

## Per-job generation

Use case: `compositing`, with strong `identity-preserve` and `lighting-weather` constraints.

For every pending job:

1. Inspect the street photograph before generation.
2. Use one built-in image-generation call for that distinct job.
3. Follow the job's requested shot scale, pose, expression, and placement intent.
4. Convert the real street photograph to a restrained pre-dawn blue hour: blue-cyan ambient light, visible architecture and road detail, lifted shadows, no black night scene.
5. Add a newly generated Komichi. Never reuse the standing reference pose.
6. Keep Komichi unmistakably 2D anime/cel-shaded. Do not make her photorealistic, 3D, or cosplay-like.
7. Preserve identity: long black hair, blunt bangs, red headband, yellow warning-sign clip on her right side (viewer left), blue left-arrow clip on her left side (viewer right), violet eyes, black outfit with dark-red accents, black skirt, black knee socks, and black shoes.
8. Preserve the two tear moles shown in both face references: exactly two small dark moles on her anatomical left cheek (the viewer-right/image-right cheek in a frontal view), vertically stacked one above the other. Do not omit them, merge them into one, mirror them to her other cheek, or add extra facial moles. Keep this constraint explicit even in wider shots; when the face is visible, the two-mole arrangement must remain readable at the available resolution.
9. Choose exactly one neck accessory independently for every job: either `red_scarf` or `traffic_light_pendant`. The choice must be random with a long-run 50/50 target and must not depend on shot scale, crop, pose, placement side, or face size. A close-up may use the scarf; a full-body or environmental shot may use the pendant. Never show both at once. When using the pendant, use the black sailor-style neckline with dark-red stripes from the face references and no scarf. Record the chosen value in job metadata and `generation-state.json`; avoid the same choice more than twice in a row when possible.
10. Preserve the street as a real photograph. Do not redesign, repaint, move, add, or remove buildings, road markings, vehicles, poles, street signs, or readable storefront elements. The stylized character is the intentional visual intrusion.
11. Keep at least one real road or traffic sign visible and do not cover the principal sign. Exception: when `mentionedLocation.resolution` is `catalog_alias` or `manual_catalog`, staying in the place named by the dynamic is more important than the sign; a signless real street photo is allowed only when no signed candidate is available there.
12. Make the action physically natural: correct hands, plausible balance, grounded feet for full-body shots, and perspective-consistent scale.
13. Enforce complete anatomy: exactly two arms and two hands, with five plausible fingers per visible hand; exactly two legs and two feet when the framing should show them. A limb may leave the frame only through a natural crop implied by the requested shot scale. Reject missing, fused, duplicated, floating, hidden-without-cause, or abruptly truncated limbs.
14. No text, caption, speech bubble, logo, frame, or new watermark.
15. All content must be all-ages and without harmful or degrading depiction.

## Diversity controller

Read `data/generation-state.json` before choosing any unprescribed creative detail. Avoid repeating the same shot scale, pose family, or frame side within the last three accepted images. Choose `neckAccessory` separately from all of those variables; do not infer it from the camera framing.

Target long-run distribution:

- environmental wide: 15%
- full body: 30%
- knee-up / three-quarter: 25%
- waist-up: 20%
- close-up: 10%

Prefer context-aware actions such as walking and looking back, tiptoeing to read a sign, crouching and pointing toward a sign, holding the scarf in a breeze, peeking into frame, carrying a warm drink, or making a playful sleepy four-o'clock expression. Do not default to a V-sign.

## Output and QA

Save accepted project-bound images under `assets/generated/` using the job's `output` filename. Never overwrite an accepted image; use a versioned sibling if regeneration is necessary.

Inspect every result and reject it if any of these checks fail:

- identity or hair-clip sides drift;
- the two viewer-right tear moles are missing, merged, misplaced, or not vertically stacked when the face is visible;
- the neck accessory is missing, both accessories appear, or the accessory choice tracks the shot scale instead of the per-job random choice;
- the street stops looking photographic;
- the requested shot scale or pose is not followed;
- hands, limbs, balance, ground contact, or perspective are implausible;
- any expected arm, hand, leg, or foot is missing, fused, duplicated, or unnaturally truncated;
- the principal road sign is covered or removed;
- the image is too dark to read as blue hour;
- unwanted text, watermark, border, or extra character appears.

For a rejected result, make one targeted retry. If the retry fails, leave the job `pending_generation` and report the failure; do not silently accept it.

For a result that passes Agent preflight, save it under a versioned filename, set the job status to `awaiting_review`, and record the output path, prompt version, `neckAccessory`, generation timestamp, and Agent QA notes. Do not append it to the published diversity history until the user approves it.

At the end of the run, return each absolute output path in the Scheduled conversation, together with a short QA summary and the job id. Explicitly ask the user to choose approval or regeneration. Do not stage, commit, or push any file.
