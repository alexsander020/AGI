/*
  Monitor de solo - ESP32 + sensor NPK RS485 (Modbus RTU) + 3 sensores de umidade + solenoide
  Bibliotecas (Gerenciador de Bibliotecas): ModbusMaster (Doc Walker), PubSubClient (Nick O'Leary)
  MQTT com TLS (porta 8883) + usuário/senha + Last Will.

  Ligações:
    MAX3485/MAX485: RO -> GPIO16 (RX2) | DI -> GPIO17 (TX2) | DE+RE juntos -> GPIO4
    Sensor NPK: A/B no módulo RS485, alimentação 12-24 V (fonte externa), GND comum com o ESP32
    Umidade (saída analógica): GPIO34, GPIO35, GPIO32  (ADC1 - ADC2 não funciona com Wi-Fi)
    Relé da solenoide: GPIO26 (válvula em fonte própria, diodo de proteção)
  ATENÇÃO: confira no datasheet do SEU sensor o baud, o endereço e o mapa de registradores.
*/
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>
#include <PubSubClient.h>
#include <ModbusMaster.h>

// ---------- Configuração ----------
const char* WIFI_SSID = "SUA_REDE";
const char* WIFI_PASS = "SUA_SENHA";
const char* MQTT_HOST = "broker.exemplo.com";
const uint16_t MQTT_PORT = 8883;                 // TLS
const char* MQTT_USER = "esp32-solo-01";         // um usuário por dispositivo
const char* MQTT_PASS = "TROQUE_ESTA_SENHA";     // não versione no Git; gere uma senha forte por placa
const char* MQTT_TOPIC  = "solo/zona1/leitura";
const char* MQTT_STATUS = "solo/zona1/status";   // "online"/"offline" (offline via Last Will)
const char* DEVICE_ID  = "esp32-solo-01";

// Certificado da CA que assinou o certificado do broker (formato PEM).
// Let's Encrypt: use o ISRG Root X1. Broker próprio: o ca.crt da sua CA.
const char* CA_CERT = R"EOF(
-----BEGIN CERTIFICATE-----
COLE_AQUI_O_CERTIFICADO_DA_CA
-----END CERTIFICATE-----
)EOF";

const uint8_t  PIN_RX = 16, PIN_TX = 17, PIN_DE_RE = 4;
const uint8_t  PIN_UMID[3] = {34, 35, 32};
const uint8_t  PIN_VALVULA = 26;
const bool     RELE_ATIVO_EM_LOW = false;

// Sensor 7 em 1 comum: 0x0000 umid(0.1%) | 0x0001 temp(0.1C) | 0x0002 EC(uS/cm)
//                      0x0003 pH(0.1) | 0x0004 N | 0x0005 P | 0x0006 K (mg/kg)
const uint8_t  MODBUS_ADDR = 1;
const uint32_t MODBUS_BAUD = 4800;
const uint16_t REG_INICIO = 0x0000;
const uint8_t  REG_QTD = 7;

// Calibração do sensor analógico (meça no ar e na água!)
const int ADC_SECO = 3200, ADC_MOLHADO = 1400;
const int LIMIAR_SECO_PCT = 35, HISTERESE_PCT = 10;   // liga abaixo de 35%, desliga acima de 45%

const uint32_t INTERVALO_MS = 5000;

// ---------- Globais ----------
WiFiClientSecure wifi;
PubSubClient mqtt(wifi);
ModbusMaster node;
bool valvulaAberta = false;
uint32_t ultimaLeitura = 0;

void preTx()  { digitalWrite(PIN_DE_RE, HIGH); }
void postTx() { digitalWrite(PIN_DE_RE, LOW); }

int umidadePct(int bruto) {
  int p = map(bruto, ADC_SECO, ADC_MOLHADO, 0, 100);
  return constrain(p, 0, 100);
}

void conectarWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) delay(250);
}

// TLS valida a data do certificado: sem hora correta a conexão falha.
void sincronizarHora() {
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  uint32_t t0 = millis();
  while (time(nullptr) < 1700000000 && millis() - t0 < 10000) delay(200);
}

