# Homebrew formula for gifhole, with `brew services` support so it can run in
# the background and start at login on macOS.
#
# NOT YET INSTALLABLE AS-IS. Two things must be filled in first:
#
#   1. A release to point at. Push the repo, tag a release, then set `url` to
#      the tarball and `sha256` to its checksum:
#          curl -sL <tarball-url> | shasum -a 256
#      (Or install straight from git with `--HEAD`, which uses the `head` line.)
#
#   2. Pinned Python dependencies. Homebrew builds in a network sandbox, so
#      every dependency must be declared as a `resource`. Generate them with:
#          brew update-python-resources packaging/homebrew/gifhole.rb
#      and paste the output where marked below.
#
# Then, from a tap (e.g. `brew tap <you>/tap && brew install <you>/tap/gifhole`):
#
#     brew services start gifhole     # run now, and at every login
#     brew services stop  gifhole
#     brew services info  gifhole
#
# ffmpeg is deliberately NOT a dependency: gifhole works without it and simply
# skips video sources. `brew install ffmpeg` to enable video-to-GIF conversion
# (Giphy/Tenor/Reddit/Imgur mostly serve MP4).

class Gifhole < Formula
  include Language::Python::Virtualenv

  desc "Local GIF library with click-to-copy, OCR search, and dot-com-era skins"
  homepage "https://github.com/kcm/gifhole"
  url "https://github.com/kcm/gifhole/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"
  head "https://github.com/kcm/gifhole.git", branch: "main"

  depends_on "python@3.13"

  # <<< brew update-python-resources output goes here >>>
  # resource "fastapi" do ... end
  # resource "uvicorn" do ... end
  # ... etc

  def install
    virtualenv_install_with_resources
  end

  service do
    run [opt_bin/"gifhole", "--host", "127.0.0.1", "--port", "8777", "--no-open"]
    keep_alive true
    run_type :immediate
    working_dir var
    log_path var/"log/gifhole.log"
    error_log_path var/"log/gifhole.log"
  end

  def caveats
    <<~EOS
      gifhole serves your library at http://127.0.0.1:8777/ and stores GIFs in
      ~/.gifhole/gifs (override with GIFHOLE_ROOT).

      Start it in the background, and at login:
        brew services start gifhole

      For video sources (most "GIFs" on Giphy/Tenor/Reddit are MP4):
        brew install ffmpeg

      Burned-in text search uses the macOS Vision framework and needs no key.
      Claude descriptions are opt-in and need ANTHROPIC_API_KEY.
    EOS
  end

  test do
    assert_match "local GIF library", shell_output("#{bin}/gifhole --help")
  end
end
