"""The double-click launcher and the shortcut helper.

The launcher runs before anything is installed, so it may only use the standard
library - that is the first thing checked here. The bootstrap path is exercised
against a throwaway directory so a real venv is built and handed over to,
without touching the checkout's own .venv.
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# The launcher reads the saved language; keep it away from the real config.
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="mnc-launch-cfg-")

LAUNCHER = os.path.join(ROOT, "MicNoiseCancel.pyw")

fails = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not ok:
        fails.append(name)


print("=== The launcher only needs the standard library ===")
source = open(LAUNCHER, encoding="utf-8").read()
tree = ast.parse(source)

top_level = []
for node in tree.body:  # module scope only; deferred imports are fine
    if isinstance(node, ast.Import):
        top_level += [a.name.split(".")[0] for a in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module:
        top_level.append(node.module.split(".")[0])

stdlib_only = {"os", "subprocess", "sys", "threading", "time"}
check("no third-party imports at module scope", set(top_level) <= stdlib_only,
      str(sorted(set(top_level) - stdlib_only)))
check("micnoise is imported lazily, after the venv exists",
      "from micnoise.app import main" in source and "micnoise" not in top_level)

# Importing it must not start anything.
sys.path.insert(0, ROOT)
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402

# .pyw is not a recognised source suffix, so name the loader explicitly.
# Windows runs the file as a script rather than importing it, so this is a
# concern for the test only.
spec = importlib.util.spec_from_file_location(
    "mnc_launcher", LAUNCHER,
    loader=importlib.machinery.SourceFileLoader("mnc_launcher", LAUNCHER),
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
check("importing the launcher has no side effects", hasattr(launcher, "main"))

print("\n=== Dependency detection ===")
absent = launcher.missing_modules()
print("  missing here: %s" % (absent or "none"))
check("detection returns a list", isinstance(absent, list))
saved = launcher.REQUIRED_MODULES
launcher.REQUIRED_MODULES = ("os", "sys", "json")
check("modules that exist are not reported missing", launcher.missing_modules() == [])
launcher.REQUIRED_MODULES = ("os", "definitely_not_a_module_xyz")
check("modules that do not exist are reported",
      launcher.missing_modules() == ["definitely_not_a_module_xyz"])
launcher.REQUIRED_MODULES = saved

print("\n=== Interpreter paths ===")
path = launcher.venv_python()
check("venv python sits under .venv", ".venv" in path)
check("windowless variant differs on Windows or matches elsewhere",
      (launcher.venv_python(True) != path) if os.name == "nt"
      else (launcher.venv_python(True) == path))

print("\n=== Bootstrap builds a working environment ===")
# Point the launcher at a scratch directory and let it do the real thing.
tmp = tempfile.mkdtemp(prefix="mnc-boot-")
try:
    reqs = os.path.join(tmp, "requirements.txt")
    with open(reqs, "w") as fh:
        fh.write("packaging\n")  # tiny, pure-python, no compilation

    launcher.HERE = tmp
    launcher.VENV = os.path.join(tmp, ".venv")
    launcher.REQUIREMENTS = reqs
    launcher.REQUIRED_MODULES = ("packaging",)

    messages = []
    ok, error = launcher._install_everything(messages.append)
    print("  steps reported: %d" % len(messages))
    check("bootstrap succeeds", ok, error[:200])
    from micnoise import i18n  # the same module instance the launcher translated with

    check("it says what it is doing, in the UI language",
          messages[0] == i18n.t("launcher.creating_env")
          and i18n.t("launcher.installing") in messages, str(messages))
    check("the venv interpreter exists", os.path.isfile(launcher.venv_python()))

    probe = subprocess.run(
        [launcher.venv_python(), "-c", "import packaging; print(packaging.__name__)"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    check("the requirement is importable in the new venv",
          probe.returncode == 0 and b"packaging" in probe.stdout,
          (probe.stdout or b"").decode()[:200])

    print("\n=== Handover to the new interpreter ===")
    # A stand-in launcher that just records how it was invoked.
    stand_in = os.path.join(tmp, "MicNoiseCancel.pyw")
    marker = os.path.join(tmp, "started.txt")
    with open(stand_in, "w") as fh:
        fh.write(
            "import os, sys\n"
            "open(%r, 'w').write(sys.executable + '\\n' + repr(sys.argv[1:])"
            " + '\\n' + os.environ.get('MICNOISE_BOOTSTRAPPED', ''))\n" % marker)
    launcher.__file__ = stand_in
    code = launcher.relaunch(["--startup"])
    check("relaunch reports success", code == 0)

    for _ in range(100):
        if os.path.isfile(marker):
            break
        import time

        time.sleep(0.1)
    check("the new process actually started", os.path.isfile(marker))
    if os.path.isfile(marker):
        exe, argv, flag = open(marker).read().split("\n")
        check("it runs the venv interpreter", ".venv" in exe, exe)
        check("arguments are passed through", argv == "['--startup']", argv)
        check("the bootstrap flag prevents an install loop", flag == "1", repr(flag))

    print("\n=== A second bootstrap does not rebuild ===")
    before = os.path.getmtime(launcher.venv_python())
    messages2 = []
    ok2, _ = launcher._install_everything(messages2.append)
    check("re-running is still successful", ok2)
    check("the existing venv is reused",
          os.path.getmtime(launcher.venv_python()) == before)
    check("it does not claim to create a new one",
          i18n.t("launcher.creating_env") not in messages2, str(messages2))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n=== Failure is reported, not swallowed ===")
tmp2 = tempfile.mkdtemp(prefix="mnc-boot-fail-")
try:
    reqs = os.path.join(tmp2, "requirements.txt")
    with open(reqs, "w") as fh:
        fh.write("this-package-does-not-exist-anywhere-9x8y7z\n")
    launcher.HERE = tmp2
    launcher.VENV = os.path.join(tmp2, ".venv")
    launcher.REQUIREMENTS = reqs
    ok, error = launcher._install_everything(lambda _m: None)
    check("a bad requirement fails the bootstrap", ok is False)
    check("the error explains itself",
          error.startswith(i18n.t("launcher.install_failed", output="").split("\n")[0]),
          error[:160])
    check("and includes what pip said", "this-package-does-not-exist" in error)
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

print("\n=== Shortcuts ===")
from micnoise import shortcut  # noqa: E402

check("supported() is honest about the platform", shortcut.supported() == (os.name == "nt"))
check("locations() is empty off Windows",
      bool(shortcut.locations()) == (os.name == "nt"))
check("exists() is safe to call anywhere", shortcut.exists("desktop") in (True, False))
check("create() declines cleanly off Windows",
      shortcut.create("desktop") is False or os.name == "nt")
check("remove() is idempotent", shortcut.remove("desktop") is True)
check("PowerShell quoting escapes single quotes",
      shortcut._ps_quote("it's") == "'it''s'", shortcut._ps_quote("it's"))
check("PowerShell quoting wraps plain paths",
      shortcut._ps_quote(r"C:\Program Files\x") == r"'C:\Program Files\x'")

target, arguments = shortcut._launch_target()
print("  shortcut would run: %s %s" % (os.path.basename(target), arguments))
check("target is an interpreter or the frozen exe", os.path.isfile(target), target)
check("arguments point at a launcher we ship",
      arguments == "" or arguments.strip('"').endswith((".pyw", ".py")), arguments)

print("\n=== Build script ===")
build = os.path.join(ROOT, "build_exe.py")
check("build script exists", os.path.isfile(build))
ast.parse(open(build, encoding="utf-8").read())
check("build script parses", True)
check("no .bat files are left in the project",
      not [f for f in os.listdir(ROOT) if f.lower().endswith(".bat")],
      str([f for f in os.listdir(ROOT) if f.lower().endswith(".bat")]))

print("\n" + "=" * 62)
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("All checks passed.")