void conectarMqtt() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) return;
  static uint32_t ultimaTentativa = 0;
  if (ultimaTentativa && millis() - ultimaTentativa < 5000) return;   // não trava o loop
  ultimaTentativa = millis();
  if (time(nullptr) < 1700000000) sincronizarHora();

  // Last Will: se o ESP32 cair, o broker publica "offline" (retido) no tópico de status
  if (mqtt.connect(DEVICE_ID, MQTT_USER, MQTT_PASS, MQTT_STATUS, 1, true, "offline")) {
    mqtt.publish(MQTT_STATUS, "online", true);
  } else {
    Serial.printf("MQTT falhou, estado=%d (-4 timeout, 5 não autorizado; veja hora e certificado)\n", mqtt.state());
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_DE_RE, OUTPUT); postTx();
  pinMode(PIN_VALVULA, OUTPUT);
  digitalWrite(PIN_VALVULA, RELE_ATIVO_EM_LOW ? HIGH : LOW);
  analogReadResolution(12);

  Serial2.begin(MODBUS_BAUD, SERIAL_8N1, PIN_RX, PIN_TX);
  node.begin(MODBUS_ADDR, Serial2);
  node.preTransmission(preTx);
  node.postTransmission(postTx);

  wifi.setCACert(CA_CERT);        // valida o servidor; NUNCA use setInsecure() em produção
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(30);
  mqtt.setBufferSize(512);        // o padrão (256 B) descartaria o JSON de ~300 B em silêncio
  conectarWifi();
  sincronizarHora();
}

void loop() {
  conectarWifi();
  conectarMqtt();
  mqtt.loop();

  if (millis() - ultimaLeitura < INTERVALO_MS) return;
  ultimaLeitura = millis();

  // 1) Umidade (média das 3 zonas)
  int pct[3], soma = 0;
  for (uint8_t i = 0; i < 3; i++) { pct[i] = umidadePct(analogRead(PIN_UMID[i])); soma += pct[i]; }
  int media = soma / 3;

  // 2) NPK via Modbus
  uint8_t rc = node.readHoldingRegisters(REG_INICIO, REG_QTD);
  bool npkOk = (rc == node.ku8MBSuccess);
  float umidSensor = 0, temp = 0, ph = 0;
  uint16_t ec = 0, n = 0, p = 0, k = 0;
  if (npkOk) {
    umidSensor = node.getResponseBuffer(0) / 10.0;
    temp       = node.getResponseBuffer(1) / 10.0;
    ec         = node.getResponseBuffer(2);
    ph         = node.getResponseBuffer(3) / 10.0;
    n = node.getResponseBuffer(4);
    p = node.getResponseBuffer(5);
    k = node.getResponseBuffer(6);
  }

  // 3) Irrigação com histerese; em falha de sensor, fecha a válvula (fail-safe)
  if (!valvulaAberta && media < LIMIAR_SECO_PCT) valvulaAberta = true;
  else if (valvulaAberta && media > LIMIAR_SECO_PCT + HISTERESE_PCT) valvulaAberta = false;
  bool nivel = RELE_ATIVO_EM_LOW ? !valvulaAberta : valvulaAberta;
  digitalWrite(PIN_VALVULA, nivel ? HIGH : LOW);

  // 4) Publica JSON
  char msg[320];
  snprintf(msg, sizeof(msg),
    "{\"id\":\"%s\",\"z\":[%d,%d,%d],\"umid_media\":%d,\"solo\":\"%s\",\"valvula\":%d,"
    "\"npk_ok\":%d,\"n\":%u,\"p\":%u,\"k\":%u,\"ph\":%.1f,\"ec\":%u,\"temp\":%.1f,\"umid_sensor\":%.1f}",
    DEVICE_ID, pct[0], pct[1], pct[2], media, media < LIMIAR_SECO_PCT ? "SECO" : "UMIDO",
    valvulaAberta, npkOk, n, p, k, ph, ec, temp, umidSensor);
  Serial.println(msg);
  if (mqtt.connected()) mqtt.publish(MQTT_TOPIC, msg);
  if (!npkOk) Serial.printf("Modbus erro 0x%02X (verifique baud, endereço, A/B e alimentação)\n", rc);
}
