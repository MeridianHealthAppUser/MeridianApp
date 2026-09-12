FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_DEBUG=false \
    PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system meridian \
    && useradd --system --gid meridian --home-dir /app meridian
COPY --chown=meridian:meridian . .
# This command-only key is not stored in the runtime environment or used to sign
# any user data. The running app still requires its real secret configuration.
RUN DJANGO_SECRET_KEY=static-build-only-not-a-runtime-key-0123456789abcdef0123456789abcdef python manage.py collectstatic --noinput
USER meridian
EXPOSE 8080
CMD ["sh", "-c", "exec daphne -b 0.0.0.0 -p ${PORT:-8080} --proxy-headers --websocket-max-message-size 65536 --websocket-max-frame-size 65536 --access-log - config.asgi:application"]
