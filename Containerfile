# Containerfile for the atomic-image-builder tool image, published to
# ghcr.io/danathar/atomic-image-builder. This packages the beginner TUI and
# all of its runtime dependencies so it can be run with `podman run` or
# distrobox without a local clone. The primary install method is still
# `git clone` + run the script directly (see README.md); this image is an
# alternative for people who would rather not install the dependencies on
# their host.
FROM registry.fedoraproject.org/fedora:44

LABEL org.opencontainers.image.source="https://github.com/Danathar/atomic-image-builder" \
      org.opencontainers.image.description="Beginner-focused terminal tool for creating and updating GitHub-backed bootc image repos" \
      org.opencontainers.image.licenses="GPL-3.0-only"

# git, rpm-ostree (client only — see AIB_RPM_OSTREE_STATUS_FILE in
# maintenance_notes.txt for why the container can't use it directly against
# the host), gh, and gum. gh and gum come from their own official repos.
#
# Note: rpm-ostree is a hard startup requirement of the tool, and it pulls in
# bootc, which hard-requires podman. So podman ends up in this image even
# though we do not install it directly. Because podman is present, the tool
# would otherwise *attempt* a nested "Test build locally" build (unsupported
# here, and it fails) instead of degrading. We set AIB_DISABLE_LOCAL_BUILD
# below so that option shows a clean "not available" message instead. Local
# test builds remain a clone-and-run feature — see the README limitations.
#
# Both signing keys are pinned by sha256, checked before anything is imported
# or installed, and each repo file points at the *verified local copy* rather
# than at a URL — so nothing here trusts a key merely because the host serving
# the packages also served it.
#
# That circularity is the thing being fixed. `gpgcheck=1` with a `gpgkey=`
# URL on the package origin proves only that whoever served the package also
# served the key; anyone able to serve repo.charm.sh could serve a matching
# pair and the check would pass. Pinning moves the trust root into this file,
# where changing it is a reviewed commit rather than a build-day download.
#
# The gh repo file is written here rather than fetched with
# `config-manager --from-repofile=https://…`, which pulled repository
# configuration — including the `gpgkey=` line that names the trust root —
# over the network at build time. The contents are upstream's, verbatim, with
# only that one line repointed at the verified copy. Writing it ourselves is
# also why dnf5-plugins is gone: config-manager was its only user here, and
# `repoquery` and `makecache`, the two dnf5 subcommands the tool itself runs,
# are core dnf5 commands rather than plugins.
#
# Keys are verified in /tmp and only then installed to /etc/pki/rpm-gpg, so an
# unverified key never occupies the path the repo files trust. The checksums
# go through a file rather than `echo … | sha256sum -c -`, for the reason
# spelled out by the cosign install below.
#
# To re-pin — which a key rotation upstream will require, loudly — see
# maintenance_notes.txt, "Third-Party Repository Trust Roots". The fingerprints
# these digests correspond to, for whoever does that:
#   Charm       ED92 7B38 BE98 1E53 CA09  153D 03BB F595 D4DF D35C
#   GitHub CLI  7F38 BBB5 9D06 4DBC B3D8  4D72 5612 B364 6231 3325
RUN dnf5 -y install curl && \
    curl -fsSL -o /tmp/RPM-GPG-KEY-charm https://repo.charm.sh/yum/gpg.key && \
    curl -fsSL -o /tmp/RPM-GPG-KEY-gh-cli https://cli.github.com/packages/githubcli-archive-keyring.asc && \
    printf '%s\n' \
      "bfccddb028d4d6ad5758fc9c9778337fb446a8e5fb270894011616963e9a9c8a  /tmp/RPM-GPG-KEY-charm" \
      "cec6e9ed82d3949ca5f4428cc968b41ef5e7416cb3653cdfc2a421977663bbfd  /tmp/RPM-GPG-KEY-gh-cli" \
      > /tmp/keys.sha256 && \
    sha256sum -c /tmp/keys.sha256 && \
    install -D -m 0644 /tmp/RPM-GPG-KEY-charm /etc/pki/rpm-gpg/RPM-GPG-KEY-charm && \
    install -D -m 0644 /tmp/RPM-GPG-KEY-gh-cli /etc/pki/rpm-gpg/RPM-GPG-KEY-gh-cli && \
    rm -f /tmp/RPM-GPG-KEY-charm /tmp/RPM-GPG-KEY-gh-cli /tmp/keys.sha256 && \
    rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-charm /etc/pki/rpm-gpg/RPM-GPG-KEY-gh-cli && \
    printf '[charm]\nname=Charm\nbaseurl=https://repo.charm.sh/yum/\nenabled=1\ngpgcheck=1\ngpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-charm\n' > /etc/yum.repos.d/charm.repo && \
    printf '[gh-cli]\nname=packages for the GitHub CLI\nbaseurl=https://cli.github.com/packages/rpm\nenabled=1\ngpgcheck=1\ngpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-gh-cli\n' > /etc/yum.repos.d/gh-cli.repo && \
    dnf5 -y install git rpm-ostree gh gum python3 && \
    dnf5 clean all

