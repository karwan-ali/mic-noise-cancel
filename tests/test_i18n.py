"""The translation table, and the code that uses it.

Most translation bugs are not wrong words but missing ones: a string added in
one language only, a placeholder renamed in one half, a label someone typed
straight into the code. Each of those is checked mechanically here, against
both the table and the source that reads from it.
"""
import glob
import os
import re
import string
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="mnc-i18n-")

from micnoise import config, i18n  # noqa: E402

fails = []

SWEDISH_ONLY = set("åäöÅÄÖ„“")

# Captions that are genuinely the same word in both languages.
IDENTICAL_OK = {"settings.meter_in"}


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def placeholders(text):
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


SOURCES = sorted(glob.glob(os.path.join(ROOT, "micnoise", "*.py"))) + [
    os.path.join(ROOT, "MicNoiseCancel.pyw"),
    os.path.join(ROOT, "build_exe.py"),
    os.path.join(ROOT, "run.py"),
]


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


print("=== The table is complete ===")
keys = i18n.keys()
print("  %d strings, %d languages" % (len(keys), len(i18n.SUPPORTED)))
missing = [(k, lang) for k in keys for lang in i18n.SUPPORTED
           if not i18n.entry(k).get(lang, "").strip()]
check("every string exists in every language", not missing, str(missing[:5]))
extra = [k for k in keys if set(i18n.entry(k)) - set(i18n.SUPPORTED)]
check("no string has a language we do not ship", not extra, str(extra[:5]))

mismatched = [k for k in keys
              if len({frozenset(placeholders(i18n.entry(k)[lang]))
                      for lang in i18n.SUPPORTED}) != 1]
check("placeholders match across languages", not mismatched, str(mismatched[:5]))

untranslated = [k for k in keys if SWEDISH_ONLY & set(i18n.entry(k)["en"])]
check("no Swedish characters in the English text", not untranslated, str(untranslated[:5]))

same = [k for k in keys
        if i18n.entry(k)["sv"] == i18n.entry(k)["en"] and k not in IDENTICAL_OK]
check("English is not a copy of the Swedish", not same, str(same[:5]))

check("every language has a display name", set(i18n.NAMES) == set(i18n.SUPPORTED))
check("the language label reads in both languages",
      "Språk" in i18n.LANGUAGE_LABEL and "Language" in i18n.LANGUAGE_LABEL)
check("the fallback is a shipped language", i18n.FALLBACK in i18n.SUPPORTED)

print("\n=== The code and the table agree ===")
used = set()
for path in SOURCES:
    text = read(path)
    # Engine errors carry their key and are rendered when shown.
    used |= set(re.findall(r'(?:i18n\.t|\btr|EngineError)\(\s*"([a-z][a-z0-9_.]*)"', text))
    used |= {"help." + k for k in re.findall(r'_help\(\s*"([a-z_]+)"', text)}
    used |= {"help." + k for k in re.findall(r'self\._slider\([^,]+,\s*\d+,\s*"([a-z_]+)"', text)}
# "help." + key and friends are prefixes, not keys.
used = {k for k in used if not k.endswith(".")}
# Keys assembled at run time, which no regex can see.
used |= {"help.preset_" + name for name in config.PRESET_NAMES}
used |= {"preset." + name for name in config.PRESET_NAMES}
used |= {"settings.rnnoise_kind.native", "settings.rnnoise_kind.ladspa"}

unknown = sorted(k for k in used if not i18n.has(k))
check("every key the code asks for exists", not unknown, str(unknown[:5]))
dead = sorted(set(keys) - used)
check("every string in the table is used somewhere", not dead, str(dead[:5]))

hardcoded = []
for path in SOURCES:
    if path.endswith("i18n.py"):
        continue
    for number, line in enumerate(read(path).splitlines(), 1):
        if SWEDISH_ONLY & set(line):
            hardcoded.append("%s:%d" % (os.path.basename(path), number))
check("no Swedish text is hard-coded outside the table", not hardcoded, str(hardcoded[:5]))

print("\n=== Lookup ===")
i18n.set_language("sv")
check("Swedish when asked", i18n.t("tray.quit") == "Avsluta")
i18n.set_language("en")
check("English when asked", i18n.t("tray.quit") == "Quit")
check("placeholders are filled",
      i18n.t("app.ready", device="CABLE Output") ==
      'Ready. Choose "CABLE Output" as the microphone in Teams, Zoom or Discord.')
check("values containing braces are left alone",
      "{weird}" in i18n.t("launcher.install_failed", output="pip said {weird}"))
check("a missing key shows the key rather than crashing",
      i18n.t("no.such.key") == "no.such.key")
check("a missing placeholder degrades to the raw text",
      "{device}" in i18n.t("app.ready"))
check("an unknown language falls back to the system one",
      i18n.set_language("xx") == i18n.detect())
check("None means follow the system", i18n.set_language(None) == i18n.detect())

print("\n=== Detecting the system language ===")
saved = {k: os.environ.get(k) for k in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG")}
try:
    for k in saved:
        os.environ.pop(k, None)
    if os.name != "nt":
        os.environ["LANG"] = "sv_SE.UTF-8"
        check("a Swedish locale gives Swedish", i18n.detect() == "sv")
        os.environ["LANG"] = "en_GB.UTF-8"
        check("an English locale gives English", i18n.detect() == "en")
        os.environ["LANG"] = "de_DE.UTF-8"
        check("anything else gives English", i18n.detect() == "en")
        os.environ["LC_ALL"] = "sv_FI.UTF-8"
        check("LC_ALL wins over LANG", i18n.detect() == "sv")
    check("detection always returns a shipped language", i18n.detect() in i18n.SUPPORTED)
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("\n=== Config ===")
check("a new install follows the system language", config.DEFAULTS["language"] is None)
cfg = dict(config.DEFAULTS, language="sv")
config.save(cfg)
check("the choice is saved and read back", config.load()["language"] == "sv")
i18n.set_language("sv")
check("preset names are translated", config.preset_label("light") == "Lätt")
i18n.set_language("en")
check("in both languages", config.preset_label("light") == "Light")

print("\n" + "=" * 62)
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("All checks passed.")
