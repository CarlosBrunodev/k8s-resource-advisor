# Prometheus FALSO para validação local (não usar em produção).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY server.py ./server.py
RUN useradd --uid 10001 --no-create-home fake
USER 10001

EXPOSE 9090
CMD ["python", "server.py"]
