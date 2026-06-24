class Looptrip < Formula
  include Language::Python::Virtualenv

  desc "Deterministic detector of multi-agent coordination pathologies"
  homepage "https://github.com/ek33450505/looptrip"
  # Points at the published PyPI sdist (not a GitHub archive); sha256 verified
  # against files.pythonhosted.org.
  url "https://files.pythonhosted.org/packages/cc/b5/653ac452da81f71c9f4fb9443cee042c9781fb7dd571d42ce612af06233a/looptrip-0.1.2.tar.gz"
  sha256 "a3174d240eea6784628fb130f6bb65ac3615dae85eeb04e4eea32ecc2b521310"
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
