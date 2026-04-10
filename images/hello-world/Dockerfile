FROM nginxinc/nginx-unprivileged:alpine

COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/
COPY hero.jpg /usr/share/nginx/html/

EXPOSE 8080
