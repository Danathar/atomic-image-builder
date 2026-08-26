class AtomicImageBuilder < Formula
  include Language::Python::Shebang

  desc "Beginner-focused terminal tool for GitHub-backed bootc image repos"
  homepage "https://github.com/Danathar/atomic-image-builder"
  url "https://github.com/Danathar/atomic-image-builder/archive/refs/tags/v0.9.1.tar.gz"
  sha256 "1a45408f4ff9e5c62780d68610991cbec0cdf9c7eb14a0d33c40f3ade4899981"
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
    # The command is `aib-tool`, not `aib`: contrib/aib already installs a host
    # wrapper called `aib` into ~/.local/bin, which normally precedes Homebrew's
    # bin on PATH, so sharing the name would silently shadow one with the other.
    bin.install_symlink libexec/"atomic_image_builder.py" => "aib-tool"
  end

  def caveats
    <<~EOS
      atomic-image-builder also needs dnf5 and rpm-ostree. Those come from the
      host, not from Homebrew: it reads the host's package metadata and talks to
      the host's rpm-ostreed. The tool targets Fedora Atomic and Universal Blue
      desktops, where both are already present.

      The installed command is `aib-tool`. If you also use the container
      wrapper from contrib/aib, that one stays `aib`; the names are kept
      distinct so neither shadows the other on PATH.

      Log in to GitHub before first use:
        gh auth login
    EOS
  end

  test do
    assert_match "aib-tool #{version}", shell_output("#{bin}/aib-tool --version")
    assert_match "Usage: aib-tool", shell_output("#{bin}/aib-tool --help")
  end
end
