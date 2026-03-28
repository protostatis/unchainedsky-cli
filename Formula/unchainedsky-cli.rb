class UnchainedskyCli < Formula
  include Language::Python::Virtualenv

  desc "Browser automation CLI over local Chrome CDP"
  homepage "https://github.com/protostatis/unchainedsky-cli"
  url "https://github.com/protostatis/unchainedsky-cli/archive/refs/tags/v0.1.1.tar.gz"
  head "https://github.com/protostatis/unchainedsky-cli.git", branch: "main"
  license "MIT"

  depends_on "python@3.13"

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/source/w/websockets/websockets-16.0.tar.gz"
    sha256 "a8429ae538944e1e5ebf03da3c0951c7e93b17e50b703a405b38c0e688c18330"
  end

  def install
    virtualenv_install_with_resources

    # DDM and Intel are included as Python modules (entry points).
    # The CLI dispatcher finds them via unchained-ddm / unchained-intel
    # console_scripts or falls back to the built-in Python engine.
  end

  test do
    system bin/"unchained", "--help"
    system bin/"unchained-ddm", "--help"
    system bin/"unchained-intel", "--help"
  end
end
