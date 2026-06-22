class Looptrip < Formula
  include Language::Python::Virtualenv

  desc "Deterministic detector of multi-agent coordination pathologies"
  homepage "https://github.com/ek33450505/looptrip"
  # Points at the published PyPI sdist (not a GitHub archive) so the formula installs
  # while the GitHub repo is still private; sha256 verified against files.pythonhosted.org.
  url "https://files.pythonhosted.org/packages/1f/70/94795ce5be2634a1d2cdbc5cfade5f983707dbdf09bfded6fd79f348078d/looptrip-0.1.0.tar.gz"
  sha256 "46269bd4b869705d9d8ed84ca18a08fa5567d0bbc5ba3a11cf052b531f581f37"
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
