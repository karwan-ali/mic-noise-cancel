# Mic Noise Cancel

Real-time microphone noise suppression for Windows. It lives in the system
tray, strips background noise out of your microphone and goes completely silent
between sentences, so the people you talk to hear your voice and little else.

The interface is available in **English and Swedish**.

---

## Read this first

Windows doesn't let an ordinary program create a new microphone. That takes a
signed kernel driver. The standard way around it, which commercial tools use
too, is a **virtual audio cable**:

```
   Microphone  ──►  Mic Noise Cancel  ──►  CABLE Input
                                                │
                                      (the cable, inside Windows)
                                                │
                                                ▼
                      Teams / Zoom / Discord ◄── CABLE Output
```

1. Install **VB-CABLE** (free) from <https://vb-audio.com/Cable/>. Download it,
   unzip it, right-click `VBCABLE_Setup_x64.exe` → *Run as administrator*,
   click *Install Driver* and restart the computer.
2. Start this app. It finds the cable on its own.
3. In Teams, Zoom, Discord or OBS, choose **CABLE Output** as the microphone.

The names are from the cable's point of view: the app writes *into* the cable
and the meeting app reads *out of* it. Never pick CABLE Output as the app's own
microphone, or it will end up listening to itself.

Without the cable the app still runs, but only for monitoring in your
headphones, because other programs can't reach the cleaned audio. The app tells
you when the cable is missing and has a button that takes you to the download.

---

## Getting started

**Double-click `MicNoiseCancel.pyw`.** That's it. Windows associates `.pyw`
with Python and starts it without a console window. The first time, it builds
its own environment, installs the dependencies and then starts the app. A small
window shows the progress, and it takes about a minute.

You need Python 3.9 or newer from <https://www.python.org/downloads/windows/>.
Tick *Add python.exe to PATH* during installation.

**Want it on the desktop?** Open *Settings → General* and click *Create desktop
shortcut*. You get a shortcut with the app's own icon on the desktop and in the
Start Menu. The same tab has *Start automatically with Windows*.

**No Python on the target machine?** Run `python build_exe.py` once on a
computer that has Python. You get `dist\MicNoiseCancel.exe`, a single file that
runs anywhere.

From a terminal, `python run.py` works too, and shows the log as it goes.

The app doesn't open a window when it starts. It goes straight to the system
tray in the bottom right corner. Click the `^` arrow if the icon is hidden, and
drag it out if you want to keep it visible.

---

## Language

The app starts in the language Windows itself uses: Swedish on a Swedish
Windows, English everywhere else. To change it:

- right-click the tray icon → **Språk / Language**, or
- use the **Språk / Language** picker at the top right of the settings window.

That menu is labelled in both languages on purpose, so you can find it even if
the app is currently speaking one you don't read. The choice is saved and
applies right away to the menu, the settings window, notifications and error
messages. Even an error that's already on screen switches language.

---

## The tray menu

Right-click the icon:

| Item | What it does |
|---|---|
| **Noise reduction on** | Turns processing on and off. Off passes the audio straight through. |
| **Level** | Light, Balanced or Aggressive. |
| **Microphone** | Which microphone is read. |
| **Send to** | Where the cleaned audio goes, normally *CABLE Input*. |
| **Monitor in headphones** | Lets you hear the result yourself. Useful while tuning, but turn it off afterwards. |
| **Settings...** | Opens the window below. Double-clicking the icon does the same. |
| **Språk / Language** | Svenska or English. |
| **Restart audio** | For when you've changed or plugged in audio hardware. |
| **Quit** | Closes the app. |

The icon shows the state: green is active, grey is bypassed or stopped, and red
means something went wrong.

---

## Settings

**Hover over any setting** to see what it does and when it's worth changing.
The tables below summarise the same text.

The **General** tab has the devices, the three levels, level meters,
monitoring, startup and the shortcut button. The meters show input and output
levels live, along with whether speech is being detected right now. They're the
quickest way to check that the microphone actually works.

