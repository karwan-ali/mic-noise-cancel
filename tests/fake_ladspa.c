/* A stand-in LADSPA plugin, written against the published ladspa.h layout.
 *
 * Index 0 is stereo and index 1 is mono, deliberately in that order, so a host
 * that just grabs the first descriptor picks the wrong one.
 *
 * The mono plugin computes  out[i] = in[i] * Gain + Threshold * 0.001  so the
 * host can read both control values back out of the audio it returns.
 */
#include <stdlib.h>
#include <string.h>

typedef float LADSPA_Data;
typedef void * LADSPA_Handle;
typedef int LADSPA_Properties;
typedef int LADSPA_PortDescriptor;
typedef int LADSPA_PortRangeHintDescriptor;

typedef struct _LADSPA_PortRangeHint {
  LADSPA_PortRangeHintDescriptor HintDescriptor;
  LADSPA_Data LowerBound;
  LADSPA_Data UpperBound;
} LADSPA_PortRangeHint;

typedef struct _LADSPA_Descriptor {
  unsigned long UniqueID;
  const char * Label;
  LADSPA_Properties Properties;
  const char * Name;
  const char * Maker;
  const char * Copyright;
  unsigned long PortCount;
  const LADSPA_PortDescriptor * PortDescriptors;
  const char * const * PortNames;
  const LADSPA_PortRangeHint * PortRangeHints;
  void * ImplementationData;
  LADSPA_Handle (*instantiate)(const struct _LADSPA_Descriptor *, unsigned long);
  void (*connect_port)(LADSPA_Handle, unsigned long, LADSPA_Data *);
  void (*activate)(LADSPA_Handle);
  void (*run)(LADSPA_Handle, unsigned long);
  void (*run_adding)(LADSPA_Handle, unsigned long);
  void (*set_run_adding_gain)(LADSPA_Handle, LADSPA_Data);
  void (*deactivate)(LADSPA_Handle);
  void (*cleanup)(LADSPA_Handle);
} LADSPA_Descriptor;

#define PORT_INPUT   0x1
#define PORT_OUTPUT  0x2
#define PORT_CONTROL 0x4
#define PORT_AUDIO   0x8

#define HINT_BOUNDED_BELOW 0x1
#define HINT_BOUNDED_ABOVE 0x2
#define HINT_DEFAULT_MIDDLE 0xC0
#define HINT_DEFAULT_1      0x1C0

/* Observable from the test so it can assert the lifecycle really ran. */
int fake_activate_count = 0;
int fake_cleanup_count = 0;
int fake_run_count = 0;
unsigned long fake_last_samplerate = 0;

typedef struct {
  LADSPA_Data * gain;
  LADSPA_Data * threshold;
  LADSPA_Data * in;
  LADSPA_Data * out;
  LADSPA_Data * in2;
  LADSPA_Data * out2;
} Instance;

static LADSPA_Handle fake_instantiate(const LADSPA_Descriptor * d, unsigned long rate) {
  (void) d;
  fake_last_samplerate = rate;
  return calloc(1, sizeof(Instance));
}

static void fake_connect(LADSPA_Handle h, unsigned long port, LADSPA_Data * loc) {
  Instance * s = (Instance *) h;
  switch (port) {
    case 0: s->gain = loc; break;
    case 1: s->threshold = loc; break;
    case 2: s->in = loc; break;
    case 3: s->out = loc; break;
    case 4: s->in2 = loc; break;
    case 5: s->out2 = loc; break;
    default: break;
  }
}

static void fake_activate(LADSPA_Handle h) { (void) h; fake_activate_count++; }

static void fake_run(LADSPA_Handle h, unsigned long n) {
  Instance * s = (Instance *) h;
  unsigned long i;
  float gain = s->gain ? *s->gain : 0.0f;
  float thr = s->threshold ? *s->threshold : 0.0f;
  fake_run_count++;
  if (!s->in || !s->out) return;
  for (i = 0; i < n; i++) s->out[i] = s->in[i] * gain + thr * 0.001f;
}

static void fake_run_stereo(LADSPA_Handle h, unsigned long n) {
  Instance * s = (Instance *) h;
  unsigned long i;
  if (!s->in || !s->out) return;
  for (i = 0; i < n; i++) s->out[i] = -99.0f;   /* obviously wrong if picked */
  if (s->in2 && s->out2) for (i = 0; i < n; i++) s->out2[i] = -99.0f;
}

