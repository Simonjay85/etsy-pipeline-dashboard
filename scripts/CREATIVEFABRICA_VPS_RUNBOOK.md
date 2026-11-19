# Creative Fabrica survey on VPS

Source session: `20260913_235156_d6e606` (Hermes Desktop, Etsy project).

This moves the survey execution and checkpoint to the VPS. It does not replicate
the Hermes conversation database and is not an ANNA task. ANNA Core and its
existing task queue were not changed. No browser cookies or provider tokens were
copied. Public pages are read using the existing VPS Chromium CDP service.

## Runtime

- SSH alias: `templystudio`
- Root: `/home/ubuntu/apps/cf-kdp-survey`
- User service: `cf-kdp-survey.service`
- Browser: `hermes-chromium-cdp.service`, loopback CDP port 9223
- Python: isolated `venv311`, Playwright 1.58.0
- Authoritative data: `output/creativefabrica/` under the runtime root
- Local original data remains a snapshot; `vps_handoff.json` prevents the legacy
  local CLI from starting. Local Hermes cron `43d9e157df44` is paused.

The process runs independently of SSH and Mac power. It starts at VPS boot with
user linger, restarts on transient process errors, and saves every product. A
10,000 product-request UTC-day ceiling and ten-second minimum delay bound the
survey rate. It waits for the next UTC day at the ceiling. It stops after verified
catalog completion; it does not automatically start a new recrawl.

CSV data is atomically replaced before checkpoint updates. On restart, verified
CSV rows are skipped even if the checkpoint was interrupted. Legacy error rows
are retried first. `favorite_display` preserves the site's text, and
`favorite_estimate` may be approximate for K/M abbreviations. Changing live catalog
pagination means this is a rolling survey, not a frozen catalog snapshot.

Individual product HTTP 403/404/410 responses and missing/unparseable favorite fields are retained as unavailable gaps
without a numeric favorite and are not retried automatically on restart. Three
consecutive product 403 responses stop the survey. Category HTTP 403, HTTP 401/429,
unrecognized categories, or ambiguous catalog termination
produce `blocked_review_required` and exit 78. Systemd does
not repeatedly restart an access-blocked worker. A service being enabled does not
prove the survey is making progress; inspect status timestamps and CSV evidence.

## Inspect

```sh
ssh templystudio 'systemctl --user status cf-kdp-survey.service --no-pager'
ssh templystudio 'cat /home/ubuntu/apps/cf-kdp-survey/output/creativefabrica/vps_status.json'
ssh templystudio 'journalctl --user -u cf-kdp-survey.service -n 20 --no-pager'
```

The VPS status includes the original Hermes session ID, current category page,
total rows, verified favorite counts, runtime host, and any blocker.

Download CSV to a **new snapshot filename**, not over the preserved Mac checkpoint:

```sh
scp templystudio:/home/ubuntu/apps/cf-kdp-survey/output/creativefabrica/kdp_interiors_favorites_all.csv ./kdp_interiors_favorites_vps_download.csv
```

## Restart / rollback

After addressing a recorded blocker, restart only this service:

```sh
ssh templystudio 'systemctl --user restart cf-kdp-survey.service'
```

To return execution to Mac: first stop and disable `cf-kdp-survey.service`, confirm
the process has stopped, then retrieve the latest VPS CSV and checkpoint together.
Back up the local snapshot before replacing it. Archive `vps_handoff.json` only
after the source of truth has been transferred, and resume local cron if desired.
Never run both copies against independent changing checkpoints.

## Validation

Local focused tests: `python -m pytest -q scripts/test_creativefabrica_vps_survey.py`.
Live acceptance: public category and favorite element read on VPS, transferred
file hashes matched, three-product sample succeeded, and service was started.
A controlled worker restart continued collecting without duplicate CSV product IDs.
The initial worker then stopped at an individual product HTTP 403; the updated
worker records this as a gap and uses the consecutive-denial stop described above.
Full VPS reboot, Mac power-off, and 24-hour elapsed soak require separate evidence;
they must not be inferred from the service starting.
