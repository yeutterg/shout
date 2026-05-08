# Homebrew formula for Shout.
#
# Personal tap usage:
#   brew tap yeutterg/shout https://github.com/yeutterg/shout
#   brew install yeutterg/shout/shout
#
# Local development (no tap required):
#   brew install --build-from-source --HEAD ./Formula/shout.rb
#
# This formula deviates from the standard Language::Python::Virtualenv
# pattern by installing PyPI deps directly with pip rather than as
# pinned `resource` blocks. The reason is parakeet-mlx pulls in `mlx`
# and `mlx-metal`, which are wheel-only (Metal/native code, no sdist on
# PyPI) and therefore incompatible with Homebrew's resource-build flow.
# In a personal tap this trade-off is fine; for homebrew/core we would
# need to pre-vendor wheels.
class Shout < Formula
  desc "Local-first push-to-talk dictation for macOS, powered by Parakeet via MLX"
  homepage "https://github.com/yeutterg/shout"
  url "https://github.com/yeutterg/shout/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"
  head "https://github.com/yeutterg/shout.git", branch: "master"

  # MLX is Apple-Silicon only. Failing fast at install is friendlier than
  # a `Stream(gpu, 0)` runtime crash on Intel Macs.
  depends_on arch: :arm64
  # Tkinter is a separate Homebrew formula for python@3.12; the daemon
  # uses it for the floating overlay.
  depends_on "python-tk@3.12"
  depends_on "python@3.12"

  def install
    venv_root = libexec/"venv"
    python = Formula["python@3.12"].opt_bin/"python3.12"
    system python, "-m", "venv", venv_root

    pip = venv_root/"bin/pip"
    system pip, "install", "--upgrade", "pip", "wheel"

    # Runtime deps. Versions match pyproject.toml.
    system pip, "install",
           "parakeet-mlx>=0.3",
           "sounddevice>=0.5",
           "numpy>=2.0",
           "pyobjc-framework-Quartz>=10.3"

    # Install Shout itself last so its console_scripts entrypoint
    # ('shout = shout.cli:main') is registered against this venv.
    system pip, "install", buildpath

    bin.install_symlink venv_root/"bin/shout"
    # The Hammerspoon Lua, Karabiner JSON, and launchd plist are
    # bundled into the Python wheel's `_resources` package data
    # (see [tool.hatch.build.targets.wheel.force-include] in
    # pyproject.toml), so `shout setup` can find them via importlib.
  end

  service do
    run [opt_bin/"shout", "daemon"]
    keep_alive successful_exit: false
    log_path "/tmp/shout.out.log"
    error_log_path "/tmp/shout.err.log"
    process_type :interactive
  end

  def caveats
    <<~EOS
      Shout needs two GUI apps for the push-to-talk hotkey path. They are
      not formula dependencies because casks cannot be required from
      formulas, so install them manually:

        brew install --cask karabiner-elements hammerspoon

      Then wire up the configs and grant the daemon permissions:

        shout setup
        # System Settings → Privacy & Security:
        #   - Microphone        → enable for Python (or for the daemon)
        #   - Accessibility     → enable (for typing at cursor)
        #   - Input Monitoring  → enable for Hammerspoon
        shout doctor

      Start the daemon:

        brew services start shout
    EOS
  end

  test do
    assert_match "usage: shout", shell_output("#{bin}/shout --help")
  end
end
