// Physical feedback model for the Cyber Kinetic City power plant (ESP32-S3, Arduino core 3.x).
//
// Listens for the plant's one-way UDP truth feed (server.py --feedback / $ESP32_FEEDBACK, 10 Hz): {"rpm":3600,"valve":54.0,"running":true,"breaker":true,"tripped":false,"damaged":false}
// and shows it physically:
//   N20 motor (TB6612FNG)  turbine speed: 3600 rpm = steady, overspeed visibly faster, trip coasts down (the coast-down comes from the model's rpm, not from the firmware)
//   MG90S servo            generator breaker: swings between OPEN and CLOSED (opens on trip / loss of mains)
//   onboard RGB LED        green tied to grid, amber islanded, red tripped, red strobe damaged, blue blink = no feed (motor stopped)
//   light pole             green on grid, yellow islanded (flashing above 63 Hz), red tripped (flashing damaged), slow yellow blink = no feed
// It never sends anything back, so it cannot be used to reach or influence the plant.

#include <WiFi.h>
#include <WiFiUdp.h>

// ---- set these ----
const char *WIFI_SSID = "SSID";
const char *WIFI_PASS = "P@SSW0RD";
const uint16_t UDP_PORT = 4210;

// ---- hardware ----
const int PIN_PWMA = 4, PIN_AIN1 = 5, PIN_AIN2 = 6, PIN_STBY = 7;  // TB6612FNG channel A
const int PIN_SERVO = 15;                                          // MG90S signal (orange)
// Light pole (module has its own resistors; GND pin to GND).
const int PIN_GREEN = 16, PIN_YELLOW = 17, PIN_RED = 18;
const float WARN_RPM = 3780;  // 63 Hz: yellow flashes while the turbine is heading for the 66 Hz trip

// set SUPPLY_V to the bench supply; N20 is rated 6 V
const float SUPPLY_V = 5.0, MOTOR_MAX_V = 6.0;  
// Speed curve: 0..3600 rpm -> MIN_DUTY..NOMINAL_DUTY, 3600..4000 -> NOMINAL_DUTY..full (trip is 3960).
// Overspeed is only +10 % in the model
// below this the gearmotor stalls; raise if yours won't start
const float MIN_DUTY = 0.25;      
// 60 Hz
const float NOMINAL_DUTY = 0.65;  
// servo pulse per position; swap to reverse direction
const int BREAKER_OPEN_US = 1000, BREAKER_CLOSED_US = 1900;  
const unsigned long FEED_TIMEOUT_MS = 2000;

const int MOTOR_HZ = 20000, MOTOR_BITS = 10;
const int SERVO_HZ = 50, SERVO_BITS = 14;

WiFiUDP udp;
float rpm = 0;
bool running = false, breaker = false, tripped = false, damaged = false;
unsigned long lastPacket = 0;


bool field(const char *json, const char *key, float &out) {
  char pattern[24];
  snprintf(pattern, sizeof pattern, "\"%s\":", key);
  const char *p = strstr(json, pattern);
  if (!p) return false;
  p += strlen(pattern);
  while (*p == ' ') p++;
  if (!strncmp(p, "true", 4)) out = 1;
  else if (!strncmp(p, "false", 5)) out = 0;
  else {
    char *end;
    out = strtof(p, &end);
    if (end == p || !isfinite(out)) return false;
  }
  return true;
}

bool parse(const char *json) {
  float r, run, brk, trp, dmg;
  if (!(field(json, "rpm", r) && field(json, "running", run) &&
        field(json, "breaker", brk) && field(json, "tripped", trp) && field(json, "damaged", dmg)))
    return false; 
  rpm = constrain(r, 0, 10000);
  running = run, breaker = brk, tripped = trp, damaged = dmg;
  return true;
}

void setMotor(float duty) { 
  digitalWrite(PIN_AIN1, duty > 0);
  digitalWrite(PIN_AIN2, LOW);
  ledcWrite(PIN_PWMA, (uint32_t)(duty * ((1 << MOTOR_BITS) - 1)));
}

void setBreaker(bool closed) {
  int us = closed ? BREAKER_CLOSED_US : BREAKER_OPEN_US;
  ledcWrite(PIN_SERVO, (uint32_t)(us / 20000.0 * ((1 << SERVO_BITS) - 1))); 
}

void setLed(uint8_t r, uint8_t g, uint8_t b) {
#ifdef RGB_BUILTIN
  rgbLedWrite(RGB_BUILTIN, r, g, b);
#endif
}

void setLights(bool green, bool yellow, bool red) {
  digitalWrite(PIN_GREEN, green);
  digitalWrite(PIN_YELLOW, yellow);
  digitalWrite(PIN_RED, red);
}

void setup() {
  Serial.begin(115200);
  for (int pin : {PIN_GREEN, PIN_YELLOW, PIN_RED}) pinMode(pin, OUTPUT);
  setLights(false, false, false);
  pinMode(PIN_AIN1, OUTPUT);
  pinMode(PIN_AIN2, OUTPUT);
  pinMode(PIN_STBY, OUTPUT);
  digitalWrite(PIN_STBY, HIGH);
  ledcAttach(PIN_PWMA, MOTOR_HZ, MOTOR_BITS);
  ledcAttach(PIN_SERVO, SERVO_HZ, SERVO_BITS);
  setMotor(0);
  setBreaker(false);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    setLed(0, 0, (millis() / 250) % 2 ? 40 : 0);
    delay(50);
  }
  udp.begin(UDP_PORT);
  Serial.printf("feedback model ready: set ESP32_FEEDBACK=%s:%u\n", WiFi.localIP().toString().c_str(), UDP_PORT);
}

void loop() {
  char buf[256];
  int size;
  while ((size = udp.parsePacket()) > 0) {  
    int n = udp.read(buf, sizeof buf - 1);
    buf[max(n, 0)] = 0;
    if (parse(buf)) lastPacket = millis();
  }

  unsigned long now = millis();
  bool fed = lastPacket && now - lastPacket < FEED_TIMEOUT_MS;
  float maxDuty = min(MOTOR_MAX_V / SUPPLY_V, 1.0f);  

  if (!fed) {  
    setMotor(0);
    setLed(0, 0, (now / 500) % 2 ? 60 : 0);
    setLights(false, (now / 500) % 2, false);
    return;
  }

  float level = rpm < 60 ? 0 : rpm <= 3600 ? MIN_DUTY + (NOMINAL_DUTY - MIN_DUTY) * rpm / 3600 : NOMINAL_DUTY + (1 - NOMINAL_DUTY) * min((rpm - 3600) / 400.0f, 1.0f);
  float duty = level * maxDuty;
  if (damaged && duty > 0) duty *= (now / 90) % 3 ? 0.4 : 1.0; 
  setMotor(duty);
  setBreaker(breaker);

  if (damaged) setLed((now / 100) % 2 ? 255 : 0, 0, 0);
  else if (tripped) setLed(200, 0, 0);
  else if (breaker) setLed(0, 160, 0);
  else if (running) setLed(180, 90, 0);
  else setLed(20, 20, 20);

  bool fast = (now / 150) % 2;
  bool overspeed = !tripped && rpm > WARN_RPM;
  setLights(breaker && !tripped, unning && !breaker, damaged ? fast : tripped); 
}
