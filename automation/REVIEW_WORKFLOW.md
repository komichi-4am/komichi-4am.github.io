# Komichi human-review workflow

Run from `/Users/zhang-ge-hao/codex-project/video-making/komichi-4am.github.io` only after the user explicitly reviews a candidate.

## `重新生成 JOB_ID`

1. Run `python3 scripts/review.py regenerate JOB_ID --notes "USER_NOTES"`.
2. Preserve the rejected image for comparison; do not overwrite or publish it.
3. The job returns to `pending_generation` with a new revision number. A later scheduled or manually requested run may generate the next version.
4. Do not stage, commit, or push anything.

## `提交 JOB_ID`

1. Reconfirm that the named job is `awaiting_review` and that the path shown to the user is the current output.
2. Run `python3 scripts/review.py approve JOB_ID --notes "USER_NOTES"`. This makes the local post visible in `data/posts.json` but does not use Git.
3. Inspect the exact changed-file scope. Stage only the approved image and the Komichi metadata/state files needed for that job; never sweep unrelated workspace changes into the commit.
4. Commit and push only because this reply is the user's explicit publication decision. Report the commit and the expected Pages URL.
5. If approval succeeds but Git authentication or push fails, leave the local approved state intact and report the failure. Do not regenerate or discard the candidate.

Never treat words such as “看看”, “还行”, or silence as publication approval. If the job id is ambiguous, ask which candidate the user means before changing state.
