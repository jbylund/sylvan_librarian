FROM ubuntu:latest

# Build arguments for git metadata
ARG GIT_SHA=unknown
ARG GIT_BRANCH=unknown

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qqq && apt-get install -qqq -y \
    build-essential \
    curl \
    dumb-init \
    libev-dev \
    libpq-dev \
    libpython3-dev \
    python3 > /dev/null


# python3-pip

RUN python3 -V
RUN ln -s $(python3 -c "import sys; print(sys.executable)") /usr/local/bin/python

ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python && \
    python -m pip install --upgrade pip && \
    python -m pip install --upgrade uv

COPY ./requirements/ /tmp/requirements/
RUN python -m uv pip install --quiet --system --upgrade --break-system-packages \
    -r /tmp/requirements/base.txt -r /tmp/requirements/webserver.txt

RUN mkdir -p /app
COPY ./api /app/api
WORKDIR /app

# Set git metadata as environment variables
ENV GIT_SHA=${GIT_SHA}
ENV GIT_BRANCH=${GIT_BRANCH}

# Disable core dumps
RUN echo "ulimit -c 0" >> /etc/profile
