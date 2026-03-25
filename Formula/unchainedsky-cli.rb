class UnchainedskyCli < Formula
  include Language::Python::Virtualenv

  desc "Browser automation CLI over local Chrome CDP"
  homepage "https://github.com/protostatis/unchainedsky-cli"
  head "https://github.com/protostatis/unchainedsky-cli.git", branch: "main"
  license "MIT"

  depends_on "python@3.13"

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/source/w/websockets/websockets-12.0.tar.gz"
    sha256 "81df9cbcbb6c260de1e007e58c011bfebe2dafc8435107b0537f393dd38c8b1b"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    system bin/"unchained", "--help"
  end
end
