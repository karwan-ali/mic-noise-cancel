"""User interface text in Swedish and English.

Every string the user can see goes through t(). Log messages do not - they
are for whoever debugs the app and stay in English.

Translations sit side by side per key rather than in one file per language, so
a new string cannot be added in one language and forgotten in the other
without the test suite noticing. This module is standard library only: the
double-click launcher uses it before any dependency is installed.
"""

import locale
import logging
import os

log = logging.getLogger(__name__)

SUPPORTED = ("sv", "en")
NAMES = {"sv": "Svenska", "en": "English"}
FALLBACK = "en"

# Deliberately bilingual and outside the table: someone stuck in a language
# they cannot read must still be able to find the way out.
LANGUAGE_LABEL = "Språk / Language"

_current = FALLBACK
_warned = set()


def detect():
    """The language the operating system presents its own interface in."""
    if os.name == "nt":
        try:
            import ctypes

            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "sv" if (langid & 0x3FF) == 0x1D else FALLBACK  # LANG_SWEDISH
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        value = os.environ.get(var, "").strip().lower()
        if value and value not in ("c", "posix"):
            return "sv" if value.startswith("sv") else FALLBACK
    try:
        name = (locale.getlocale()[0] or "").lower()
        if name.startswith(("sv", "swedish")):
            return "sv"
    except Exception:
        pass
    return FALLBACK


def set_language(code):
    """Switch language. Unknown or empty codes mean "follow the system"."""
    global _current
    _current = code if code in SUPPORTED else detect()
    return _current


def get_language():
    return _current


def has(key):
    return key in _STRINGS


def keys():
    return list(_STRINGS)


def entry(key):
    """Every translation of one key, for the tests."""
    return dict(_STRINGS[key])


def t(key, **params):
    """The text for key in the current language, with {placeholders} filled."""
    item = _STRINGS.get(key)
    if item is None:
        if key not in _warned:
            _warned.add(key)
            log.warning("Missing UI string %r", key)
        return key
    text = item.get(_current) or item.get(FALLBACK) or key
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            log.warning("Could not format UI string %r", key)
    return text


