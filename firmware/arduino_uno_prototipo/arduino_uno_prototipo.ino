// Monitor de umidade do solo - 3 zonas + solenoide com histerese
// Saída serial em JSON (1 linha por segundo) para o gateway/backend ler.

const uint8_t PIN_ANALOGICO[3] = {A0, A1, A2};
const uint8_t PIN_DIGITAL[3]   = {2, 3, 4};
const uint8_t PIN_SOLENOIDE    = 7;   // vai para módulo relé/MOSFET, NUNCA direto na válvula

const int LIMIAR_SECO = 700;          // maior valor = mais seco (calibrar!)
const int HISTERESE   = 50;           // evita liga/desliga em torno do limiar
bool valvulaAberta = false;

void setup() {
  for (uint8_t i = 0; i < 3; i++) pinMode(PIN_DIGITAL[i], INPUT);
  pinMode(PIN_SOLENOIDE, OUTPUT);
  digitalWrite(PIN_SOLENOIDE, LOW);   // se o relé for ativo em LOW, inverta a lógica
  Serial.begin(9600);
}

void loop() {
  int leitura[3];
  long soma = 0;
  for (uint8_t i = 0; i < 3; i++) {
    leitura[i] = analogRead(PIN_ANALOGICO[i]);
    soma += leitura[i];
  }
  int media = soma / 3;

  if (!valvulaAberta && media > LIMIAR_SECO) valvulaAberta = true;
  else if (valvulaAberta && media < (LIMIAR_SECO - HISTERESE)) valvulaAberta = false;
  digitalWrite(PIN_SOLENOIDE, valvulaAberta ? HIGH : LOW);

  Serial.print(F("{\"z1\":")); Serial.print(leitura[0]);
  Serial.print(F(",\"z2\":")); Serial.print(leitura[1]);
  Serial.print(F(",\"z3\":")); Serial.print(leitura[2]);
  Serial.print(F(",\"seco\":["));
  for (uint8_t i = 0; i < 3; i++) {
    Serial.print(digitalRead(PIN_DIGITAL[i]) == HIGH ? 1 : 0);  // HIGH = seco na maioria dos módulos
    if (i < 2) Serial.print(',');
  }
  Serial.print(F("],\"valvula\":")); Serial.print(valvulaAberta ? 1 : 0);
  Serial.println('}');
  delay(1000);
}
