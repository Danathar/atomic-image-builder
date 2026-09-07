# Installing

Three ways to run Atomic Image Builder. Pick one; they are the same tool.

| Path                   | Command                     | Tracks                      | Needs                                     |
| ---------------------- | --------------------------- | --------------------------- | ----------------------------------------- |
| [Homebrew](#homebrew)  | `aib-tool`                  | Tagged releases             | `brew`, plus host `dnf5` and `rpm-ostree` |
| [Podman](#podman)      | `aib`                       | `main`, rebuilt every merge | `podman`                                  |
| [Source](#from-source) | `./atomic_image_builder.py` | Your checkout               | Python 3.10+, plus the tools below        |

The tool needs `dnf5` and `rpm-ostree`. Where they come from depends on how you
run it. With Homebrew or a source checkout they come from the host — which is
why those paths target Fedora Atomic and Universal Blue desktops, where both are
already present. The container image bundles its own, so Podman is
almost the whole prerequisite there — the `aib` wrapper also wants `cosign`, to
verify the image's signature before running it.

What the container cannot bundle is your host's rpm-ostree *state*: it has no
access to the host's system D-Bus, so the system scan depends on the `aib`
wrapper handing that state in. See
[Limitations of running in a container](#limitations-of-running-in-a-container).

---

## Homebrew

If you already have [Homebrew](https://brew.sh/) — Universal Blue images such as
[Bazzite](https://bazzite.gg), [Bluefin](https://projectbluefin.io), and [Aurora](https://getaurora.dev) ship with it —
this is the shortest path. It
installs the tool as an ordinary command and brings `gum`, `git`, `gh`, and
`cosign` along with it, none of which are in Fedora's own repositories:

```bash
brew tap danathar/aib https://github.com/Danathar/atomic-image-builder
brew install danathar/aib/atomic-image-builder
aib-tool
```

Update it the way you update everything else:

```bash
brew upgrade atomic-image-builder
```

> [!NOTE]
> Requires the `v0.9.0` release or later. The formula installs a published
> release archive and verifies its checksum, so if no matching release exists
> yet, `brew install` stops with a checksum mismatch rather than installing
> anything.

The installed command is **`aib-tool`**, not `aib`. The container wrapper below
already installs `aib` into `~/.local/bin`, which on a normal PATH comes before
Homebrew's `bin` — if both used the same name, whichever came first would
silently win and you would have no way to tell which one you were running.
Distinct names mean you can have both installed and choose deliberately.

Homebrew tracks tagged releases, while the container image tracks `main` and
republishes on every merge. If you want the newest changes the moment they land,
use the container; if you want stable versions, use Homebrew.

---

## Podman

Run the tool as a container — no local clone or dependency install needed, with
`gum`, `git`, `gh`, `cosign`, and the `rpm-ostree` client all bundled in. Podman
is the only prerequisite for a bare `podman run`; the `aib` wrapper additionally
needs `cosign` on the host, because it verifies the image's signature before
running it. See [Verifying the image](#verifying-the-image).

### Using the wrapper script

```bash
curl -fsSL https://raw.githubusercontent.com/Danathar/atomic-image-builder/main/contrib/aib -o ~/.local/bin/aib
chmod +x ~/.local/bin/aib
aib
```

The wrapper forwards your host's `gh` login when there is one (otherwise it
persists an in-container login across runs in a podman-managed volume), makes
your host's `rpm-ostree` state available to the system scan, and mounts your
local timezone. See the comments at the top of [`contrib/aib`](../contrib/aib)
for exactly what it mounts and why.

### Plain `podman run`

If you would rather not use the wrapper script:

```bash
podman run --rm -it --pull=newer \
  -e GH_TOKEN="$(gh auth token)" \
  ghcr.io/danathar/atomic-image-builder:latest
```

Staying current matters more than it looks. Podman's default is
`--pull=missing`, which pulls only when the image is absent locally — so once you
have pulled `latest` you would keep running that copy indefinitely, however far
the published image moves on. The tool bakes in its own action pins and template
snapshots, so an old image quietly generates repos from old pins. The `aib`
wrapper pulls on every run: explicitly on the verified path, since it has to
resolve a digest before it can check a signature, and with `--pull=newer`
otherwise.

### Verifying the image

Every published image is signed with keyless (OIDC) cosign at publish time — the
identity being verified is the publishing workflow itself, not a key anyone
holds.

**The `aib` wrapper does this for you, on every run.** It is the path where
nobody thinks about pulling: it fetches a mutable tag each time, runs it as root
in the container, and forwards your GitHub token into it. A signature that is
only checked once, by hand, at install time protects none of that — so the
wrapper verifies before it runs, and runs the exact digest it verified rather
than re-resolving the tag afterwards. A failed check aborts; it does not warn
and continue.

Two things follow from that, both deliberate:

| Situation                            | What happens                                                                     |
| ------------------------------------ | -------------------------------------------------------------------------------- |
| `cosign` not installed               | The wrapper refuses to run and says so. Install cosign, or opt out explicitly.   |
| Offline, or you want to opt out      | `AIB_SKIP_VERIFY=1 aib` runs the image unverified, and warns loudly that it did. |
| `AIB_IMAGE` points at your own build | Not verified — your build cannot satisfy this repository's certificate identity. |

A release tag of the published image (`ghcr.io/danathar/atomic-image-builder:v1.2.3`)
*is* verified, because the identity below accepts tag refs as well as `main`.

To run the same check by hand — against a bare `podman run`, say:

```bash
cosign verify \
  --certificate-identity-regexp '^https://github\.com/Danathar/atomic-image-builder/\.github/workflows/publish-image\.yml@refs/(heads/main|tags/.+)$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  ghcr.io/danathar/atomic-image-builder:latest
```

The `refs/(heads/main|tags/.+)` alternation matters: `latest` is signed from a
push to `main`, but a release publishes from a tag ref, so its certificate
identity ends `@refs/tags/<tag>` instead — a regexp that only matches `main`
would reject a perfectly good release digest. It is the same regexp the wrapper
uses, so the two cannot disagree about what counts as a genuine image.

### Distrobox

[Distrobox](https://distrobox.it/) integrates the container with your host: it
shares your home directory, so your host `gh` login is reused directly, and host
system access, so the system scan can read the host's `rpm-ostree` state — no
wrapper or manual mounts needed:

```bash
distrobox create --name aib --pull --image ghcr.io/danathar/atomic-image-builder:latest
distrobox enter aib -- aib-tool
```

`distrobox create --pull` fetches the image at creation time, but `distrobox
enter` never re-pulls afterwards — unlike `podman run --pull=newer`, there is no
per-run freshness check. To pick up a newer image you have to recreate the box:

```bash
distrobox rm aib
distrobox create --name aib --pull --image ghcr.io/danathar/atomic-image-builder:latest
```

Distrobox shares your host home directory, so recreating discards only whatever
you installed inside the box, not your own files or your `gh` login.

### Limitations of running in a container

| Feature                                                          | Containerized (`podman run` or distrobox)                                                                                                                                                               |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Create/update image repos, view build status, rotate signing key | Full fidelity                                                                                                                                                                                           |
| Create Image From This System (the scan)                         | Works via the `aib` wrapper or distrobox; unavailable with a bare `podman run` (no host state)                                                                                                          |
| Package search                                                   | Works, but the image ships with no DNF metadata, so the first search offers to download it. The `aib` wrapper keeps that download in a named volume; with a bare `podman run --rm` it repeats every run |
| Local Podman test build                                          | Not available — the option reports this and does nothing; see below                                                                                                                                     |

The image includes `podman` only because `rpm-ostree` (a required dependency the
tool checks for at startup) pulls it in transitively. A nested build inside the
container is not supported, so the image tells the tool to make its "Test build
locally (podman)" option show a clean "not available in this environment"
message rather than attempt a build that would fail. Run the tool
[from source](#from-source) if you need local test builds.

---

## From source

If you would rather run the script directly, you will need these on your host:

- Python 3.10 or newer
- `gum`, `git`, `gh`, and `cosign`
- `dnf5` (used for package-name validation) and `rpm-ostree` (used for system scanning)
- Optional: `podman`, for local Containerfile test builds

The app checks for the required tools at startup and exits if any are missing. On
Universal Blue and Fedora Atomic desktop images, `dnf5` and `rpm-ostree` are
already present; install the rest with Homebrew:

```bash
brew install gum git gh cosign
```

Then log in to GitHub, clone the repo, and run the tool:

```bash
gh auth login
git clone https://github.com/Danathar/atomic-image-builder.git
cd atomic-image-builder
./atomic_image_builder.py
```

If the script is not already executable on your system, make it executable once
with `chmod +x atomic_image_builder.py`.

---

## Command-line options

The tool is a guided menu, so there is almost nothing to pass it. The two options
it does take work the same however you installed it:

| Option            | What it does                          |
| ----------------- | ------------------------------------- |
| `-h`, `--help`    | Print a short usage summary and exit. |
| `-V`, `--version` | Print the tool version and exit.      |

The command name depends on how you installed it: `aib` for the container
wrapper, `aib-tool` for the Homebrew install and inside the container image, or
`./atomic_image_builder.py` from a source checkout. The tool itself is the same
either way.

---

## If a container package with that name already exists

GitHub does not delete a repo's container packages when you delete the repo. The
leftover package keeps the Actions permissions of the repo that created it, so a
**new** repo with the same name cannot push to it — GitHub treats it as a
different repo regardless of the matching name. The build succeeds all the way
through and then fails at its final push with
`denied: permission_denied: write_package`.

The tool checks for this before creating the repo and asks whether to continue.
To clear it, open
`https://github.com/users/<you>/packages/container/<name>/settings` and either:

- **Delete the package**, then continue — the first build recreates it, correctly
  linked; or
- **Continue first**, then add the new repo under *Manage Actions access* with
  the **Write** role before the build reaches its push step. That selector only
  lists repositories that already exist, so it cannot be done before the repo is
  created. If the push already failed, grant access and re-run the job.