static void fake_deactivate(LADSPA_Handle h) { (void) h; }
static void fake_cleanup(LADSPA_Handle h) { fake_cleanup_count++; free(h); }

/* ---- mono: Gain, VAD Threshold (%), Input, Output ---------------------- */

static const LADSPA_PortDescriptor mono_ports[4] = {
  PORT_CONTROL | PORT_INPUT,
  PORT_CONTROL | PORT_INPUT,
  PORT_AUDIO | PORT_INPUT,
  PORT_AUDIO | PORT_OUTPUT,
};
static const char * const mono_names[4] = {
  "Gain", "VAD Threshold (%)", "Input", "Output"
};
static const LADSPA_PortRangeHint mono_hints[4] = {
  { HINT_BOUNDED_BELOW | HINT_BOUNDED_ABOVE | HINT_DEFAULT_1, 0.0f, 10.0f },
  { HINT_BOUNDED_BELOW | HINT_BOUNDED_ABOVE | HINT_DEFAULT_MIDDLE, 0.0f, 100.0f },
  { 0, 0.0f, 0.0f },
  { 0, 0.0f, 0.0f },
};

/* ---- stereo: Input L, Output L, Input R, Output R ---------------------- */

static const LADSPA_PortDescriptor stereo_ports[6] = {
  PORT_CONTROL | PORT_INPUT,
  PORT_CONTROL | PORT_INPUT,
  PORT_AUDIO | PORT_INPUT,
  PORT_AUDIO | PORT_OUTPUT,
  PORT_AUDIO | PORT_INPUT,
  PORT_AUDIO | PORT_OUTPUT,
};
static const char * const stereo_names[6] = {
  "Gain", "VAD Threshold (%)", "Input L", "Output L", "Input R", "Output R"
};
static const LADSPA_PortRangeHint stereo_hints[6] = {
  { HINT_BOUNDED_BELOW | HINT_BOUNDED_ABOVE | HINT_DEFAULT_1, 0.0f, 10.0f },
  { HINT_BOUNDED_BELOW | HINT_BOUNDED_ABOVE | HINT_DEFAULT_MIDDLE, 0.0f, 100.0f },
  { 0, 0.0f, 0.0f }, { 0, 0.0f, 0.0f }, { 0, 0.0f, 0.0f }, { 0, 0.0f, 0.0f },
};

static LADSPA_Descriptor g_stereo;
static LADSPA_Descriptor g_mono;
static int g_ready = 0;

static void build(void) {
  if (g_ready) return;

  memset(&g_stereo, 0, sizeof(g_stereo));
  g_stereo.UniqueID = 9001;
  g_stereo.Label = "fake_stereo";
  g_stereo.Name = "Fake Noise Suppressor (stereo)";
  g_stereo.Maker = "test";
  g_stereo.Copyright = "none";
  g_stereo.PortCount = 6;
  g_stereo.PortDescriptors = stereo_ports;
  g_stereo.PortNames = stereo_names;
  g_stereo.PortRangeHints = stereo_hints;
  g_stereo.instantiate = fake_instantiate;
  g_stereo.connect_port = fake_connect;
  g_stereo.activate = fake_activate;
  g_stereo.run = fake_run_stereo;
  g_stereo.deactivate = fake_deactivate;
  g_stereo.cleanup = fake_cleanup;

  memset(&g_mono, 0, sizeof(g_mono));
  g_mono.UniqueID = 9002;
  g_mono.Label = "fake_mono";
  g_mono.Name = "Fake Noise Suppressor (mono)";
  g_mono.Maker = "test";
  g_mono.Copyright = "none";
  g_mono.PortCount = 4;
  g_mono.PortDescriptors = mono_ports;
  g_mono.PortNames = mono_names;
  g_mono.PortRangeHints = mono_hints;
  g_mono.instantiate = fake_instantiate;
  g_mono.connect_port = fake_connect;
  g_mono.activate = fake_activate;
  g_mono.run = fake_run;
  g_mono.deactivate = fake_deactivate;
  g_mono.cleanup = fake_cleanup;

  g_ready = 1;
}

const LADSPA_Descriptor * ladspa_descriptor(unsigned long index) {
  build();
  if (index == 0) return &g_stereo;
  if (index == 1) return &g_mono;
  return NULL;
}
