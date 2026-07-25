## Building and running PayBrix in Docker (broadcast-safe)

Build once (rebuild whenever you actually intend to ship new code —
this is the deliberate "promote" step SENTINEL asked for; nothing
crosses in automatically):

    docker build -t paybrix-lead-engine .

Run it, binding ONLY to your current Tailscale IP (check it first with
`tailscale status` -- it has changed before, e.g. 100.77.221.118 ->
100.81.7.120), and passing secrets from the private, non-synced .env
that lives in THIS folder -- never the Sremium-Taha copy:

    docker run --rm ^
      --env-file "C:\Users\x\Desktop\lead engine v1.1 ANTI\.env" ^
      -v "C:\Users\x\Desktop\lead engine v1.1 ANTI\data:/app/data:ro" ^
      -v "C:\Users\x\Desktop\lead engine v1.1 ANTI\leads_vault.db:/app/leads_vault.db" ^
      -p 100.81.7.120:8000:8000 ^
      paybrix-lead-engine

Notes:
- `--env-file` injects values into the container's environment once at
  start; the .env file itself is never exposed inside the container's
  filesystem for something else to read or overwrite later.
- The `data` volume is mounted read-only (`:ro`) from the private
  folder -- not the synced one -- so ingestion input still can't be
  silently swapped by a synced file (R3).
- The `-p 100.81.7.120:8000:8000` form binds only that interface on
  the host, same principle as the run.bat fix from before, now
  enforced at the Docker layer too, not just the app's own bind.
- The `Sremium-Taha` synced folder is never referenced anywhere in
  this build or run -- structurally cannot reach anything the
  container executes.

## Still required regardless of Docker

Docker fixes the "synced file becomes executed code" class of bug
(B1, B3, R3, R4). It does NOT fix application logic bugs -- these
still need the code-level fixes SENTINEL listed:
- B2: stop loading .env by scanning the repo root; load from an
  explicit absolute path (still worth doing even with --env-file).
- B4: fail closed if bound to a non-loopback host with no API_KEY set.
- B5: disable docs_url/redoc_url/openapi_url by default.
- R1: column allow-list + READ_ONLY_MODE redaction.
- R2: validate/reject RFC1918/loopback/tailnet targets before fetching,
  follow_redirects=False with manual re-validation per hop.
- R5: rate-limit /api/log.

Docker is the boundary layer. The code fixes are still the substance.
