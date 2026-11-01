# Install Media Lab

One command. No accounts, no telemetry, nothing downloaded except Python
packages. Model weights and engines are installed later, by you, from inside
the app — each one shows its own license and size first.

```sh
git clone <this repository>
cd media-lab-studio
./install.sh
```

When it finishes it prints the URLs, the family code, and a QR code. Point
your phone's camera at the QR and the **VibeXStudio** app pairs to this
studio. Run `media-lab pair` any time to print it again.

`install.sh` is safe to re-run: it reuses the venv, never overwrites a code
file or anything under the data root, and re-renders the service. That makes
`git pull && ./install.sh` the update path.

## Which machine?

### A plain Mac (cloud-only)

No NVIDIA GPU, so the local engines are off. Run `./install.sh` — it starts the
studio in the foreground (Ctrl-C stops it). Open the printed URL, then in the
theme sheet → **Cloud providers**, paste your [fal.ai](https://fal.ai) key.
`fal-image` and `fal-video` appear as engines. Your key, your bill.

To keep it running without a terminal, install it as a launchd user agent:

```sh
./install.sh --service
```

It starts at login and restarts if it dies. `media-lab stop|start|restart|logs`
drive it.

### A DGX Spark or any Linux GPU box (the full studio)

`./install.sh` installs a **systemd --user** service (`media-lab.service`),
enables lingering so the studio survives logout and reboot, starts it and
waits until it answers. Then open the printed URL: the first-run engine shelf
offers LTX and the other local engines. Each shows its terms, size and time
before anything happens; the download and install run in the background and
you get a push when it is ready. Engines this repo has no automated path for
say so instead of pretending.

Needs Python ≥ 3.11 (or [uv](https://docs.astral.sh/uv/), which brings its
own), `ffmpeg`, and for the engines `docker` with the NVIDIA runtime.
`install.sh` tells you exactly what is missing and the apt line that fixes it.

### "I already run it"

You deployed Media Lab by hand, with your own unit or a copied tree. Run
`./install.sh` from the clone anyway: it builds the venv and the `media-lab`
CLI, sees your running server on the port, **leaves it alone**, and prints
the pairing QR for it. It never overwrites a service file it did not write
(look for `managed by media-lab install.sh` at the top). A data root that is a
real directory (not a symlink to the repo) is treated as a deployed copy and
left as it is.

## Flags

| flag | meaning |
| --- | --- |
| `--port N` | listen port (default 7863) |
| `--bind ADDR` | bind address (default `0.0.0.0`; use a tailnet IP to hide from the LAN) |
| `--service` / `--no-service` | service install on/off (default: on for Linux, off for macOS) |
| `--detach` | no service: start in the background, print, exit |
| `--yes` | never ask — for agents and scripts |
| `--uninstall` | remove the service and `.venv`; models, media, jobs and codes stay |

`MEDIA_LAB_HOME=/big/disk/media-lab ./install.sh` puts the data root
somewhere else. The server itself reads `~/media-lab-simple`, so the installer
keeps a symlink there pointing at your directory.

### Per-host settings (`config/local.env`)

Everything that differs between machines — bind address, tailnet address,
public hostnames behind a tunnel, where model weights and engine runtimes
live, the Sol-H3 install — is read from `~/media-lab-simple/config/local.env`.
It is gitignored; start from `config/local.env.example`:

```sh
cp config/local.env.example ~/media-lab-simple/config/local.env
$EDITOR ~/media-lab-simple/config/local.env
python3 -m media_lab_core.local_config     # show what resolves
```

Process environment overrides the file, the file overrides the defaults, and
the defaults describe a private single-machine install (`127.0.0.1`, no public
hosts).

Music: YuE2 is the primary song engine (`YUE2_KIT`, `YUE2_MODELS_ROOT`,
`YUE2_PORT`; weights are CC BY-NC 4.0, non-commercial use only) and the stem
separator is `MELBAND_ROFORMER_ROOT`; see [MUSIC.md](MUSIC.md) for the runtime
layout and the edit tools. The runner shell scripts source the same file and the systemd units
load it with `EnvironmentFile=`, so there is exactly one place to edit.

## Pairing from the phone

1. Install **VibeXStudio** on the phone.
2. Put the phone on the same Wi-Fi as the studio, **or** sign both into the
   same Tailscale tailnet (then it works from anywhere, including cellular).
3. Scan the QR the installer printed (`media-lab pair` shows it again). The
   link is `vibex://pair?medialab=<your studio URL>`; the app probes
   `/manifest.json` and adds a Media Lab tab.
4. No camera? In the app: **Media Lab → More options**, type the URL
   (`http://<ip>:7863`) and the family code.

The QR prefers the tailnet address when Tailscale is up, else the LAN address.
It is drawn for a dark terminal; on a light theme use `media-lab pair --no-invert`.

### The family code and the admin code

Two codes open the same door, and the code you type decides what you can do:

| Code | File (data root, mode 0600) | Who | What it opens |
| --- | --- | --- | --- |
| **Family code** | `access-code.txt` | everyone in the house, shared | make, edit, use and tidy the Library, manage the queue |
| **Admin code** | `admin-pin.txt` | the owner only | all of that, plus server settings: provider keys, engine installs, GPU profiles, a new family code |

Both are a few everyday words (e.g. `maple-otter-lantern-comet`); spaces,
dashes and capitals don't matter when typing. Every browser and paired app
enters the code **once** and stays signed in for a year. Nothing about the
network grants access — not being on the same Wi-Fi, not the tailnet, not
`localhost`: every device enters the family code.

- `media-lab code` prints the family code (`--admin`: the admin code;
  `--quiet`: names the file instead of printing it).
- `media-lab code --rotate` makes a new family code. The running server picks
  it up within a second — no restart — and **every device signed in with the
  old code is signed out at once**; each one enters the new code once. The
  owner can do the same from the studio with `POST /api/admin/family-code`
  (admin code only). `media-lab code --rotate --admin` replaces the admin code.
- Installs from before word codes keep their old codes (an 8-letter access
  code, a 4-digit admin code) until you rotate. The server log warns when a
  code is short; rotate both: `media-lab code --rotate && media-lab code --rotate --admin`.
- `media-lab code --set-family` sets a family code **you choose** instead of
  a random one. It reads the code from stdin (a hidden prompt, asked twice, in
  a terminal) or from `--from FILE`, never from the command line, and never
  prints it. It refuses the admin code, an empty code and anything longer than
  the gate's 80-character box. A short code (even 4 digits) is allowed: the
  server log then warns at every start that it is short and guessable, and the
  lockout below is what protects it. Like `--rotate`, it signs every family
  device out once.
- The codes are never written to the server log. Tools on the studio machine
  (the queue watchdog, the deploy script, the runners, `cut_cli`) use
  `local-token.txt` (also 0600) instead of a code.

### Wrong codes: the lockout

- **3 wrong codes within 10 minutes** from one network locks the code prompt
  for that network for **1 hour**. Every later lockout of the same network
  doubles: 2 h, 4 h, 8 h, ... up to 7 days. A whole day with no wrong code
  steps it back down one level.
- "One network" is one IPv4 address or one IPv6 /64 — through the Cloudflare
  tunnel, the address Cloudflare saw; on the tailnet or LAN, the device's own
  address. Everyone behind one home IPv4 address shares one counter.
- It guards the **code prompt only**. A browser or paired app that is already
  signed in keeps working during a lockout.
- The admin code has **its own counter**. While the prompt is shut for a
  network, only the admin code is checked there, so family typos never lock the
  owner out. Owner scripts sending the admin code in `X-Lab-Pin` use that
  counter too.
- `media-lab code --locks` lists who is locked out right now;
  `media-lab code --unlock` lifts every lockout and
  `media-lab code --unlock <address>` lifts one (`anon` = this machine's own
  scripts). The running studio applies it within a few seconds; the journal
  says how many were lifted, never which address.
- Lockouts are kept in `auth-attempts.json` (0600), so a restart does not
  clear them.

## The desktop app

The VibeXStudio desktop app can host Media Lab as a sidecar: it runs the same
server from its own bundle and shows the same QR. If you run both, give the
sidecar another port — or point the desktop app at this server's URL instead.

## `media-lab`

`install.sh` links `tools/media-lab` into `~/.local/bin` when that directory
exists; otherwise call it by path.

```
media-lab status [--json]      health, GPU, engines, service state
media-lab pair   [--json]      pairing QR, URLs, family code
media-lab code   [--rotate]    show / rotate the family code (--admin for the other)
media-lab code --set-family    set a family code you chose (stdin or --from FILE)
media-lab code --locks         who the code prompt is locked for right now
media-lab code --unlock [IP]   lift code-prompt lockouts (all, or one address)
media-lab start|stop|restart   service-aware; foreground fallback (start --foreground)
media-lab logs   [-f]          journal / launchd log / pid-mode log
media-lab setup  [--list]      the catalog planner + where the web wizard lives
media-lab uninstall [--yes]    same as ./install.sh --uninstall
```

`--json` on `status` and `pair` is for agents: `status` exits 0 only when
`/manifest.json` answers.

## Updating

```sh
git pull && ./install.sh
```

Re-renders the service and restarts it. Jobs, media, characters and voices
are untouched. A render in flight is interrupted — check `media-lab status`
first.

## Uninstall

```sh
./install.sh --uninstall
```

Stops and removes the service (or the background process), deletes `.venv`
and `install.json`. The data root — models, media, jobs, characters, voices,
access codes — is kept. Delete it yourself if you mean it. Engines installed
from the web shelf (docker images, ComfyUI runtimes) are also left in place.

## Troubleshooting

**"port 7863 is already taken"** — something else listens there. Another
Media Lab? `media-lab status` says. Otherwise `./install.sh --port 7870`.

**The phone says "No Media Lab answered there."** — the phone cannot reach
the address in the QR.
- Same Wi-Fi? Guest networks and "client isolation" block phone-to-laptop.
  Use Tailscale on both instead.
- Firewall: the server must accept inbound TCP on the port. Linux:
  `sudo ufw allow 7863/tcp`. macOS: System Settings → Network → Firewall →
  allow incoming for Python, or turn the firewall off for the test.
- Bound to the wrong interface? `--bind 0.0.0.0` (the default) listens on
  everything; a specific `--bind` listens only there.
- From the phone's browser, open `http://<ip>:7863/manifest.json`. JSON
  means the network is fine and the app is the problem; a timeout means the
  network is.

**Tailnet address shown but the phone is not on Tailscale** — scan anyway,
then use **More options** with the LAN URL from the summary instead.

**Service will not start** — `media-lab logs`. On Linux also
`systemctl --user status media-lab`; if `systemctl --user` itself fails you
have no user session bus — log in once over SSH as that user, or run
`./install.sh --no-service`.

**Stops when I log out (Linux)** — lingering could not be enabled. Run
`sudo loginctl enable-linger $USER` once.

**No QR, just a link** — the `qrcode` package is missing from the venv.
`./install.sh` again fixes it; the link and the typed-in fallback work
without it.

**Python too old** — needs 3.11+. Install `uv` and re-run: it fetches its own.

## Running the checks

The tests and the linter are developer-only: `./install.sh` installs
`requirements.txt` and never `requirements-dev.txt`, so a box deployed from
this repo has no `pytest`. Into the `.venv` the installer created:

```sh
uv pip install --python .venv -r requirements-dev.txt   # runtime + test tooling
.venv/bin/python -m pytest -q -rs -m "not spark"        # the suite a checkout can run
.venv/bin/ruff check .                                  # the linter
```

`requirements-dev.txt` pulls in `requirements.txt`, so that one install is
enough. `ruff check .` reports the tree's pre-existing findings as well; a
change is expected to add none.

`-m "not spark"` and `-rs` exclude the cases that need the studio host and print
the skip reasons. Use the marker, not `-k`: `-k "not spark"` also matches every
test id containing the word "spark" and silently drops 13 runnable cases.
[docs/HOST-DEPENDENT-TESTS.md](HOST-DEPENDENT-TESTS.md) is the inventory of what
is excluded and which capability each excluded case needs.

# Independent video setup status

The public first-run catalog does not currently provide a verified independent
video installer. Its former LTX and H3 recipes depended on private Maestro
container images; those recipes are now blocked and contain no executable steps.
They cannot be selected in setup or started through `/api/setup/install`, even
when a legacy container is running. This does not stop existing jobs or remove
an operator's existing engines.

A replacement must supply pinned sources, model/runtime terms, hardware checks
and successful qualification before becoming installable. This setup change does
not remove the legacy rendering routes or establish that the entire runtime is
independent of Maestro/WanGP; that migration remains in progress.
