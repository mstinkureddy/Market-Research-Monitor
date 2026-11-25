# Phone controls and report review

These steps apply only after the owner has separately approved workflow registration and a specific synthetic test. Use GitHub in a phone browser. The engine runs one bounded fixture batch. There are no in-chat start or stop controls.

## ON

1. In **Actions → Top 10 synthetic research → Run workflow**, select the protected `top10-synthetic-approved` branch. Choose the separately approved scenario and its absolute UTC deadline. Never use **Re-run jobs**.
2. Open the new run and copy the numeric ID, approved full head SHA, and exact test window. On the protected `top10-monitor-control` branch, use GitHub’s web editor to create `monitor-control/<run-id>.json` in a Verified owner-signed commit. Use the exact approved symbols, at most three from `F00`, `F01`, and `F02`. Use only `F00` for the short cancellation test. Replace the placeholder values below.

```json
{
  "schema": 1,
  "repository": "mstinkureddy/Market-Research-Monitor",
  "workflow": ".github/workflows/top10-synthetic-research.yml",
  "run_id": "<numeric run ID>",
  "attempt": 1,
  "commit": "<full approved head SHA>",
  "enabled": true,
  "synthetic": true,
  "symbols": ["F00"],
  "opens": "<whole-second UTC start>",
  "expires": "<whole-second UTC expiry>",
  "scenario": "success"
}
```

The ON permit may appear only once in signed path history. Its scenario must match the dispatch. Use whole-second timestamps ending in `Z`, with expiry at or before the approved dispatch deadline. If the handoff times out, leave it stopped and report the failed attempt. Never retry an acceptance run without new approval.

## OFF

For Test B, wait for the run log to show `ARMED`. Edit only `enabled` to `false` in the same permit file, using a second Verified owner-signed commit. After the log acknowledges signed OFF, tap **Cancel workflow** during the brief idle grace period. If the acknowledgment is unavailable, request cancellation immediately and record whether signed OFF was independently observed. Never flip the same permit back to ON.

## STATUS

Open the exact run in **Actions**. Record the run ID, attempt, head SHA, job state, conclusion, and timestamps. GitHub’s cancellation request and the application stop are separate events. The terminal status and application log provide separate evidence.

## REPORT

Give ChatGPT the exact successful GitHub run URL and approved SHA. Ask for the first attempt, the single terminal `research` job, current protected refs, Verified owner-signed permit and permit path history, artifact metadata with its SHA-256 digest, and the original ZIP. Validate through `GitHubControl.completed_proof()` and `review_archive()` against independently obtained GitHub evidence. Use the actual review time. Accept only the report for the exact successful run. Present old results as archive-only.

If direct retrieval is unavailable, download the original ZIP from that run’s **Artifacts** section and attach it in ChatGPT with the run URL. Independent GitHub metadata and permit evidence are still needed. If any required evidence cannot be recovered, report **UNVERIFIED · NO TRADE** with zero permitted contracts. Do not treat a screenshot or a pasted dashboard as authenticated evidence. Test B must never yield an accepted report.
