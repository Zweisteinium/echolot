Answers of the Sockseek 3.0.6 daemon's job API, recorded in mock mode by `tools/daemon_check.py --mock ... --record ...`
(one peer, `local`; three short test files). Each file holds the request and the status and JSON the daemon answered.
For the contract tests of the Soulseek backend (PLAN.md phase 3b); record `v4/` the same way when v4 is out.

Findings:
- A search without results ends `Succeeded` with `discoveryRawResultCount: 0`, not failed.
- `downloads/files` answers a list of song jobs; the finished one has `payload.downloadPath`.
- A running download can be cancelled (`Cancelled`); every transfer failing ends `Failed` / `AllDownloadsFailed`.
- A Manual song job accepts a cancel (202) but stays `AwaitingSelection`: use search jobs + `downloads/files` instead.
- An unknown job id answers 404 with no body. `GET /api/openapi.json` answers 500 (image built without OpenAPI documents).
