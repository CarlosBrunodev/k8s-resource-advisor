# Front-end: arquivos estáticos servidos pelo nginx sem privilégios.
# O template recebe BACKEND_URL (ex.: http://resource-advisor-backend:8080) e faz proxy de /api/.
FROM nginxinc/nginx-unprivileged:1.27-alpine

COPY index.html styles.css app.js /usr/share/nginx/html/
COPY fonts /usr/share/nginx/html/fonts/
COPY assets /usr/share/nginx/html/assets/
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

ENV BACKEND_URL=http://resource-advisor-backend:8080
EXPOSE 8080