The **Advanced** tab has the controls behind the levels:

| Control | Meaning |
|---|---|
| **Reduction strength** | How hard steady noise is reduced while you speak. Higher gives a cleaner background but can sound slightly metallic. |
| **High-pass filter** | Cuts low-frequency rumble, fan thump and knocks on the desk. |
| **Input / output gain** | For a microphone that's too quiet or too loud. A limiter prevents clipping. |
| **Silence completely between sentences** | The noise gate. With it off, the app does plain noise reduction. |
| **Gate depth** | How quiet the pauses get. -60 dB is effectively silent. |
| **Voice threshold** | How much speech it takes to open the gate. **This is the one to try first** if something sounds wrong. |
| **Hold time** | How long the gate stays open after the last syllable. |
| **Attack / release** | How quickly the gate opens and closes. |

Changes are heard immediately. Everything is saved in
`%APPDATA%\MicNoiseCancel\config.json`, with the log next to it as `app.log`.

### If something sounds wrong

- **Word endings or quiet words get cut off:** raise *Hold time* to 250-350 ms,
  or lower *Voice threshold* half a step.
- **Background noise slips through in the pauses:** raise *Voice threshold*.
- **The voice sounds hollow or watery:** lower *Reduction strength* to
  18-22 dB, or pick Balanced.
- **There's a click when the gate opens:** raise *Attack* to 15-20 ms.

---

## How it works

Audio is processed in 10 ms blocks at 48 kHz. For each block:

1. **High-pass filter** removes rumble below about 90 Hz.
2. **Noise estimate.** A sliding minimum per frequency band over 1.6 seconds
   tracks the background level. The gaps between syllables expose the floor, so
   no speech/silence decision is needed. A running minimum always sits a bit
   below the true level, but by a stable factor (measured at 2.50), so the app
   simply divides it out.
3. **Wiener filter** attenuates each band according to its signal-to-noise
   ratio, with *decision-directed* estimation to keep the metallic "musical
   noise" down.
4. **Speech detection.** The score is the mean of `log(1 + SNR)` across the
   250-4500 Hz speech band. That beats raw band energy by a wide margin at low
   SNR: a couple of formants are enough to register, while bands with only
   noise in them contribute almost nothing.
5. **Noise gate** that attenuates to -60 dB when nobody speaks. It's smoothed
   in decibels, so the attack and release times mean what they say.
6. **Limiter** that makes sure nothing clips.

The input and output devices run on separate clocks, so the two streams are
joined by a 40 ms ring buffer. The buffer is left alone until it drifts past a
watermark, and then it's nudged back. Ordinary jitter costs nothing, and a real
clock difference costs one faint seam every few minutes.

Measured on synthetic speech: background reduced by 53 dB with a headset
(20 dB input SNR) and 74 dB in a noisy room (3 dB SNR), speech kept within
1-3 dB of the clean signal, and the gate opening 20-40 ms after speech starts.
Latency is about 50 ms, and processing takes 0.06 ms per 10 ms block, which is
roughly 160 times faster than real time.

### RNNoise (optional, and rarely available on Windows)

RNNoise is a small neural network that handles uneven noise like keyboards,
paper and traffic better than classic signal processing does. The app can use it
as a first stage but works well without it, and most people on Windows won't
have it.

The app accepts two kinds of file and tells them apart by the symbols they
export, not by file name:

