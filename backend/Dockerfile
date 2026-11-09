# Back-end: somente biblioteca padrão do Python, sem dependências externas.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY app ./app
RUN useradd --uid 10001 --no-create-home appuser \
 && mkdir /data && chown appuser /data  # volume da fórmula (FORMULA_FILE): herda o dono ao ser criado
USER 10001

EXPOSE 8080
CMD ["python", "-m", "app.server"]
