//! User interface text in Swedish and English.
//!
//! Every string the user can see is a variant of `S`, and the `strings!` macro
//! makes the compiler demand both translations for each one - a string cannot
//! be added in one language and forgotten in the other. Log messages are not
//! translated; they are for whoever debugs the app.

use std::sync::atomic::{AtomicU8, Ordering};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lang {
    Sv,
    En,
}

impl Lang {
    pub const ALL: [Lang; 2] = [Lang::Sv, Lang::En];

    pub fn code(self) -> &'static str {
        match self {
            Lang::Sv => "sv",
            Lang::En => "en",
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Lang::Sv => "Svenska",
            Lang::En => "English",
        }
    }

    pub fn from_code(code: &str) -> Option<Lang> {
        Lang::ALL.into_iter().find(|l| l.code() == code)
    }
}

/// Deliberately bilingual and outside the table: someone stuck in a language
/// they cannot read must still be able to find the way out.
pub const LANGUAGE_LABEL: &str = "Språk / Language";

static CURRENT: AtomicU8 = AtomicU8::new(1); // English until told otherwise

/// The language the operating system presents its own interface in.
pub fn detect() -> Lang {
    #[cfg(windows)]
    {
        let langid = unsafe { windows_sys::Win32::Globalization::GetUserDefaultUILanguage() };
        if langid & 0x3FF == 0x1D {
            return Lang::Sv; // LANG_SWEDISH
        }
        Lang::En
    }
    #[cfg(not(windows))]
    {
        for var in ["LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"] {
            if let Ok(v) = std::env::var(var) {
                let v = v.trim().to_lowercase();
                if !v.is_empty() && v != "c" && v != "posix" {
                    return if v.starts_with("sv") { Lang::Sv } else { Lang::En };
                }
            }
        }
        Lang::En
    }
}

/// Switch language. Unknown or empty codes mean "follow the system".
pub fn set_language(code: Option<&str>) -> Lang {
    let lang = code.and_then(Lang::from_code).unwrap_or_else(detect);
    CURRENT.store(lang as u8, Ordering::Relaxed);
    lang
}

pub fn language() -> Lang {
    if CURRENT.load(Ordering::Relaxed) == Lang::Sv as u8 {
        Lang::Sv
    } else {
        Lang::En
    }
}

/// The text for `key` in the current language.
pub fn t(key: S) -> &'static str {
    let (sv, en) = key.texts();
    match language() {
        Lang::Sv => sv,
        Lang::En => en,
    }
}

/// The text for `key` with `{name}` placeholders filled in.
pub fn tf(key: S, params: &[(&str, &str)]) -> String {
    let mut text = t(key).to_string();
    for (name, value) in params {
        text = text.replace(&format!("{{{name}}}"), value);
    }
    text
}

macro_rules! strings {
    ($($key:ident => $sv:expr, $en:expr;)*) => {
        #[derive(Clone, Copy, Debug, PartialEq, Eq)]
        pub enum S { $($key),* }

        impl S {
            #[allow(dead_code)] // the tests walk it
            pub const ALL: &'static [S] = &[$(S::$key),*];

            pub fn texts(self) -> (&'static str, &'static str) {
                match self { $(S::$key => ($sv, $en)),* }
            }
        }
    };
}