# cosign: sigstore publishes an x86_64 RPM per release but not through a
# yum repo, so download the release asset directly and verify it against
# its published sha256 before installing — no unverified curl|install.
# Bump both the URL and the checksum together when updating this pin.
#
# The checksum goes through a file rather than `echo ... | sha256sum -c -`.
# Under the default /bin/sh a pipeline reports only its LAST command's status,
# so anything added ahead of sha256sum in that pipe would fail silently --
# which is what hadolint DL4006 warns about. The usual answer, a SHELL
# instruction setting pipefail, does not work here: buildah ignores SHELL under
# the OCI image format this image is built with, so it would satisfy the linter
# without changing the behaviour. No pipe, no ambiguity, nothing to suppress.
RUN curl -fsSL -o /tmp/cosign.rpm \
      https://github.com/sigstore/cosign/releases/download/v3.1.2/cosign-3.1.2-1.x86_64.rpm && \
    echo "72382d1ef1cc824e1c10acfbe7f76af76fb294a10b0d16f31b978350ec4bc3e9  /tmp/cosign.rpm" > /tmp/cosign.sha256 && \
    sha256sum -c /tmp/cosign.sha256 && \
    dnf5 -y install /tmp/cosign.rpm && \
    rm -f /tmp/cosign.rpm /tmp/cosign.sha256 && \
    dnf5 clean all

# The script resolves its bundled template snapshots relative to its own
# path (TEMPLATE_SNAPSHOT_DIR in atomic_image_builder.py), so it must keep
# template_snapshots/ as a sibling directory wherever it's installed.
COPY atomic_image_builder.py /opt/atomic-image-builder/atomic_image_builder.py
COPY template_snapshots/ /opt/atomic-image-builder/template_snapshots/
RUN chmod +x /opt/atomic-image-builder/atomic_image_builder.py && \
    ln -s /opt/atomic-image-builder/atomic_image_builder.py /usr/local/bin/atomic-image-builder && \
    ln -s /usr/local/bin/atomic-image-builder /usr/local/bin/aib-tool

COPY container/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# This image runs as root (the base image default; no USER switch here).
# Under rootless podman — the intended way to run this image — the
# container's root maps to the invoking host user via user namespaces, so
# files the tool creates (e.g. git clones) end up owned by the host user
# rather than a privileged root outside the container. Do not add a non-root
# USER here without re-verifying that rootless-podman UID mapping still holds
# end-to-end.

# Make the tool's "Test build locally (podman)" option degrade with a clean
# message instead of attempting an unsupported nested build (see the note by
# the package install above, and maintenance_notes.txt).
ENV AIB_DISABLE_LOCAL_BUILD=1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
