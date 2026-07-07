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
# the host), gh, and gum. gh and gum come from their own official repos;
# dnf5-plugins provides the `config-manager` subcommand used to add gh's.
#
# Note: rpm-ostree is a hard startup requirement of the tool, and it pulls in
# bootc, which hard-requires podman. So podman ends up in this image even
# though we do not install it directly. Because podman is present, the tool
# would otherwise *attempt* a nested "Test build locally" build (unsupported
# here, and it fails) instead of degrading. We set AIB_DISABLE_LOCAL_BUILD
# below so that option shows a clean "not available" message instead. Local
# test builds remain a clone-and-run feature — see the README limitations.
RUN dnf5 -y install dnf5-plugins curl && \
    dnf5 config-manager addrepo --from-repofile=https://cli.github.com/packages/rpm/gh-cli.repo && \
    printf '[charm]\nname=Charm\nbaseurl=https://repo.charm.sh/yum/\nenabled=1\ngpgcheck=1\ngpgkey=https://repo.charm.sh/yum/gpg.key\n' > /etc/yum.repos.d/charm.repo && \
    rpm --import https://repo.charm.sh/yum/gpg.key && \
    dnf5 -y install git rpm-ostree gh gum python3 && \
    dnf5 clean all

# cosign: sigstore publishes an x86_64 RPM per release but not through a
# yum repo, so download the release asset directly and verify it against
# its published sha256 before installing — no unverified curl|install.
# Bump both the URL and the checksum together when updating this pin.
RUN curl -fsSL -o /tmp/cosign.rpm \
      https://github.com/sigstore/cosign/releases/download/v3.1.1/cosign-3.1.1-1.x86_64.rpm && \
    echo "daa90177c32a62550676ba1cf6be153291d601e53fa0e46a852fc5af020e5674  /tmp/cosign.rpm" | sha256sum -c - && \
    dnf5 -y install /tmp/cosign.rpm && \
    rm -f /tmp/cosign.rpm && \
    dnf5 clean all

# The script resolves its bundled template snapshots relative to its own
# path (TEMPLATE_SNAPSHOT_DIR in atomic_image_builder.py), so it must keep
# template_snapshots/ as a sibling directory wherever it's installed.
COPY atomic_image_builder.py /opt/atomic-image-builder/atomic_image_builder.py
COPY template_snapshots/ /opt/atomic-image-builder/template_snapshots/
RUN chmod +x /opt/atomic-image-builder/atomic_image_builder.py && \
    ln -s /opt/atomic-image-builder/atomic_image_builder.py /usr/local/bin/atomic-image-builder

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
