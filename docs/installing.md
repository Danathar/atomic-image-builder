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

`cosign` comes from Homebrew on both targets. The two differ in one way only:
Universal Blue images (Bluefin, Aurora, Bazzite) ship Homebrew already, and
Fedora Atomic (Silverblue, Kinoite) does not — so install it from
[brew.sh](https://brew.sh) there first. Then, on either:

```bash
brew install cosign
```

`rpm-ostree install cosign` is not an alternative on either one. cosign is not
packaged for Fedora at all, which is the same reason the Homebrew formula below
has to supply it — and layering a package onto the OS image and rebooting to
get one command would be the wrong mechanism on an atomic desktop even if it
were packaged.

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

### Installing the wrapper

```bash
curl -fsSLO https://github.com/Danathar/atomic-image-builder/releases/latest/download/aib &&
curl -fsSL  https://github.com/Danathar/atomic-image-builder/releases/latest/download/aib.sha256 | sha256sum -c - &&
install -m 755 aib ~/.local/bin/aib && rm aib
aib
```

`aib` and `aib.sha256` are attached to each release by
[`publish-wrapper.yml`](../.github/workflows/publish-wrapper.yml), both generated
from the same checkout of the tag, so the pair always agrees. `sha256sum -c -`
exits non-zero and prints `FAILED` if it does not.

The `&&` at the end of the first two lines is doing the same work it does in the
`cosign verify` command further down: it makes the check a gate rather than a
report. Pasted into a shell without them, each line runs regardless of what the
one above it returned, so a `FAILED` checksum — or a download that produced no
file at all — would still be followed by `install`, putting the unverified
wrapper on your `PATH` and running it. With them, nothing is installed unless
the file matches the checksum published with the release. The bare `aib` on the
last line stays unchained deliberately: if the install did not happen, nothing
new was placed on `PATH`.

Verifying the wrapper matters for the same reason the wrapper verifies the
image. It runs the image as root, and it forwards your GitHub credential into
it — and it decides *whether* to forward that credential. A wrapper is a
position upstream of every check it performs: a substituted one can call
`gh auth token` and never reach cosign at all. Fetching it from a release, and
checking it against a checksum published with that release, is what keeps the
thing doing the verifying from being the unverified part.

#### The `main` version

```bash
curl -fsSL https://raw.githubusercontent.com/Danathar/atomic-image-builder/main/contrib/aib -o ~/.local/bin/aib
chmod +x ~/.local/bin/aib
```

This is the bleeding edge: whatever is on the default branch at the moment you
run it, with no checksum to check it against and no release behind it. Use it
to pick up a wrapper fix before the next release, or to test a change. The
release install above is the recommended one.

The wrapper forwards your host's `gh` login when there is one (otherwise it
persists an in-container login across runs in a podman-managed volume), makes
your host's `rpm-ostree` state available to the system scan, and mounts your
local timezone. Either GitHub credential is forwarded only to an image whose
signature was verified — see [Verifying the image](#verifying-the-image). See
the comments at the top of [`contrib/aib`](../contrib/aib) for exactly what it
mounts and why.

### Plain `podman run`

If you would rather not use the wrapper script:

```bash
podman run --rm -it --pull=newer \
  ghcr.io/danathar/atomic-image-builder:latest
```

That forwards no GitHub credential, and the omission is deliberate. Nothing in
the command checks who built the image: it fetches a mutable tag and runs
whatever is behind it, as root. Handing `$(gh auth token)` to an image nothing
has vouched for is a much larger thing to agree to than running it — which is
why the wrapper withholds credentials from an image it did not verify, and why
this snippet withholds them too. What a signature answers here, and what
checking one costs, is [Verifying the image](#verifying-the-image).

The tool still starts; the steps that reach GitHub are the ones that stop
working. `gh auth login` inside the container gets those back, at the price of
logging in again every run — `--rm` discards the login along with the
container. To forward your host's token instead, verify first and run the
digest you verified — [Credentials follow the
signature](#credentials-follow-the-signature) has that command.

Staying current matters more than it looks. Podman's default is
`--pull=missing`, which pulls only when the image is absent locally — so once you
have pulled `latest` you would keep running that copy indefinitely, however far
the published image moves on. The tool bakes in its own action pins and template
snapshots, so an old image quietly generates repos from old pins. The `aib`
wrapper pulls on every run: explicitly on the verified path, since it has to
resolve a digest before it can check a signature, and with `--pull=newer`
otherwise.

### Verifying the image

**What a signature answers here.** Anyone can publish a container image under
any name, and the name alone proves nothing about who built it. A signature is
what makes that claim checkable: it lets your machine confirm the image came
out of *this project's own GitHub Actions workflow* before running it. That is
the whole question — not whether the image is good software, but whether it is
the one this project published or somebody else's standing where it should be.

Every published image is signed with keyless (OIDC) cosign at publish time — the
identity being verified is the publishing workflow itself, not a key anyone
holds. That is why the check below constrains a workflow path and an OIDC
issuer rather than naming a key: there is no key to steal, and no key for you
to have to trust.

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
| `cosign` not installed               | The wrapper refuses to run and says so, naming `brew install cosign`.            |
| Offline, or you want to opt out      | `AIB_SKIP_VERIFY=1 aib` runs the image unverified, and warns loudly that it did. |
| `AIB_IMAGE` points at your own build | Not verified — your build cannot satisfy this repository's certificate identity. |

### Credentials follow the signature

An image the wrapper did not verify gets no GitHub credentials: neither a token
from your host's `gh` login nor the `aib-gh` volume holding an in-container one.
Both of the rows above are unverified runs, so both of them are logged out.

That pairing is the point. Without it,

```bash
AIB_IMAGE=evil.example/evil/image aib
```

would not only run someone else's image as root — it would hand that image a
live credential for your GitHub account, which is a much larger thing to agree
to than "skip a check". The tool still starts; the steps that reach GitHub are
the ones that stop working.

The wrapper's own messages avoid the word "signature" and say "no way to check
who built it" instead, because that is the part that makes withholding a GitHub
login follow rather than seem arbitrary. When it declines, it prints the exact
command that undoes it — `env -u AIB_IMAGE aib`, or `env -u AIB_SKIP_VERIFY aib`
— rather than telling you to unset a variable. The usual way to arrive here is pasting a command with
the variable written in front of it, and there is then nothing left set to
unset; the command above works either way.

To forward credentials anyway — iterating on your own build of this image is
the case that needs it — say so explicitly:

```bash
AIB_ALLOW_UNVERIFIED_AUTH=1 AIB_IMAGE=localhost/my-own-build:dev aib
```

It warns each time. Set it only for an image you built yourself.

A release tag of the published image (`ghcr.io/danathar/atomic-image-builder:v1.2.3`)
*is* verified, because the identity below accepts tag refs as well as `main`.

To run the same check by hand — before a bare `podman run`, say, so that run
can be given a GitHub token — resolve a digest, verify that digest, and run it:

```bash
image=ghcr.io/danathar/atomic-image-builder
podman pull "$image:latest"
ref="$image@$(podman image inspect --format '{{.Digest}}' "$image:latest")"

cosign verify \
  --certificate-identity-regexp '^https://github\.com/Danathar/atomic-image-builder/\.github/workflows/publish-image\.yml@refs/(heads/main|tags/.+)$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  "$ref" &&
  podman run --rm -it -e GH_TOKEN="$(gh auth token)" "$ref"
```

Verifying `:latest` and then running `:latest` would be two lookups of a
mutable tag with a check in between, and it is the second one that decides what
runs. Resolving the digest once and handing the same `$ref` to both closes
that — the same reason the wrapper runs the digest it verified rather than the
tag that produced it. The `&&` does the rest of the work: if `cosign verify`
exits non-zero nothing runs, and `gh auth token` is never even called.

That command needs `cosign`; `brew install cosign` if you do not have it. The
plain `podman run` above deliberately does without both the check and the
token, and is the right shape when you do not want either.

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
