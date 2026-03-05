/*
  NeoPixel (WS2812) Busy Light – OOP version with detailed comments
  -----------------------------------------------------------------
  PURPOSE
    Drive a single WS2812/NeoPixel LED as a "busy light" controlled over Serial.
    The device accepts simple, human-readable commands and supports a heartbeat
    watchdog that turns the LED off if the host stops sending PINGs.

  PROTOCOL (ASCII lines ending with \n or \r\n)
    Color:
      - "R G B"            e.g., "255 0 0" sets solid red
      - "RGB r g b"        same as above with an "RGB" prefix
      - "#RRGGBB"          hex color, e.g., "#FF8000"
      - "OFF"              turn LED off

    Heartbeat / status:
      - "PING"             replies "PONG" and resets heartbeat timer
      - "HBEN 0|1"         disable/enable heartbeat watchdog (auto-off on timeout)
      - "HBEN?"            replies with the heartbeat watchdog disable/enable status
      - "HBTO <ms>"        set heartbeat timeout in milliseconds; min clamp = 500ms; max clamp = 86400000ms (one day)
      - "HBTO?"            replies with the heartbeat timeout in milliseconds;
      - "HBRST"            reset heartbeat timer to "now"
      - "STAT?"            prints "STAT <ok|timeout> RGB r g b"
      - "VER?"             replies with the version of the Arduino code

    Test:
      - "TEST"             cycles R→G→B→White→Off for quick verification

  ELECTRICAL / HARDWARE
    - Uses Adafruit NeoPixel library on a *single* LED by default.
    - Default data pin = D4; you can change in cfg::LED_PIN.
    - Keep GND common with the host/device.

  DESIGN
    - OOP structure with clear responsibilities:
        * NeoPixelLed      -> hardware control of the LED
        * Heartbeat        -> timeout logic for auto-off
        * SerialLineReader -> non-blocking line-based serial input
        * BusyLightApp     -> command parsing & orchestration
    - Memory-conscious: small fixed buffers, no dynamic allocation.
    - The single app instance is held as a function-local static.

  DEPENDENCIES
    - Adafruit NeoPixel (install via Arduino Library Manager)
    - Works on typical AVR boards (Uno, Nano, Leonardo/Pro Micro), and others.

  BAUD RATE
    - 115200 (match with your Python GUI/app)

  COPYRIGHT / LICENSE
    - This project is released as open-source software under the MIT License, Copyright (c) 2025 Allan Juhl Kristensen
    - The software is provided “as is,” without any express or implied warranty.
*/

#include <Arduino.h>
#include <Adafruit_NeoPixel.h>
#include <ctype.h>   // toupper
#include <string.h>  // strlen, strcmp, strncpy
#include <stdlib.h>  // strtol, atoi, atol
#include <EEPROM.h>

// Version string
constexpr const char* VERSION = "1.0.0";

// --- Workaround for Arduino's auto-prototype order ---
class BusyLightApp;            // forward-declare the class so it's a known type
static BusyLightApp& app();    // forward-declare the function, too

// ---------- Compile-time configuration (kept as constexpr, not as globals) ----------
namespace cfg {
  // NeoPixel pin and count. For a single LED, NUM_LEDS = 1.
  constexpr uint8_t  LED_PIN  = 4;
  constexpr uint16_t NUM_LEDS = 1;

  // Serial BAUD. Must match your host (e.g., Python app).
  constexpr uint32_t BAUD     = 115200;

  // Heartbeat defaults: enable auto-off and set a sensible timeout.
  constexpr bool     HB_DEFAULT_ENABLED   = true;
  constexpr uint32_t HB_DEFAULT_TIMEOUT_MS = 25000UL; // 25 seconds works well with 10s PING interval
  constexpr uint32_t HB_MIN_TIMEOUT_MS = 500UL; // clamp to a safe 0.5s minimum
  constexpr uint32_t HB_MAX_TIMEOUT_MS = 86400000UL; // One day (24*60*60*1000)
  