_STRINGS = {
    # ------------------------------------------------------------ status
    "status.active": {"sv": "Aktiv", "en": "Active"},
    "status.stopped": {"sv": "Stoppad", "en": "Stopped"},
    "status.bypassed": {"sv": "Förbikopplad", "en": "Bypassed"},
    "status.error": {"sv": "Fel", "en": "Error"},

    # ------------------------------------------------------------ presets
    "preset.light": {"sv": "Lätt", "en": "Light"},
    "preset.balanced": {"sv": "Balanserad", "en": "Balanced"},
    "preset.aggressive": {"sv": "Aggressiv (endast röst)", "en": "Aggressive (voice only)"},

    # ------------------------------------------------------------ shared actions
    "action.restart_audio": {"sv": "Starta om ljudet", "en": "Restart audio"},
    "action.close": {"sv": "Stäng", "en": "Close"},

    # ------------------------------------------------------------ app
    "app.already_running": {
        "sv": "{app} körs redan. Ikonen finns i systemfältet.",
        "en": "{app} is already running. Look for its icon in the system tray.",
    },
    "app.audio_library_failed": {
        "sv": "Ljudbiblioteket kunde inte laddas:\n\n{error}\n\n"
              "Kör: pip install -r requirements.txt",
        "en": "The audio library could not be loaded:\n\n{error}\n\n"
              "Run: pip install -r requirements.txt",
    },
    "app.start_failed": {
        "sv": "{app} kunde inte starta:\n\n{error}\n\nLogg: {log}",
        "en": "{app} could not start:\n\n{error}\n\nLog: {log}",
    },
    "app.start_failed_title": {
        "sv": "{app} - kunde inte starta",
        "en": "{app} - could not start",
    },
    "app.no_cable": {
        "sv": "Ingen virtuell ljudkabel hittades. Öppna inställningarna för att "
              "installera VB-CABLE, annars kan andra program inte använda det "
              "rensade ljudet.",
        "en": "No virtual audio cable was found. Open the settings to install "
              "VB-CABLE - without it, other programs cannot use the cleaned audio.",
    },
    "app.ready": {
        "sv": "Klart. Välj „{device}“ som mikrofon i Teams, Zoom eller Discord.",
        "en": 'Ready. Choose "{device}" as the microphone in Teams, Zoom or Discord.',
    },

    # ------------------------------------------------------------ engine
    "engine.no_input": {
        "sv": "Ingen mikrofon vald eller hittad.",
        "en": "No microphone selected or found.",
    },
    "engine.no_output": {
        "sv": "Ingen utgång vald. Installera VB-CABLE och välj „CABLE Input“.",
        "en": 'No output selected. Install VB-CABLE and choose "CABLE Input".',
    },
    "engine.open_input_failed": {
        "sv": "Kunde inte öppna mikrofonen „{device}“: {error}",
        "en": 'Could not open the microphone "{device}": {error}',
    },
    "engine.open_output_failed": {
        "sv": "Kunde inte öppna utgången „{device}“: {error}",
        "en": 'Could not open the output "{device}": {error}',
    },

    # ------------------------------------------------------------ tray
    "tray.enabled": {"sv": "Brusreducering på", "en": "Noise reduction on"},
    "tray.level": {"sv": "Nivå", "en": "Level"},
    "tray.microphone": {"sv": "Mikrofon", "en": "Microphone"},
    "tray.send_to": {"sv": "Skicka till", "en": "Send to"},
    "tray.monitor": {"sv": "Medhörning i hörlurar", "en": "Monitor in headphones"},
    "tray.settings": {"sv": "Inställningar...", "en": "Settings..."},
    "tray.quit": {"sv": "Avsluta", "en": "Quit"},
    "tray.no_devices": {"sv": "(inga enheter hittades)", "en": "(no devices found)"},
    "tray.more_in_settings": {"sv": "Fler i Inställningar...", "en": "More in Settings..."},
    "tray.headline.error": {"sv": "Fel - se Inställningar", "en": "Error - see Settings"},
    "tray.headline.no_cable": {
        "sv": "Aktiv (ingen virtuell kabel)",
        "en": "Active (no virtual cable)",
    },
    "tray.headline.active": {
        "sv": "Aktiv - rensar mikrofonen",
        "en": "Active - cleaning the microphone",
    },
    "tray.tip.error": {"sv": "Fel: {error}", "en": "Error: {error}"},
    "tray.tip.input": {"sv": "In:  {device}", "en": "In:   {device}"},
    "tray.tip.output": {"sv": "Ut:  {device}", "en": "Out: {device}"},

    # ------------------------------------------------------------ settings window
    "settings.title": {"sv": "{app} - inställningar", "en": "{app} - settings"},
    "settings.tab.general": {"sv": "Allmänt", "en": "General"},
    "settings.tab.advanced": {"sv": "Avancerat", "en": "Advanced"},

    "settings.devices": {"sv": "Ljudenheter", "en": "Audio devices"},
    "settings.microphone": {"sv": "Mikrofon", "en": "Microphone"},
    "settings.output": {"sv": "Skicka rensat ljud till", "en": "Send cleaned audio to"},
    "settings.rescan_devices": {"sv": "Sök enheter igen", "en": "Rescan devices"},
    "settings.download_cable": {"sv": "Ladda ner VB-CABLE", "en": "Download VB-CABLE"},
    "settings.none_found": {"sv": "(ingen hittad)", "en": "(none found)"},
    "settings.cable_ok": {
        "sv": "Klart. Välj „{device}“ som mikrofon i Teams, Zoom, Discord eller OBS.",
        "en": 'Ready. Choose "{device}" as the microphone in Teams, Zoom, Discord or OBS.',
    },
    "settings.cable_missing": {
        "sv": "Den valda utgången är inte en virtuell ljudkabel, så andra program "
              "kan inte använda det rensade ljudet som mikrofon. Installera "
              "VB-CABLE och välj „CABLE Input“ här.",
        "en": "The selected output is not a virtual audio cable, so other "
              "programs cannot use the cleaned audio as a microphone. Install "
              'VB-CABLE and choose "CABLE Input" here.',
    },

    "settings.noise_reduction": {"sv": "Brusreducering", "en": "Noise reduction"},
    "settings.hover_presets": {
        "sv": "Håll muspekaren över ett val för att se vad det gör.",
        "en": "Hover over an option to see what it does.",
    },

    "settings.levels": {"sv": "Nivåer", "en": "Levels"},
    "settings.meter_in": {"sv": "In", "en": "In"},
    "settings.meter_out": {"sv": "Ut", "en": "Out"},
    "settings.voice_detected": {
        "sv": "Röst upptäckt - grinden är öppen",
        "en": "Voice detected - the gate is open",
    },
    "settings.quiet": {
        "sv": "Tyst - bakgrundsljudet tystas",
        "en": "Quiet - background noise is being muted",
    },

    "settings.monitor_and_startup": {"sv": "Medhörning och start", "en": "Monitoring and startup"},
    "settings.monitor_enabled": {
        "sv": "Lyssna på det rensade ljudet i hörlurarna",
        "en": "Listen to the cleaned audio in headphones",
    },
    "settings.headphones": {"sv": "Hörlurar", "en": "Headphones"},
    "settings.monitor_volume": {"sv": "Medhörningsvolym", "en": "Monitor volume"},
    "settings.autostart": {
        "sv": "Starta automatiskt när Windows startar",
        "en": "Start automatically with Windows",
    },
    "settings.shortcut_create": {"sv": "Skapa genväg på skrivbordet", "en": "Create desktop shortcut"},
    "settings.shortcut_update": {"sv": "Uppdatera genvägen", "en": "Update the shortcut"},

    "settings.reduction": {"sv": "Reduktionsstyrka", "en": "Reduction strength"},
    "settings.highpass": {"sv": "Högpassfilter", "en": "High-pass filter"},
    "settings.input_gain": {"sv": "Ingångsförstärkning", "en": "Input gain"},
    "settings.output_gain": {"sv": "Utgångsförstärkning", "en": "Output gain"},
    "settings.gate": {"sv": "Brusgrind", "en": "Noise gate"},
    "settings.gate_enabled": {
        "sv": "Tysta helt mellan meningarna",
        "en": "Silence completely between sentences",
    },
    "settings.gate_floor": {"sv": "Grindens djup", "en": "Gate depth"},
    "settings.vad_threshold": {"sv": "Rösttröskel", "en": "Voice threshold"},
    "settings.hangover": {"sv": "Efterhäng", "en": "Hold time"},
    "settings.attack": {"sv": "Öppningstid", "en": "Attack"},
    "settings.release": {"sv": "Stängningstid", "en": "Release"},
    "settings.hover_sliders": {
        "sv": "Håll muspekaren över ett reglage för att se vad det gör och när "
              "det är värt att röra.",
        "en": "Hover over a slider to see what it does and when it is worth changing.",
    },

    "settings.other": {"sv": "Övrigt", "en": "Other"},
    "settings.use_rnnoise": {
        "sv": "Använd RNNoise (neuralt nätverk) om ett bibliotek finns",
        "en": "Use RNNoise (neural network) if a library is present",
    },
    "settings.rnnoise_rescan": {"sv": "Sök efter RNNoise igen", "en": "Look for RNNoise again"},
    "settings.open_logs": {"sv": "Öppna loggmapp", "en": "Open log folder"},
    "settings.reset": {"sv": "Återställ standard", "en": "Reset to defaults"},
    "settings.rnnoise_none": {
        "sv": "Ingen RNNoise-fil hittad - appens egen brusreducering används, "
              "vilket är det normala på Windows.",
        "en": "No RNNoise file found - the app's own noise reduction is in use, "
              "which is normal on Windows.",
    },
    "settings.rnnoise_active": {"sv": "Aktiv: {kind}\n{path}", "en": "Active: {kind}\n{path}"},
    "settings.rnnoise_kind.native": {"sv": "RNNoise-bibliotek", "en": "RNNoise library"},
    "settings.rnnoise_kind.ladspa": {"sv": "LADSPA-plugin", "en": "LADSPA plugin"},

    "settings.status.stopped": {
        "sv": "Ljudbehandlingen är inte igång.",
        "en": "Audio processing is not running.",
    },
    "settings.status.bypassed": {
        "sv": "Ljudet passerar orört.",
        "en": "Audio passes through untouched.",
    },

    "settings.autostart_failed": {
        "sv": "Kunde inte ändra autostart i registret.",
        "en": "Could not change the startup entry in the registry.",
    },
    "settings.shortcut_done": {
        "sv": "Genvägen ligger nu på skrivbordet och i Startmenyn.",
        "en": "The shortcut is now on the desktop and in the Start Menu.",
    },
    "settings.shortcut_failed": {
        "sv": "Kunde inte skapa genvägen. Se app.log för detaljer.",
        "en": "Could not create the shortcut. See app.log for details.",
    },
    "settings.reset_confirm": {
        "sv": "Återställ alla inställningar till standard?",
        "en": "Reset all settings to their defaults?",
    },
    "settings.rnnoise_not_found": {
        "sv": "Hittade ingen användbar fil.\n\nAppen behöver ett RNNoise-bibliotek "
              "(exporterar rnnoise_create) eller ett LADSPA-plugin (exporterar "
              "ladspa_descriptor), 64-bit. VST2-, VST3- och LV2-filer går inte "
              "att läsa.\n\nGenomsökta mappar:\n{dirs}\n\nSe app.log för vad som "
              "hittades och varför det inte dög.",
        "en": "No usable file was found.\n\nThe app needs an RNNoise library "
              "(exports rnnoise_create) or a LADSPA plugin (exports "
              "ladspa_descriptor), 64-bit. VST2, VST3 and LV2 files cannot be "
              "read.\n\nFolders searched:\n{dirs}\n\nSee app.log for what was "
              "found and why it was not usable.",
    },

    # ------------------------------------------------------------ hover help
    "help.language": {
        "sv": "Byter språk i det här fönstret och i menyn i systemfältet.",
        "en": "Changes the language of this window and of the system tray menu.",
    },
    "help.close": {
        "sv": "Stänger fönstret. Appen fortsätter köra i systemfältet.",
        "en": "Closes this window. The app keeps running in the system tray.",
    },
    "help.input_device": {
        "sv": "Mikrofonen som ska rensas - ditt headset, webbkameran eller den "
              "inbyggda.\n\nVälj aldrig CABLE Output här. Då läser appen sin egen "
              "utgång och du får rundgång.",
        "en": "The microphone to clean up - your headset, webcam or built-in "
              "mic.\n\nNever choose CABLE Output here. The app would read its own "
              "output and you would get a feedback loop.",
    },
    "help.output_device": {
        "sv": "Vart det rensade ljudet skickas. Normalt CABLE Input.\n\nNamnen är "
              "sedda från kabelns håll: appen skriver IN i kabeln, Teams läser UT "
              "ur den. Därför väljer du CABLE Output i Teams.",
        "en": "Where the cleaned audio is sent. Normally CABLE Input.\n\nThe names "
              "are from the cable's point of view: the app writes INTO the cable "
              "and Teams reads OUT of it. That is why you pick CABLE Output in Teams.",
    },
    "help.rescan": {
        "sv": "Läser om listan över ljudenheter. Klicka om du just kopplat in ett "
              "headset eller installerat VB-CABLE.",
        "en": "Re-reads the list of audio devices. Click it after plugging in a "
              "headset or installing VB-CABLE.",
    },
    "help.cable_download": {
        "sv": "Öppnar nedladdningssidan för VB-CABLE. Utan den virtuella kabeln "
              "kan andra program inte komma åt det rensade ljudet.",
        "en": "Opens the VB-CABLE download page. Without the virtual cable, other "
              "programs cannot reach the cleaned audio.",
    },
    "help.preset_light": {
        "sv": "Dämpar jämnt bakgrundsbrus omkring 14 dB och lämnar pauserna "
              "orörda.\n\nVälj den om rösten är viktigast och lite rumsljud inte stör.",
        "en": "Reduces steady background noise by about 14 dB and leaves the "
              "pauses untouched.\n\nPick it when the voice matters most and a "
              "little room sound bothers nobody.",
    },
    "help.preset_balanced": {
        "sv": "22 dB dämpning och en grind som sänker pauserna till -28 dB.\n\n"
              "Rimlig kompromiss i ett vanligt kontorsrum.",
        "en": "22 dB of reduction and a gate that lowers the pauses to -28 dB.\n\n"
              "A sensible compromise in an ordinary office.",
    },
    "help.preset_aggressive": {
        "sv": "30 dB dämpning och en grind som tystar pauserna helt.\n\nDet här är "
              "läget för „bara rösten ska höras“. Byt till Balanserad om svaga ord "
              "kapas.",
        "en": "30 dB of reduction and a gate that silences the pauses completely."
              '\n\nThis is the "only my voice should be heard" mode. Switch to '
              "Balanced if quiet words get cut off.",
    },
    "help.meter_in": {
        "sv": "Nivån som kommer in från mikrofonen, före behandling. Ska röra sig "
              "tydligt när du pratar.\n\nRör den sig inte alls är fel mikrofon "
              "vald, eller så är den avstängd i Windows.",
        "en": "The level coming in from the microphone, before processing. It "
              "should move clearly when you speak.\n\nIf it does not move at all, "
              "the wrong microphone is selected or it is muted in Windows.",
    },
    "help.meter_out": {
        "sv": "Nivån som skickas vidare. Ska följa rösten och falla till ingenting "
              "i pauserna.",
        "en": "The level being sent on. It should follow your voice and drop to "
              "nothing in the pauses.",
    },
    "help.monitor_enabled": {
        "sv": "Spelar upp det rensade ljudet i dina hörlurar så att du hör "
              "resultatet.\n\nAnvänd vid inställning och stäng av före samtalet, "
              "annars hör du dig själv hela tiden. Använd hörlurar, inte högtalare.",
        "en": "Plays the cleaned audio in your headphones so you can hear the "
              "result.\n\nUse it while tuning and turn it off before the call, or "
              "you will hear yourself the whole time. Use headphones, not speakers.",
    },
    "help.monitor_device": {
        "sv": "Vilka hörlurar medhörningen spelas i. Inte den virtuella kabeln.",
        "en": "Which headphones the monitoring plays through. Not the virtual cable.",
    },
    "help.monitor_gain_db": {
        "sv": "Hur starkt du hör dig själv i medhörningen. Påverkar inte vad andra hör.",
        "en": "How loud you hear yourself while monitoring. Does not affect what "
              "others hear.",
    },
    "help.autostart": {
        "sv": "Lägger appen i Windows autostart, så den finns i systemfältet direkt "
              "efter inloggning.",
        "en": "Adds the app to Windows startup, so it is in the system tray right "
              "after you sign in.",
    },
    "help.shortcut": {
        "sv": "Skapar en genväg med appens ikon på skrivbordet och i Startmenyn, så "
              "du slipper leta upp programmappen.",
        "en": "Creates a shortcut with the app's icon on the desktop and in the "
              "Start Menu, so you do not have to find the program folder.",
    },
    "help.reduction_db": {
        "sv": "Hur hårt jämnt bakgrundsbrus dämpas medan du pratar - fläktar, "
              "ventilation, brus.\n\nHögre ger renare bakgrund. Över ungefär 35 dB "
              "kan rösten börja låta ihålig.",
        "en": "How hard steady background noise is reduced while you speak - "
              "fans, ventilation, hiss.\n\nHigher gives a cleaner background. Above "
              "roughly 35 dB the voice can start to sound hollow.",
    },
    "help.highpass_hz": {
        "sv": "Skär bort allt under den här frekvensen: rummel, fläktdunk, stötar i "
              "bordet.\n\nHöj mot 120 Hz om det dunkar. Sänk mot 60 Hz om en mörk "
              "röst tappar botten.",
        "en": "Cuts everything below this frequency: rumble, fan thump, knocks on "
              "the desk.\n\nRaise it towards 120 Hz if you hear thumping. Lower it "
              "towards 60 Hz if a deep voice loses its body.",
    },
    "help.input_gain_db": {
        "sv": "Förstärker mikrofonsignalen före behandlingen.\n\nHöj om In-mätaren "
              "knappt rör sig när du pratar. En för svag signal gör det svårare att "
              "skilja röst från brus.",
        "en": "Amplifies the microphone signal before processing.\n\nRaise it if "
              "the In meter barely moves when you speak. A signal that is too weak "
              "makes it harder to tell voice from noise.",
    },
    "help.makeup_gain_db": {
        "sv": "Justerar nivån på det som skickas vidare.\n\nHöj om motparten tycker "
              "att du låter svag. En begränsare hindrar överstyrning.",
        "en": "Adjusts the level of what is sent on.\n\nRaise it if people say you "
              "sound quiet. A limiter prevents clipping.",
    },
    "help.gate_enabled": {
        "sv": "Slår på brusgrinden, som tystar helt när ingen pratar.\n\nStäng av "
              "den om du hellre vill ha en jämn, svag bakgrund än total tystnad.",
        "en": "Turns on the noise gate, which goes fully silent when nobody is "
              "speaking.\n\nTurn it off if you would rather have a steady, faint "
              "background than total silence.",
    },
    "help.gate_floor_db": {
        "sv": "Hur tyst det blir i pauserna. -60 dB är i praktiken helt tyst, "
              "-25 dB lämnar kvar en antydan om rummet.\n\nEn helt död linje kan få "
              "motparten att tro att samtalet brutits. -40 dB låter mer naturligt.",
        "en": "How quiet the pauses become. -60 dB is effectively silent; -25 dB "
              "leaves a hint of the room.\n\nA completely dead line can make people "
              "think the call dropped. -40 dB sounds more natural.",
    },
    "help.vad_threshold_db": {
        "sv": "Hur mycket tal som krävs för att grinden ska öppna.\n\nDet här är "
              "reglaget att röra först. Höj om bakgrundsljud slinker igenom i "
              "pauserna. Sänk om svaga eller inledande ord kapas.",
        "en": "How much speech it takes to open the gate.\n\nThis is the slider to "
              "try first. Raise it if background sound slips through in the pauses. "
              "Lower it if quiet or opening words get cut off.",
    },
    "help.vad_hangover_ms": {
        "sv": "Hur länge grinden hålls öppen efter sista stavelsen.\n\nHöj mot "
              "300 ms om ordslut och väsljud som „s“ kapas. Sänk om det dröjer för "
              "länge innan det blir tyst.",
        "en": "How long the gate stays open after the last syllable.\n\nRaise it "
              'towards 300 ms if word endings and sounds like "s" get cut off. '
              "Lower it if it takes too long to go quiet.",
    },
    "help.gate_attack_ms": {
        "sv": "Hur snabbt grinden går upp när du börjar prata.\n\nHöj mot 15-20 ms "
              "om det knäpper vid varje ordstart. För långsamt kapar början av ord.",
        "en": "How quickly the gate opens when you start speaking.\n\nRaise it "
              "towards 15-20 ms if you hear a click at the start of each word. Too "
              "slow cuts off the beginnings of words.",
    },
    "help.gate_release_ms": {
        "sv": "Hur snabbt grinden går ner när du slutat prata.\n\nSnabbt ger tvärare "
              "tystnad, långsamt låter mjukare men släpper igenom lite mer.",
        "en": "How quickly the gate closes after you stop speaking.\n\nFast gives a "
              "more abrupt silence; slow sounds smoother but lets a little more through.",
    },
    "help.use_rnnoise": {
        "sv": "Använder RNNoise, ett litet neuralt nätverk, som försteg. Det tar "
              "ojämnt brus - tangentbord, papper, trafik - bättre än klassisk "
              "signalbehandling.\n\nKräver ett RNNoise-bibliotek eller ett "
              "LADSPA-plugin (64-bit) bredvid programmet, eller i en mapp som heter "
              "rnnoise. Det finns inget färdigbyggt för Windows - VST- och "
              "LV2-filerna som cirkulerar är plugin-format som appen inte kan läsa. "
              "Appen fungerar bra utan.",
        "en": "Uses RNNoise, a small neural network, as a first stage. It handles "
              "uneven noise - keyboards, paper, traffic - better than classic signal "
              "processing.\n\nRequires an RNNoise library or a LADSPA plugin "
              "(64-bit) next to the program, or in a folder called rnnoise. Nothing "
              "prebuilt exists for Windows - the VST and LV2 files that circulate "
              "are plugin formats the app cannot read. The app works well without it.",
    },
    "help.rnnoise_rescan": {
        "sv": "Letar igenom programmappen efter ett RNNoise-bibliotek eller "
              "LADSPA-plugin igen. Använd efter att du kopierat in filen.",
        "en": "Searches the program folder again for an RNNoise library or LADSPA "
              "plugin. Use it after copying the file in.",
    },
    "help.open_logs": {
        "sv": "Öppnar mappen med config.json och app.log. Loggen är första stället "
              "att titta om något inte fungerar.",
        "en": "Opens the folder with config.json and app.log. The log is the first "
              "place to look when something does not work.",
    },
    "help.reset": {
        "sv": "Återställer alla reglage till standard. Dina valda ljudenheter och "
              "ditt språkval behålls.",
        "en": "Resets every slider to its default. Your chosen audio devices and "
              "language are kept.",
    },
    "help.restart_audio": {
        "sv": "Stänger och öppnar ljudströmmarna igen. Prova om ljudet tystnat eller "
              "om du bytt ljudenhet.",
        "en": "Closes and reopens the audio streams. Try it if the audio has gone "
              "silent or you changed audio device.",
    },

    # ------------------------------------------------------------ shortcut
    "shortcut.description": {
        "sv": "Brusreducering för mikrofonen",
        "en": "Microphone noise reduction",
    },

    # ------------------------------------------------------------ launcher
    "launcher.preparing": {
        "sv": "Förbereder appen för första starten",
        "en": "Preparing the app for its first start",
    },
    "launcher.creating_env": {"sv": "Skapar en Python-miljö...", "en": "Creating a Python environment..."},
    "launcher.updating_pip": {"sv": "Uppdaterar pip...", "en": "Updating pip..."},
    "launcher.installing": {
        "sv": "Installerar beroenden. Det här tar ungefär en minut...",
        "en": "Installing dependencies. This takes about a minute...",
    },
    "launcher.venv_failed": {
        "sv": "Kunde inte skapa Python-miljön.\n\n{output}\n\nKontrollera att "
              "Python 3.9 eller nyare är installerat från\n{url}",
        "en": "Could not create the Python environment.\n\n{output}\n\nMake sure "
              "Python 3.9 or newer is installed from\n{url}",
    },
    "launcher.install_failed": {
        "sv": "Kunde inte installera beroendena.\n\n{output}\n\nHar datorn "
              "internetanslutning?",
        "en": "Could not install the dependencies.\n\n{output}\n\nIs the computer "
              "connected to the internet?",
    },
    "launcher.failed": {"sv": "Installationen misslyckades.", "en": "The installation failed."},
    "launcher.start_failed": {
        "sv": "Kunde inte starta appen:\n\n{error}",
        "en": "Could not start the app:\n\n{error}",
    },
    "launcher.still_missing": {
        "sv": "Beroendena kunde inte laddas trots installationen.\n\nSaknas: "
              "{modules}\n\nTa bort mappen .venv och försök igen.",
        "en": "The dependencies could not be loaded despite the installation.\n\n"
              "Missing: {modules}\n\nDelete the .venv folder and try again.",
    },
}
