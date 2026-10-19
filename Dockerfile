FROM python:3.13.7-slim-bookworm

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY building ./building/
COPY dashboard ./dashboard/
COPY manage.py ./

# Settings won't load without these; the real values come from the Kubernetes secret at runtime
RUN SECRET_KEY=build OIDC_RP_CLIENT_ID=build OIDC_RP_CLIENT_SECRET=build FQDN=build \
    python manage.py collectstatic --noinput

EXPOSE 8080

# Pages wait on several APIs, so allow 2 minutes per request and several requests per worker
ENTRYPOINT ["sh", "-c", "python manage.py migrate && exec gunicorn building.wsgi --bind 0.0.0.0:8080 --workers 2 --threads 4 --timeout 120 --access-logfile -"]