  // WS2812 brightness (0..255). Host can scale brightness before sending RGB;
  // KSeep the pixel at full power here.
  constexpr uint8_t  BRIGHTNESS     = 255;
}

namespace eeprom_cfg {
  constexpr int      ADDR    = 0;        // EEPROM start address
  constexpr uint16_t MAGIC   = 0xB11E;   // "B11E" = BusyLight-ish
  constexpr uint8_t  VERSION = 1;
}

// ---------- Small utility functions ----------
/* Returns true for standard whitespace characters used by our parser. */
static inline bool isSpaceC(char c) {
  return (c==' ' || c=='\t' || c=='\r' || c=='\n');
}

/* Trims leading/trailing whitespace IN PLACE and returns pointer to the first non-space. */
static char* trimInPlace(char* s) {
  while (*s && isSpaceC(*s)) s++; // skip leading space
  if (!*s) return s; // empty or all space -> return ""
  char* e = s + strlen(s) - 1; // last char
  while (e > s && isSpaceC(*e)) { *e = 0; e--; } // remove trailing space
  return s;
}

// ---------- NeoPixel driver (LED hardware abstraction) ----------
class NeoPixelLed {
public:
  /* Construct with number of LEDs and the data pin. Use GRB order @ 800kHz. */
  NeoPixelLed(uint16_t n, uint8_t pin)
  : strip_(n, pin, NEO_GRB + NEO_KHZ800), desired_{0, 0, 0}, actual_{0, 0, 0} {}

  /* Initialize the NeoPixel, set brightness, and turn it off. */
  void begin() {
    strip_.begin();
    strip_.setBrightness(cfg::BRIGHTNESS);
    showActual_();
  }

  // Force LED OFF due to heartbeat timeout without losing desired color.
  void forceOff() {
    actual_ = {0, 0, 0};
    showActual_();
  }

  // Restore actual color to desired (used when heartbeat resumes).
  void restoreDesired() {
    actual_ = desired_;
    showActual_();
  }

  /* Set the current color (0..255 per channel) and push to the LED. */
  void setRGB(uint8_t r, uint8_t g, uint8_t b) {
    desired_ = {r, g, b};
    actual_  = desired_;
    showActual_();
  }

  /* Simple power-on blink: dim gray → off, so you know the firmware is running. */
  void bootBlink() {
    delay(120);
    setRGB(24,24,24);
    delay(120);
    setRGB(0,0,0);
  }

  /* A small color cycle test for manual verification. */
  void testPattern() {
    setRGB(255,0,0);     delay(250);
    setRGB(0,255,0);     delay(250);
    setRGB(0,0,255);     delay(250);
    setRGB(255,255,255); delay(250);
    setRGB(0,0,0);       delay(200);
  }

  /* Read back the last commanded RGB (what is on the LED). */
  void getRGB(uint8_t& r, uint8_t& g, uint8_t& b) const {
    r = actual_.r; g = actual_.g; b = actual_.b;
  }

private:
  struct Rgb {
    uint8_t r;
    uint8_t g;
    uint8_t b;
  };

  void showActual_() {
    strip_.setPixelColor(0, strip_.Color(actual_.r, actual_.g, actual_.b));
    strip_.show();
  }

  Adafruit_NeoPixel strip_;
  // To keep track of the last desired color to restore actual color to desired (used when heartbeat resumes)
  Rgb desired_{0, 0, 0};
  // To keep track of the last commanded color so STAT? can report it. 
  Rgb actual_{0, 0, 0}; 
};

// ---------- Heartbeat watchdog (auto-off when host stops pinging) ----------
class Heartbeat {
public:
  /* Construct with initial enabled state and timeout in ms. */
  Heartbeat(bool enabled, uint32_t timeoutMs)
  : enabled_(enabled), timeoutMs_(timeoutMs),
    lastPingMs_(0), timedOut_(false) {}

  /* Start or restart the timer. Call once in setup(). */
  void begin() {
    loadFromEeprom_();     // override defaults if valid
    reset();
  }

  /* Call when a PING command is received; marks us as alive. */
  void ping() {
    reset();
  }

