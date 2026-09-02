ARG PYTHON_VERSION=3.14-slim

FROM python:${PYTHON_VERSION}

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# install psycopg2 dependencies.
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /code

WORKDIR /code

COPY requirements.txt /tmp/requirements.txt
RUN set -ex && \
    pip install --upgrade pip && \
    pip install -r /tmp/requirements.txt && \
    rm -rf /root/.cache/
COPY . /code

RUN DJANGO_DEBUG=False python manage.py collectstatic --noinput

EXPOSE 8000

# Access logs to stdout so `fly logs` shows every request. %({fly-client-ip}i)s
# is the real client IP (Fly terminates TLS at its proxy, so %(h)s would just be
# the proxy); %(L)s is the request duration in seconds.
CMD ["gunicorn", "config.wsgi", \
     "--bind", ":8000", \
     "--workers", "2", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--access-logformat", "%({fly-client-ip}i)s \"%(r)s\" %(s)s %(b)s %(L)ss \"%(f)s\" \"%(a)s\""]
