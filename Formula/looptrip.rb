class Looptrip < Formula
  include Language::Python::Virtualenv

  desc "Deterministic detector of multi-agent coordination pathologies"
  homepage "https://github.com/ek33450505/looptrip"
  # Points at the published PyPI sdist (not a GitHub archive); sha256 verified
  # against files.pythonhosted.org.
  url "https://files.pythonhosted.org/packages/83/11/7c2e4cd3189f1c18369840dc3fdaa3475efb9c4fbf4b70036c2361f3f9bb/looptrip-0.1.1.tar.gz"
  sha256 "bbf019a4ae2b27b74f92193d6d1e7ff73a4b964ba6623778924b5fffe4b6cf21"
  license "Apache-2.0"

  depends_on "python@3.13"

  # Core is stdlib-only (zero runtime dependencies) — no resource stanzas needed.
  # The optional [otel] extra is intentionally NOT bundled; the brew install ships the
  # stdlib-only detector + CLI. Install opentelemetry separately for the live SpanProcessor.
  def install
    virtualenv_install_with_resources
  end

  test do
    # Version surface.
    assert_match "looptrip #{version}", shell_output("#{bin}/looptrip --version")
    # The hermetic Phase-1 proof ships in the package (_data/*.json); its headline figure
    # is byte-stable, so it doubles as an end-to-end install smoke test.
    assert_match "792.96", shell_output("#{bin}/looptrip proof")
  end
end
