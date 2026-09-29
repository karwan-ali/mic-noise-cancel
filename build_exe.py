"""Build a standalone MicNoiseCancel.exe. Run this on Windows:

    python build_exe.py

It sets up the environment, installs PyInstaller and produces dist\\MicNoiseCancel.exe,
which runs on machines with no Python at all.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
BUILD = os.path.join(HERE, "build")


def venv_python():
    if os.name == "nt":
        return os.path.join(VENV, "Scripts", "python.exe")
    return os.path.join(VENV, "bin", "python")


def run(command, what):
    print("\n==> %s" % what)
    result = subprocess.call(command, cwd=HERE)
    if result != 0:
        print("\nERROR: %s failed (exit code %d)." % (what, result))
        sys.exit(1)


def main():
    if os.name != "nt":
        print("Warning: a Windows .exe can only be built on Windows.\n"
              "Run this script on the target machine instead.")

    python = venv_python()
    if not os.path.isfile(python):
        run([sys.executable, "-m", "venv", VENV], "Creating the Python environment")
        run([python, "-m", "pip", "install", "--upgrade", "pip"], "Updating pip")
        run([python, "-m", "pip", "install", "-r",
             os.path.join(HERE, "requirements.txt")], "Installing dependencies")

    run([python, "-m", "pip", "install", "--upgrade", "pyinstaller"],
        "Installing PyInstaller")

    os.makedirs(BUILD, exist_ok=True)
    icon = os.path.join(BUILD, "app.ico")
    run([python, "-c", "from micnoise.icon import save_ico; save_ico(%r)" % icon],
        "Drawing the program icon")

    # PortAudio's DLL and pystray's backend are loaded at runtime, so
    # PyInstaller cannot discover them by following imports alone.
    command = [
        python, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed",
        "--name", "MicNoiseCancel",
        "--collect-all", "sounddevice",
        "--collect-all", "pystray",
        "--hidden-import", "PIL._tkinter_finder",
    ]
    if os.path.isfile(icon):
        command += ["--icon", icon]
    command.append(os.path.join(HERE, "run.py"))
    run(command, "Building the executable")

    exe = os.path.join(HERE, "dist", "MicNoiseCancel.exe")
    print("\nDone: %s" % (exe if os.path.isfile(exe) else os.path.join(HERE, "dist")))
    print("Want RNNoise? Put rnnoise.dll in the same folder as the executable.")


if __name__ == "__main__":
    main()
