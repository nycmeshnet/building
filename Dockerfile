FROM python:3.14.2-slim-bookworm

WORKDIR /usr/src/app

COPY building ./building/
COPY dashboard ./dashboard/
COPY manage.py ./
COPY requirements.txt ./

RUN pip install --no-cache-dir --require-hashes -r requirements.txt

ENTRYPOINT python manage.py migrate && python manage.py runserver 0.0.0.0:8080
