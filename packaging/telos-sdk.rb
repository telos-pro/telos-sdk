# telos-sdk Homebrew formula 模板
#
# 这是一份模板。真正发布到 Homebrew 之前还需要：
#
#   1. 把包发布到 PyPI：  python -m build && twine upload dist/*
#   2. 取 sdist 的 sha256： shasum -a 256 dist/telos_sdk-<ver>.tar.gz
#   3. 填好下面 url / sha256；用 `brew update-python-resources Formula/telos-sdk.rb`
#      自动生成 anthropic / openai / aiohttp 等依赖的 resource 块。
#   4. 建一个 tap 仓库（如 telos-pro/homebrew-telos），把本文件放进 Formula/。
#   5. 用户即可：  brew install telos-pro/telos/telos-sdk
#
# 详见同目录 README.md。

class TelosSdk < Formula
  include Language::Python::Virtualenv

  desc "Cache-friendly prompt protocol — gateway + multi-harness manager"
  homepage "https://github.com/telos-pro/telos-sdk"
  url "https://files.pythonhosted.org/packages/source/t/telos-sdk/telos_sdk-0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "Apache-2.0"

  depends_on "python@3.12"

  # `brew update-python-resources` 会在这里自动写入 anthropic / openai /
  # aiohttp 及其传递依赖的 resource 块。

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "usage: telos", shell_output("#{bin}/telos --help")
  end
end