strings! {
    // ------------------------------------------------------------ status
    StatusActive => "Aktiv", "Active";
    StatusStopped => "Stoppad", "Stopped";
    StatusBypassed => "Förbikopplad", "Bypassed";
    StatusError => "Fel", "Error";

    // ------------------------------------------------------------ presets
    PresetLight => "Lätt", "Light";
    PresetBalanced => "Balanserad", "Balanced";
    PresetAggressive => "Aggressiv (endast röst)", "Aggressive (voice only)";

    // ------------------------------------------------------------ shared actions
    ActionRestartAudio => "Starta om ljudet", "Restart audio";
    ActionClose => "Stäng", "Close";

    // ------------------------------------------------------------ app
    AppAlreadyRunning =>
        "{app} körs redan. Ikonen finns i systemfältet.",
        "{app} is already running. Look for its icon in the system tray.";
    AppStartFailed =>
        "{app} kunde inte starta:\n\n{error}\n\nLogg: {log}",
        "{app} could not start:\n\n{error}\n\nLog: {log}";
    AppStartFailedTitle => "{app} - kunde inte starta", "{app} - could not start";
    AppNoCable =>
        "Ingen virtuell ljudkabel hittades. Öppna inställningarna för att installera \
         VB-CABLE, annars kan andra program inte använda det rensade ljudet.",
        "No virtual audio cable was found. Open the settings to install VB-CABLE - \
         without it, other programs cannot use the cleaned audio.";
    AppReady =>
        "Klart. Välj „{device}“ som mikrofon i Teams, Zoom eller Discord.",
        "Ready. Choose \"{device}\" as the microphone in Teams, Zoom or Discord.";

    // ------------------------------------------------------------ engine
    EngineNoInput => "Ingen mikrofon vald eller hittad.", "No microphone selected or found.";
    EngineNoOutput =>
        "Ingen utgång vald. Installera VB-CABLE och välj „CABLE Input“.",
        "No output selected. Install VB-CABLE and choose \"CABLE Input\".";
    EngineOpenInputFailed =>
        "Kunde inte öppna mikrofonen „{device}“: {error}",
        "Could not open the microphone \"{device}\": {error}";
    EngineOpenOutputFailed =>
        "Kunde inte öppna utgången „{device}“: {error}",
        "Could not open the output \"{device}\": {error}";

    // ------------------------------------------------------------ tray
    TrayEnabled => "Brusreducering på", "Noise reduction on";
    TrayLevel => "Nivå", "Level";
    TrayNeural => "Neuralt nätverk", "Neural network";
    NeuralOff => "Av", "Off";
    NeuralRnnoise => "RNNoise (lätt)", "RNNoise (light)";
    NeuralDeepFilterNet => "DeepFilterNet (bäst)", "DeepFilterNet (best)";
    TrayMicrophone => "Mikrofon", "Microphone";
    TraySendTo => "Skicka till", "Send to";
    TrayMonitor => "Medhörning i hörlurar", "Monitor in headphones";
    TraySettings => "Inställningar...", "Settings...";
    TrayQuit => "Avsluta", "Quit";
    TrayNoDevices => "(inga enheter hittades)", "(no devices found)";
    TrayMoreInSettings => "Fler i Inställningar...", "More in Settings...";
    TrayHeadlineError => "Fel - se Inställningar", "Error - see Settings";
    TrayHeadlineNoCable => "Aktiv (ingen virtuell kabel)", "Active (no virtual cable)";
    TrayHeadlineActive => "Aktiv - rensar mikrofonen", "Active - cleaning the microphone";
    TrayTipError => "Fel: {error}", "Error: {error}";
    TrayTipInput => "In:  {device}", "In:   {device}";
    TrayTipOutput => "Ut:  {device}", "Out: {device}";

    // ------------------------------------------------------------ settings window
    SettingsTitle => "{app} - inställningar", "{app} - settings";
    SettingsTabGeneral => "Allmänt", "General";
    SettingsTabAdvanced => "Avancerat", "Advanced";

    SettingsDevices => "Ljudenheter", "Audio devices";
    SettingsMicrophone => "Mikrofon", "Microphone";
    SettingsOutput => "Skicka rensat ljud till", "Send cleaned audio to";
    SettingsRescanDevices => "Sök enheter igen", "Rescan devices";
    SettingsDownloadCable => "Ladda ner VB-CABLE", "Download VB-CABLE";
    SettingsNoneFound => "(ingen hittad)", "(none found)";
    SettingsCableOk =>
        "Klart. Välj „{device}“ som mikrofon i Teams, Zoom, Discord eller OBS.",
        "Ready. Choose \"{device}\" as the microphone in Teams, Zoom, Discord or OBS.";
    SettingsCableMissing =>
        "Den valda utgången är inte en virtuell ljudkabel, så andra program kan inte \
         använda det rensade ljudet som mikrofon. Installera VB-CABLE och välj \
         „CABLE Input“ här.",
        "The selected output is not a virtual audio cable, so other programs cannot \
         use the cleaned audio as a microphone. Install VB-CABLE and choose \
         \"CABLE Input\" here.";

    SettingsNoiseReduction => "Brusreducering", "Noise reduction";
    SettingsHoverPresets =>
        "Håll muspekaren över ett val för att se vad det gör.",
        "Hover over an option to see what it does.";

    SettingsLevels => "Nivåer", "Levels";
    SettingsMeterIn => "In", "In";
    SettingsMeterOut => "Ut", "Out";
    SettingsVoiceDetected => "Röst upptäckt - grinden är öppen", "Voice detected - the gate is open";
    SettingsQuiet => "Tyst - bakgrundsljudet tystas", "Quiet - background noise is being muted";

    SettingsMonitorAndStartup => "Medhörning och start", "Monitoring and startup";
    SettingsMonitorEnabled =>
        "Lyssna på det rensade ljudet i hörlurarna",
        "Listen to the cleaned audio in headphones";
    SettingsHeadphones => "Hörlurar", "Headphones";
    SettingsMonitorVolume => "Medhörningsvolym", "Monitor volume";
    SettingsAutostart =>
        "Starta automatiskt när Windows startar",
        "Start automatically with Windows";
    SettingsShortcutCreate => "Skapa genväg på skrivbordet", "Create desktop shortcut";
    SettingsShortcutUpdate => "Uppdatera genvägen", "Update the shortcut";

    SettingsReduction => "Reduktionsstyrka", "Reduction strength";
    SettingsHighpass => "Högpassfilter", "High-pass filter";
    SettingsInputGain => "Ingångsförstärkning", "Input gain";
    SettingsOutputGain => "Utgångsförstärkning", "Output gain";
    SettingsGate => "Brusgrind", "Noise gate";
    SettingsGateEnabled => "Tysta helt mellan meningarna", "Silence completely between sentences";
    SettingsGateFloor => "Grindens djup", "Gate depth";
    SettingsVadThreshold => "Rösttröskel", "Voice threshold";
    SettingsHangover => "Efterhäng", "Hold time";
    SettingsAttack => "Öppningstid", "Attack";
    SettingsRelease => "Stängningstid", "Release";
    SettingsHoverSliders =>
        "Håll muspekaren över ett reglage för att se vad det gör och när det är \
         värt att röra.",
        "Hover over a slider to see what it does and when it is worth changing.";

    SettingsOther => "Övrigt", "Other";
    SettingsNeural => "Neuralt nätverk", "Neural network";
    SettingsDeepFilterLimit => "Max dämpning (DFN)", "Max reduction (DFN)";
    SettingsOpenLogs => "Öppna loggmapp", "Open log folder";
    SettingsReset => "Återställ standard", "Reset to defaults";

    SettingsStatusStopped => "Ljudbehandlingen är inte igång.", "Audio processing is not running.";
    SettingsStatusBypassed => "Ljudet passerar orört.", "Audio passes through untouched.";

    SettingsAutostartFailed =>
        "Kunde inte ändra autostart i registret.",
        "Could not change the startup entry in the registry.";
    SettingsShortcutDone =>
        "Genvägen ligger nu på skrivbordet och i Startmenyn.",
        "The shortcut is now on the desktop and in the Start Menu.";
    SettingsShortcutFailed =>
        "Kunde inte skapa genvägen. Se app.log för detaljer.",
        "Could not create the shortcut. See app.log for details.";
    SettingsResetConfirm =>
        "Återställ alla inställningar till standard?",
        "Reset all settings to their defaults?";

    // ------------------------------------------------------------ hover help
    HelpLanguage =>
        "Byter språk i det här fönstret och i menyn i systemfältet.",
        "Changes the language of this window and of the system tray menu.";
    HelpClose =>
        "Stänger fönstret. Appen fortsätter köra i systemfältet.",
        "Closes this window. The app keeps running in the system tray.";
    HelpInputDevice =>
        "Mikrofonen som ska rensas - ditt headset, webbkameran eller den inbyggda.\n\n\
         Välj aldrig CABLE Output här. Då läser appen sin egen utgång och du får rundgång.",
        "The microphone to clean up - your headset, webcam or built-in mic.\n\n\
         Never choose CABLE Output here. The app would read its own output and you \
         would get a feedback loop.";
    HelpOutputDevice =>
        "Vart det rensade ljudet skickas. Normalt CABLE Input.\n\nNamnen är sedda från \
         kabelns håll: appen skriver IN i kabeln, Teams läser UT ur den. Därför väljer du \
         CABLE Output i Teams.",
        "Where the cleaned audio is sent. Normally CABLE Input.\n\nThe names are from the \
         cable's point of view: the app writes INTO the cable and Teams reads OUT of it. \
         That is why you pick CABLE Output in Teams.";
    HelpRescan =>
        "Läser om listan över ljudenheter. Klicka om du just kopplat in ett headset \
         eller installerat VB-CABLE.",
        "Re-reads the list of audio devices. Click it after plugging in a headset or \
         installing VB-CABLE.";
    HelpCableDownload =>
        "Öppnar nedladdningssidan för VB-CABLE. Utan den virtuella kabeln kan andra \
         program inte komma åt det rensade ljudet.",
        "Opens the VB-CABLE download page. Without the virtual cable, other programs \
         cannot reach the cleaned audio.";
    HelpPresetLight =>
        "Dämpar jämnt bakgrundsbrus omkring 14 dB och lämnar pauserna orörda.\n\n\
         Välj den om rösten är viktigast och lite rumsljud inte stör.",
        "Reduces steady background noise by about 14 dB and leaves the pauses \
         untouched.\n\nPick it when the voice matters most and a little room sound \
         bothers nobody.";
    HelpPresetBalanced =>
        "22 dB dämpning och en grind som sänker pauserna till -28 dB.\n\n\
         Rimlig kompromiss i ett vanligt kontorsrum.",
        "22 dB of reduction and a gate that lowers the pauses to -28 dB.\n\n\
         A sensible compromise in an ordinary office.";
    HelpPresetAggressive =>
        "30 dB dämpning och en grind som tystar pauserna helt.\n\nDet här är läget för \
         „bara rösten ska höras“. Byt till Balanserad om svaga ord kapas.",
        "30 dB of reduction and a gate that silences the pauses completely.\n\nThis is \
         the \"only my voice should be heard\" mode. Switch to Balanced if quiet words \
         get cut off.";
    HelpMeterIn =>
        "Nivån som kommer in från mikrofonen, före behandling. Ska röra sig tydligt när \
         du pratar.\n\nRör den sig inte alls är fel mikrofon vald, eller så är den \
         avstängd i Windows.",
        "The level coming in from the microphone, before processing. It should move \
         clearly when you speak.\n\nIf it does not move at all, the wrong microphone is \
         selected or it is muted in Windows.";
    HelpMeterOut =>
        "Nivån som skickas vidare. Ska följa rösten och falla till ingenting i pauserna.",
        "The level being sent on. It should follow your voice and drop to nothing in \
         the pauses.";
    HelpMonitorEnabled =>
        "Spelar upp det rensade ljudet i dina hörlurar så att du hör resultatet.\n\n\
         Använd vid inställning och stäng av före samtalet, annars hör du dig själv hela \
         tiden. Använd hörlurar, inte högtalare.",
        "Plays the cleaned audio in your headphones so you can hear the result.\n\n\
         Use it while tuning and turn it off before the call, or you will hear yourself \
         the whole time. Use headphones, not speakers.";
    HelpMonitorDevice =>
        "Vilka hörlurar medhörningen spelas i. Inte den virtuella kabeln.",
        "Which headphones the monitoring plays through. Not the virtual cable.";
    HelpMonitorGain =>
        "Hur starkt du hör dig själv i medhörningen. Påverkar inte vad andra hör.",
        "How loud you hear yourself while monitoring. Does not affect what others hear.";
    HelpAutostart =>
        "Lägger appen i Windows autostart, så den finns i systemfältet direkt efter \
         inloggning.",
        "Adds the app to Windows startup, so it is in the system tray right after you \
         sign in.";
    HelpShortcut =>
        "Skapar en genväg med appens ikon på skrivbordet och i Startmenyn, så du slipper \
         leta upp programmappen.",
        "Creates a shortcut with the app's icon on the desktop and in the Start Menu, so \
         you do not have to find the program folder.";
    HelpReduction =>
        "Hur hårt jämnt bakgrundsbrus dämpas medan du pratar - fläktar, ventilation, \
         brus.\n\nHögre ger renare bakgrund. Över ungefär 35 dB kan rösten börja låta \
         ihålig.",
        "How hard steady background noise is reduced while you speak - fans, \
         ventilation, hiss.\n\nHigher gives a cleaner background. Above roughly 35 dB \
         the voice can start to sound hollow.";
    HelpHighpass =>
        "Skär bort allt under den här frekvensen: rummel, fläktdunk, stötar i bordet.\n\n\
         Höj mot 120 Hz om det dunkar. Sänk mot 60 Hz om en mörk röst tappar botten.",
        "Cuts everything below this frequency: rumble, fan thump, knocks on the desk.\n\n\
         Raise it towards 120 Hz if you hear thumping. Lower it towards 60 Hz if a deep \
         voice loses its body.";
    HelpInputGain =>
        "Förstärker mikrofonsignalen före behandlingen.\n\nHöj om In-mätaren knappt rör \
         sig när du pratar. En för svag signal gör det svårare att skilja röst från brus.",
        "Amplifies the microphone signal before processing.\n\nRaise it if the In meter \
         barely moves when you speak. A signal that is too weak makes it harder to tell \
         voice from noise.";
    HelpMakeupGain =>
        "Justerar nivån på det som skickas vidare.\n\nHöj om motparten tycker att du \
         låter svag. En begränsare hindrar överstyrning.",
        "Adjusts the level of what is sent on.\n\nRaise it if people say you sound \
         quiet. A limiter prevents clipping.";
    HelpGateEnabled =>
        "Slår på brusgrinden, som tystar helt när ingen pratar.\n\nStäng av den om du \
         hellre vill ha en jämn, svag bakgrund än total tystnad.",
        "Turns on the noise gate, which goes fully silent when nobody is speaking.\n\n\
         Turn it off if you would rather have a steady, faint background than total \
         silence.";
    HelpGateFloor =>
        "Hur tyst det blir i pauserna. -60 dB är i praktiken helt tyst, -25 dB lämnar \
         kvar en antydan om rummet.\n\nEn helt död linje kan få motparten att tro att \
         samtalet brutits. -40 dB låter mer naturligt.",
        "How quiet the pauses become. -60 dB is effectively silent; -25 dB leaves a hint \
         of the room.\n\nA completely dead line can make people think the call dropped. \
         -40 dB sounds more natural.";
    HelpVadThreshold =>
        "Hur mycket tal som krävs för att grinden ska öppna.\n\nDet här är reglaget att \
         röra först. Höj om bakgrundsljud slinker igenom i pauserna. Sänk om svaga eller \
         inledande ord kapas.",
        "How much speech it takes to open the gate.\n\nThis is the slider to try first. \
         Raise it if background sound slips through in the pauses. Lower it if quiet or \
         opening words get cut off.";
    HelpHangover =>
        "Hur länge grinden hålls öppen efter sista stavelsen.\n\nHöj mot 300 ms om \
         ordslut och väsljud som „s“ kapas. Sänk om det dröjer för länge innan det blir \
         tyst.",
        "How long the gate stays open after the last syllable.\n\nRaise it towards \
         300 ms if word endings and sounds like \"s\" get cut off. Lower it if it takes \
         too long to go quiet.";
    HelpAttack =>
        "Hur snabbt grinden går upp när du börjar prata.\n\nHöj mot 15-20 ms om det \
         knäpper vid varje ordstart. För långsamt kapar början av ord.",
        "How quickly the gate opens when you start speaking.\n\nRaise it towards \
         15-20 ms if you hear a click at the start of each word. Too slow cuts off the \
         beginnings of words.";
    HelpRelease =>
        "Hur snabbt grinden går ner när du slutat prata.\n\nSnabbt ger tvärare tystnad, \
         långsamt låter mjukare men släpper igenom lite mer.",
        "How quickly the gate closes after you stop speaking.\n\nFast gives a more abrupt \
         silence; slow sounds smoother but lets a little more through.";
    HelpNeural =>
        "Ett neuralt nätverk som rensar ljudet före appens egen brusreducering. Båda \
         är inbyggda i appen.\n\nRNNoise: litet och nästan gratis. Tar ojämnt brus - \
         tangentbord, papper, trafik - bättre än klassisk signalbehandling.\n\n\
         DeepFilterNet: ett mycket större nätverk som också tar hundskall, rinnande \
         vatten, disk och sorl i bakgrunden. Kostar ungefär en millisekund processortid \
         per 10 ms ljud och lägger till cirka 20 ms fördröjning.\n\nAv: bara appens \
         egen brusreducering. Prova om rösten låter konstig eller datorn är mycket svag.",
        "A neural network that cleans the audio before the app's own noise reduction. \
         Both are built into the app.\n\nRNNoise: small and nearly free. Handles uneven \
         noise - keyboards, paper, traffic - better than classic signal processing.\n\n\
         DeepFilterNet: a much larger network that also takes barking dogs, running \
         water, dishes and chatter in the background. Costs about a millisecond of CPU \
         per 10 ms of audio and adds roughly 20 ms of delay.\n\nOff: only the app's own \
         noise reduction. Try it if your voice sounds odd or the computer is very slow.";
    HelpDeepFilterLimit =>
        "Hur mycket DeepFilterNet högst får dämpa bruset. 100 dB betyder ingen gräns.\n\n\
         Sänk till 20-30 dB om rösten låter tunn eller robotaktig: lite bakgrund får \
         då vara kvar, och rösten låter naturligare. Gäller bara när DeepFilterNet är \
         valt.",
        "The most DeepFilterNet may reduce the noise. 100 dB means no limit.\n\nLower it \
         to 20-30 dB if your voice sounds thin or robotic: a little background is then \
         left in, and the voice sounds more natural. Only applies when DeepFilterNet is \
         selected.";
    HelpOpenLogs =>
        "Öppnar mappen med config.json och app.log. Loggen är första stället att titta \
         om något inte fungerar.",
        "Opens the folder with config.json and app.log. The log is the first place to \
         look when something does not work.";
    HelpReset =>
        "Återställer alla reglage till standard. Dina valda ljudenheter och ditt \
         språkval behålls.",
        "Resets every slider to its default. Your chosen audio devices and language are \
         kept.";
    HelpRestartAudio =>
        "Stänger och öppnar ljudströmmarna igen. Prova om ljudet tystnat eller om du \
         bytt ljudenhet.",
        "Closes and reopens the audio streams. Try it if the audio has gone silent or \
         you changed audio device.";

    // ------------------------------------------------------------ shortcut
    ShortcutDescription => "Brusreducering för mikrofonen", "Microphone noise reduction";
}

#[cfg(test)]
mod tests {
    use super::*;

    fn placeholders(text: &str) -> Vec<String> {
        let mut out: Vec<String> = text
            .split('{')
            .skip(1)
            .filter_map(|rest| rest.split_once('}').map(|(name, _)| name.to_string()))
            .collect();
        out.sort();
        out
    }

    #[test]
    fn translations_agree_on_placeholders() {
        for key in S::ALL {
            let (sv, en) = key.texts();
            assert!(!sv.trim().is_empty() && !en.trim().is_empty(), "{key:?} is empty");
            assert_eq!(placeholders(sv), placeholders(en), "{key:?}");
        }
    }

    #[test]
    fn switching_and_formatting() {
        set_language(Some("sv"));
        assert_eq!(t(S::TrayQuit), "Avsluta");
        assert_eq!(tf(S::TrayTipError, &[("error", "x")]), "Fel: x");
        set_language(Some("en"));
        assert_eq!(t(S::TrayQuit), "Quit");
        assert_eq!(language(), Lang::En);
    }
}
