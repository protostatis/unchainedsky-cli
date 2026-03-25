class UnchainedskyCli < Formula
  desc "Browser automation CLI over local Chrome CDP"
  homepage "https://github.com/protostatis/unchainedsky-cli"
  url "https://github.com/protostatis/unchainedsky-cli/archive/refs/tags/v<VERSION>.tar.gz"
  sha256 "<SHA256>"
  license "MIT"

  depends_on "python@3.13"

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/source/w/websockets/websockets-<WEBSOCKETS_VERSION>.tar.gz"
    sha256 "<WEBSOCKETS_SHA256>"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    system bin/"unchained", "--help"
  end
end