  /* Reset: timer restarts, not timed-out. */
  void reset() { 
    lastPingMs_ = millis();
    timedOut_ = false; }

  /* Enable/disable the watchdog at runtime (HBEN). */
  void setEnabled(bool en){
    if (enabled_ == en) return;
    enabled_ = en;
    if (!enabled_) timedOut_ = false;
    saveToEeprom_();
  }
  bool enabled() const { return enabled_; }
  uint32_t timeoutMs() const { return timeoutMs_; }

  /* Configure timeout (HBTO). */
  void setTimeout(uint32_t ms) {
    if (ms < cfg::HB_MIN_TIMEOUT_MS) ms = cfg::HB_MIN_TIMEOUT_MS;
    if (ms > cfg::HB_MAX_TIMEOUT_MS) ms = cfg::HB_MAX_TIMEOUT_MS;
    if (timeoutMs_ == ms) return;
    timeoutMs_ = ms;
    saveToEeprom_();
  }
  uint32_t timeout() const { return timeoutMs_; }

  void resetToDefaultsAndPersist() {
      enabled_   = cfg::HB_DEFAULT_ENABLED;
      timeoutMs_ = cfg::HB_DEFAULT_TIMEOUT_MS;
      timedOut_  = false;
      lastPingMs_ = millis();
      saveToEeprom_();
    }

  /*
    Periodic update. Returns true exactly once when transition into "timed out".
    The caller (app) can use this to perform an action (e.g., force LED off).
    If heartbeat remains disabled, update() does nothing.
  */
  bool update() {
    if (!enabled_) return false;
    const uint32_t now = millis();
    if (!timedOut_ && (now - lastPingMs_ > timeoutMs_)) {
      timedOut_ = true;
      return true; // edge: just timed out this call
    }
    return false;  // either still OK or already timed-out
  }

  bool isTimedOut() const { return timedOut_; }

private:
  struct Persist {
    uint16_t magic;
    uint8_t  version;
    uint8_t  enabled;     // 0/1
    uint32_t timeoutMs;   // heartbeat timeout
    uint8_t  checksum;    // simple checksum
  };

  void loadFromEeprom_() {
    Persist p{};
    EEPROM.get(eeprom_cfg::ADDR, p);

    if (p.magic != eeprom_cfg::MAGIC) return;
    if (p.version != eeprom_cfg::VERSION) return;

    const uint8_t cs = checksum(p);
    if (cs != p.checksum) return;

    enabled_ = (p.enabled != 0);
    timeoutMs_ = p.timeoutMs;

    if (timeoutMs_ < cfg::HB_MIN_TIMEOUT_MS) timeoutMs_ = cfg::HB_MIN_TIMEOUT_MS;
  }

  void saveToEeprom_() const {
    Persist p{};
    p.magic     = eeprom_cfg::MAGIC;
    p.version   = eeprom_cfg::VERSION;
    p.enabled   = enabled_ ? 1 : 0;
    p.timeoutMs = timeoutMs_;
    p.checksum  = checksum(p);

    // EEPROM.put uses update semantics (only writes changed bytes)
    EEPROM.put(eeprom_cfg::ADDR, p);
  }

  uint8_t checksum(const Persist& p) {
    // Simple XOR checksum over all bytes except checksum itself
    const uint8_t* b = reinterpret_cast<const uint8_t*>(&p);
    uint8_t x = 0;
    for (size_t i = 0; i < sizeof(Persist) - 1; ++i) {
      x ^= b[i];
    }
    return x;
  }

  bool     enabled_;
  uint32_t timeoutMs_;
  uint32_t lastPingMs_;
  bool     timedOut_;
};

// ---------- Non-blocking, line-based serial reader ----------
class SerialLineReader {
public:
  SerialLineReader(): len_(0) { buf_[0] = 0; }

