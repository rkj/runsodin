# Maintained fork

The `homelab` branch aggregates the deployed application changes. `main` tracks
upstream releases; contribution branches keep independent changes reviewable.
The deployment builds an exact `homelab` commit, never a moving branch or local
patch script. `Dockerfile.homelab` builds the complete frontend and copies the
complete backend from this repository. A digest-pinned official release provides
runtime dependencies; changing that base requires revalidating dependency and
schema compatibility. Runtime settings, credentials and printer registration
belong to the deployment repository, not this public fork.

Included changes address upstream issues #22 (Moonraker/SQLite lifecycle), #23
(PrusaLink credentials), #24 (printer-bound G-code and sliced plate imports), #25
(material enum/list recovery), and #26 / PR #27 (read-only external display).
No tests connect to printer hardware or start jobs.

Focused backend checks run in a disposable image with `--network none`, mounting
this source read-only at `/repo` and setting `ODIN_TEST_BACKEND=/repo/backend`:

```sh
python /repo/tests/homelab/test_moonraker.py /repo
python /repo/tests/homelab/test_prusalink.py
python /repo/tests/homelab/test_gcode_import.py /repo
python /repo/tests/homelab/test_plates.py
python /repo/tests/homelab/test_filament.py
python /repo/tests/homelab/test_ledger.py
```

Frontend tests render the actual components:

```sh
cd frontend
npm test -- src/components/printers/PrinterCredentials.test.tsx src/components/printers/LedgerDisplay.test.tsx src/pages/printers/PrinterList.test.tsx src/pages/jobs/UploadBinding.test.tsx
npm run build
```

The full upstream API/RBAC and security suites need an isolated service; do not
point the restart/rate-limit-reset fixtures at a production deployment.
