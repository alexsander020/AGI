FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py alertas.py ./
COPY templates templates
RUN useradd -r -u 10001 app && mkdir /data && chown app /data
USER app
# 1 worker + threads: o app inicia o cliente MQTT e o vigia de offline na importação;
# vários workers duplicariam leituras e alertas.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", "app:app"]