  /*
    Read bytes from the given Stream (e.g., Serial) without blocking.
    Accumulates into an internal buffer until a newline (\n or \r) is seen.
    When a full line is ready, returns true and sets lineOut to point to the buffer.
    The line is NUL-terminated and safe to parse. The next call starts a new line.
    If the line would overflow the buffer, it's discarded (len reset to 0).
  */
  bool readLine(Stream& s, const char*& lineOut) {
    while (s.available() > 0) {
      const char c = (char)s.read();
      if (c == '\n' || c == '\r') {
        if (len_ > 0) {
          buf_[len_] = 0; // terminate the string
          lineOut = buf_;
          len_ = 0;
          return true;
        }
        // ignore empty CR/LF sequences
      } else {
        if (len_ < (sizeof(buf_) - 1)) {
          buf_[len_++] = c;
        } else {
          // Overflow: drop current line to protect memory and resync.
          len_ = 0;
        }
      }
    }
    return false;
  }

private:
  static constexpr uint8_t MAX_ = 64; // keep this matched with parser expectations
  char   buf_[MAX_];
  uint8_t len_;
};

// ---------- Main application: parses commands & orchestrates everything ----------
class BusyLightApp {
public:
  BusyLightApp()
  : led_(cfg::NUM_LEDS, cfg::LED_PIN),
    hb_(cfg::HB_DEFAULT_ENABLED, cfg::HB_DEFAULT_TIMEOUT_MS) {}

  /* Initialize Serial, LED, heartbeat, and print a ready banner. */
  void begin() {
    Serial.begin(cfg::BAUD);
    led_.begin();
    led_.bootBlink();   // quick visual check that firmware is alive
    hb_.begin();        // start heartbeat timer window now
    Serial.println(F("BUSYLIGHT READY"));
  }

  /* Main loop: process serial input and heartbeat timeout. */
  void tick() {
    // 1) Handle any pending serial lines (non-blocking)
    const char* line = nullptr;
    while (reader_.readLine(Serial, line)) {
      processLine(line);
    }

    // 2) Heartbeat watchdog: auto-off when host stops pinging
    if (hb_.update()) {
      // Entered "timeout" state → turn LED off once.
      led_.forceOff();
      // Optional debug:
      // Serial.println(F("TIMEOUT"));
    }
  }

private:
  // ---- Parsing helpers ----

  /*
    Parse three integers from a C-string. Accepts whitespace-separated values.
    Clamps each channel to 0..255. Returns true on success.
  */
  static bool parseRGBTriplet(const char* s, int& r, int& g, int& b) {
    char* p;
    r = strtol(s, &p, 10); if (p == s) return false;  // no digits -> fail
    g = strtol(p, &p, 10);
    b = strtol(p, &p, 10);
    r = constrain(r, 0, 255);
    g = constrain(g, 0, 255);
    b = constrain(b, 0, 255);
    return true;
  }

  /* Tiny convenience to reply to PING. Also resets the heartbeat in cmd handler. */
  void replyPong() {
    Serial.println(F("PONG"));
  }

  // ---- Command handlers (each handles its own arguments) ----

  void cmdHBEN(const char* s) {
    const int v = atoi(s);            // "0" or "1" (anything nonzero -> true)
    hb_.setEnabled(v != 0);
    Serial.print(F("HBEN=")); Serial.println(hb_.enabled() ? 1 : 0);
  }

  void cmdHBENQ() {
    Serial.print("HBEN="); Serial.println(hb_.enabled() ? 1 : 0);
  }

  void cmdHBTO(const char* s) {
    long v = atol(s);
    hb_.setTimeout((uint32_t)v);
    Serial.print(F("HBTO=")); Serial.println(hb_.timeout());
  }

  void cmdHBTOQ() {
    Serial.print("HBTO=");
    Serial.println(hb_.timeoutMs());
  }

  void cmdHBRST() {
    hb_.reset();
    Serial.println(F("HBRST=OK"));
  }

  void cmdSTAT() {
    uint8_t r,g,b; led_.getRGB(r,g,b);
    Serial.print(F("STAT="));
    Serial.print(hb_.isTimedOut() ? F("TIMEOUT") : F("OK"));
    Serial.print(F("; RGB="));
    Serial.print(r); Serial.print(' ');
    Serial.print(g); Serial.print(' ');
    Serial.println(b);
  }

