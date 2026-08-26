class AtomicImageBuilder < Formula
  include Language::Python::Shebang

  desc "Beginner-focused terminal tool for GitHub-backed bootc image repos"
  homepage "https://github.com/Danathar/atomic-image-builder"
  url "https://github.com/Danathar/atomic-image-builder/archive/refs/tags/v0.9.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "GPL-3.0-only"

  # The tool shells out to all four of these and refuses to start without them,
  # so they are real dependencies rather than suggestions. None of them are in
  # Fedora's own repositories, which is the reason the container image exists;
  # here Homebrew supplies them instead. All four have x86_64_linux bottles.
  depends_on "cosign"
  depends_on "gh"
  depends_on "git"
  depends_on "gum"
  depends_on "python@3.13"

  def install
    # TEMPLATE_SNAPSHOT_DIR resolves as Path(__file__).resolve().parent /
    # "template_snapshots", so the snapshots have to stay a sibling of the
    # script. Installing both into libexec keeps that true; .resolve() follows
    # the bin symlink back here, so the snapshots are still found.
    libexec.install "atomic_image_builder.py", "template_snapshots"
    rewrite_shebang detected_python_shebang, libexec/"atomic_image_builder.py"
    chmod 0755, libexec/"atomic_image_builder.py"
    bin.install_symlink libexec/"atomic_image_builder.py" => "atomic-image-builder"
  end

  def caveats
    <<~EOS
      atomic-image-builder also needs dnf5 and rpm-ostree. Those come from the
      host, not from Homebrew: it reads the host's package metadata and talks to
      the host's rpm-ostreed. The tool targets Fedora Atomic and Universal Blue
      desktops, where both are already present.

      Log in to GitHub before first use:
        gh auth login
    EOS
  end

  test do
    assert_match "atomic-image-builder #{version}", shell_output("#{bin}/atomic-image-builder --version")
    assert_match "Usage: atomic-image-builder", shell_output("#{bin}/atomic-image-builder --help")
  end
end
