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

The app is a single file, `MicNoiseCancel.exe`. There's nothing to install and
nothing else to download.

**Get the program** in one of two ways:

- **Download it:** open the repository's *Actions* tab on GitHub, pick the
  latest *Build* run and download the `MicNoiseCancel` artifact. It's a zip with
  the .exe inside.
- **Build it yourself:** install Rust from <https://rustup.rs> (the default
  options are fine, including the Visual Studio C++ build tools it offers), then
  run this in the repository folder:

  ```
  cargo build --release
  ```

  The program ends up in `target\release\MicNoiseCancel.exe`.

Put the .exe wherever you like, for example in a folder under `Documents`, and
double-click it.

**Want it on the desktop?** Open *Settings → General* and click *Create desktop
shortcut*. You get a shortcut with the app's own icon on the desktop and in the
Start Menu. The same tab has *Start automatically with Windows*. Both point at
wherever the .exe is when you click them, so move it to its final folder first.

The app doesn't open a window when it starts. It goes straight to the system
tray in the bottom right corner. Click the `^` arrow if the icon is hidden, and
drag it out if you want to keep it visible.

**Coming from the Python version?** Your settings carry over, since they live in
the same file. If you had *Start automatically with Windows* turned on, untick
and tick it again once, so the startup entry points at the new program instead
of Python.

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
| **Settings...** | Opens the window below. Clicking the icon does the same. |
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
Esc or *Close* closes the window; the app keeps running in the tray.

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

Measured on synthetic speech by the test suite: background reduced by 48 dB
(Balanced) and 78 dB (Aggressive), both with a headset and in a noisy room,
speech kept within 1-3 dB of the clean signal, and the gate opening 20-40 ms
after speech starts. Latency is about 50 ms, and processing takes about
0.01 ms per 10 ms block, roughly a thousand times faster than real time.

### RNNoise

RNNoise is a small neural network that handles uneven noise like keyboards,
paper and traffic better than classic signal processing does. It's built into
the program (as [nnnoiseless](https://github.com/jneem/nnnoiseless), a Rust
port of [Xiph's rnnoise](https://github.com/xiph/rnnoise)) and runs before the
app's own noise reduction. It's on by default. If your voice sounds odd with it,
or the computer is very slow, untick *Use RNNoise* under *Settings → Advanced*.

### Memory

The app is written in Rust and talks to Windows directly, with no runtime, no
GUI framework and no browser engine. A few things keep it small:

- The **settings window only exists while it's open.** Closing it destroys
  every control and hands the memory back to Windows.
- **Audio buffers are allocated once** when the streams open. The audio
  callbacks never allocate, which also keeps them free of hiccups.
- **RNNoise's network weights are read straight from the program file** (87 KB)
  instead of being copied into memory.
- **Icons are drawn in code** at exactly the size the taskbar asks for.
- Windows converts the output to the cable's format itself, so the app doesn't
  resample the output.

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
- **The automated tests cover the signal processing, not the Windows parts.**
  The test suite runs on any platform, so it checks the DSP, ring buffer,
  resampler, settings and translations, but not the tray, the settings window
  or the audio devices. On every push, a Windows machine on GitHub builds the
  program, starts it and checks that it keeps running. Beyond that, the Windows
  parts are tested by hand, and if something fails there, `app.log` says why.

---

## Tests

```
cargo test --release
```

- **Signal processing:** perfect reconstruction, noise reduction and speech
  preservation in two kinds of room, gate behaviour and onset latency, the
  limiter, live parameter changes, and robustness against silence and
  overdriven input.
- **Ring buffer and resampler:** wrap-around, overflow, drift trimming and
  padding, and rate accuracy.
- **Settings:** presets, and reading the JSON file the Python version wrote,
  including bad values.
- **Translations:** every string has both languages, with matching
  placeholders. A string missing in one language doesn't even compile.

The tests run on Windows, Linux and macOS. The program itself only builds for
Windows.

---

## Project layout

```
Cargo.toml          dependencies and build settings
build.rs            draws the program icon and embeds it with the manifest
res/app.manifest    themed controls, DPI awareness
src/
  main.rs           entry point
  app.rs            ties it all together: tray menu, startup, the UI thread
  dsp.rs            noise reduction, speech detection, noise gate
  engine.rs         audio streams, RNNoise, ring buffers, watchdog
  ring.rs           ring buffer with clock-drift correction
  resample.rs       resampling for microphones that don't run at 48 kHz
  devices.rs        device discovery and virtual cable detection
  settings.rs       the settings window
  tray.rs           the system tray icon
  i18n.rs           every user-visible string, in Swedish and English
  config.rs         saved settings and presets
  autostart.rs      start with Windows
  shortcut.rs       desktop and Start Menu shortcuts
  icon_draw.rs      the icons, drawn in code
  log.rs            app.log
  win.rs            small Win32 helpers
```

### Adding or changing text

All user-visible text lives in `src/i18n.rs`, with both languages side by side
under one name. Use `t(S::SomeName)` in the code instead of writing the text
inline. Log messages are the exception: they stay in English and don't go
through the table.

---

## License

[MIT](LICENSE). Use it, change it and ship it, just keep the copyright notice.