  void cmdVERQ() {
    Serial.print("VER="); Serial.print(VERSION);
    Serial.print("; BUILD="); Serial.print(__DATE__);   // "Jan  5 2026"
    Serial.print(" "); Serial.println(__TIME__); // "HH:MM:SS"
  }

  /*
    Parse and act on a single command line.
    Copy 'raw' into a small mutable buffer to safely trim/tokenize in place.
    Supported commands are described at the top of this file.
  */
  void processLine(const char* raw) {
    // Local buffer avoids modifying the caller's memory and keeps things bounded.
    char line[64];
    strncpy(line, raw, sizeof(line)-1);
    line[sizeof(line)-1] = 0;

    // Trim leading/trailing whitespace
    char* s = trimInPlace(line);
    if (!*s) return; // empty line after trimming

    // Extract first token (uppercased) for command matching
    char token[8] = {0};
    uint8_t i = 0;
    while (s[i] && !isSpaceC(s[i]) && i < sizeof(token)-1) {
      token[i] = (char)toupper(s[i]);
      i++;
    }
    token[i] = 0;

    // Advance pointer beyond the first token into "args"
    char* args = s + i;
    while (*args && isSpaceC(*args)) args++;

    // ---- Heartbeat / status commands ----
    if (!strcmp(token, "PING"))  { if (hb_.isTimedOut()) {led_.restoreDesired();}; hb_.ping(); replyPong(); return; }
    if (!strcmp(token, "HBEN"))  { cmdHBEN(args); return; }
    if (!strcmp(token, "HBEN?")) { cmdHBENQ(); return; }
    if (!strcmp(token, "HBTO"))  { cmdHBTO(args); return; }
    if (!strcmp(token, "HBTO?")) { cmdHBTOQ(); return; }
    if (!strcmp(token, "HBRST")) { if (hb_.isTimedOut()) {led_.restoreDesired();}; cmdHBRST(); return; }
    if (!strcmp(token, "STAT?")) { cmdSTAT(); return; }
    if (!strcmp(token, "VER?")) { cmdVERQ(); return; }

    // ---- Test / simple control ----
    if (!strcmp(token, "TEST"))  { led_.testPattern(); return; }
    if (!strcmp(token, "OFF"))   { led_.setRGB(0,0,0); return; }

    // "RGB r g b" form
    if (!strcmp(token, "RGB")) {
      int r,g,b;
      if (parseRGBTriplet(args, r, g, b)) {
        led_.setRGB((uint8_t)r,(uint8_t)g,(uint8_t)b);
        return;
      }
    }

    // Plain "r g b" form (no leading token)
    {
      int r,g,b;
      if (parseRGBTriplet(s, r, g, b)) {
        led_.setRGB((uint8_t)r,(uint8_t)g,(uint8_t)b);
        return;
      }
    }

    // Hex "#RRGGBB" form
    if (s[0] == '#' && strlen(s) == 7) {
      long v = strtol(s + 1, nullptr, 16);
      uint8_t r = (v >> 16) & 0xFF;
      uint8_t g = (v >> 8)  & 0xFF;
      uint8_t b = (v      ) & 0xFF;
      led_.setRGB(r,g,b);
      return;
    }

    // Unknown command → print a lightweight error and continue
    Serial.println(F("ERR"));
  }

private:
  NeoPixelLed       led_;     // hardware LED controller
  Heartbeat         hb_;      // heartbeat watchdog (auto-off)
  SerialLineReader  reader_;  // non-blocking line input
};

// ---------- Single hidden instance ----------
/*
  Avoid a global variable by using a function-local static. This guarantees
  a single instance for the lifetime of the program and defers initialization
  until first use (before setup()).
*/
static BusyLightApp& app() {
  static BusyLightApp instance;
  return instance;
}

// ---------- Arduino entry points ----------
void setup() { app().begin(); }
void loop()  { app().tick();  }