- an **RNNoise library** exporting `rnnoise_create`, which is what you get if
  you build [Xiph's rnnoise](https://github.com/xiph/rnnoise) yourself;
- a **LADSPA plugin** exporting `ladspa_descriptor`.

Put the file next to the program or in an `rnnoise` subfolder, then click *Look
for RNNoise again* under *Settings → Advanced*.

**Why this is awkward:** there's no prebuilt RNNoise library for Windows. The
closest thing is
[werman/noise-suppression-for-voice](https://github.com/werman/noise-suppression-for-voice/releases),
which ships `win-rnnoise.zip` with VST2, LV2 and VST3 files. Those are plugin
formats, not the library itself. The app can't host any of them, but it tells
you which format a file is instead of just failing. The LADSPA variant only
exists in the Linux build.

To get RNNoise on Windows you'd have to build Xiph's rnnoise with MSYS2 or
Visual Studio. The gain is modest, though. The gate already silences the
pauses, so RNNoise would mainly help with noise that sits *on top of* your
voice while you speak. When the app has found a file, the line under the
checkbox says which one.

---

## Limitations

- **Needs the virtual cable** for other programs to reach the audio.
- **About 1.6 seconds** after starting, the app is still learning the room, and
  audio passes almost untouched during that time.
- **One voice at a time.** The app tells *speech* from *noise*, not *your voice*
  from *someone else's*. A colleague talking clearly next to you will be heard.
  Filtering them out would take speaker recognition, which this app doesn't
  have.
- **At very low SNR** (your voice less than about 3 dB above the background)
  the gate becomes unreliable. Try Balanced, or move the microphone closer.
- **No echo cancellation.** Use headphones. Speaker sound picked up by the
  microphone is treated like any other sound.
- **Windows is tested by hand, not by the suite.** The app has been run on
  Windows with both languages. The automated suite runs on any platform, so it
  only reaches the Windows-only parts (WASAPI devices, the Win32 tray backend,
  shortcuts and the startup entry) through stand-ins. If something fails there,
  `app.log` says why.

---

## Tests

```
python tests/run_all.py
```

Seven suites, 328 checks:

- **Signal processing:** perfect reconstruction, noise reduction and speech
  preservation in two kinds of room, gate behaviour and onset latency,
  adaptation when the noise level changes, transients, the limiter, and
  robustness against silence and overdriven input.
- **Audio engine:** ring buffer, 60 simulated seconds of clock drift,
  resampling, bypass and thread safety. The audio callbacks are fed synthetic
  sound directly, so the tests never record your microphone.
- **User interface:** the tray menu and the settings window are built for real,
  in both languages.
- **Application:** startup, error handling, language switching and shutdown.
- **Launcher:** checks that it needs only the standard library, builds a
  working environment and hands over to it.
- **LADSPA host:** the test compiles `tests/fake_ladspa.c` and runs the whole
  chain through a real shared library, so the struct layout and port handling
  are checked against an actual binary rather than assumed. It's skipped if
  there's no C compiler.
- **Translations:** every string exists in both languages with matching
  placeholders, every key the code asks for exists, every string in the table
  is used, and no Swedish is hard-coded outside the translation table. A new
  setting without hover help, or a label typed straight into the code, fails
  the suite.

---

## Project layout

```
MicNoiseCancel.pyw   double-click to start (builds the environment the first time)
build_exe.py         build dist\MicNoiseCancel.exe
run.py               entry point from a terminal
micnoise/
  dsp.py             noise reduction, speech detection, noise gate
  engine.py          audio streams, ring buffers, clock drift, watchdog
  ring.py            ring buffer
  resample.py        resampling for devices that refuse 48 kHz
  devices.py         device discovery and virtual cable detection
  rnnoise.py         finds and loads RNNoise, whatever the file format
  ladspa.py          minimal LADSPA host for prebuilt RNNoise plugins
  i18n.py            every user-visible string, in Swedish and English
  tray.py            the system tray icon and menu
  settings_ui.py     the settings window
  config.py          saved settings and presets
  autostart.py       start with Windows
  shortcut.py        desktop and Start Menu shortcuts
  icon.py            the icons, drawn in code
  app.py             ties it all together
tests/               the test suites
```

### Adding or changing text

All user-visible text lives in `micnoise/i18n.py`, with both languages side by
side under one key. Call `i18n.t("some.key")` from the code instead of writing
the text inline. Log messages are the exception. They stay in English and don't
go through the table. `tests/test_i18n.py` will tell you if a translation is
missing, a placeholder doesn't match, or a string was hard-coded.

---

## License

[MIT](LICENSE). Use it, change it and ship it, just keep the copyright notice.
