FROM ubuntu:latest

RUN apt-get update -qqq && apt-get install -qqq -y \
    curl \
    dumb-init \
    python3 > /dev/null

RUN python3 -V
RUN ln -s $(python3 -c "import sys; print(sys.executable)") /usr/local/bin/python

ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python && \
    python -m pip install --upgrade pip && \
    python -m pip install --upgrade uv

COPY ./requirements/base.txt /tmp/requirements/base.txt
RUN python -m uv pip install --quiet --system --upgrade --break-system-packages \
    -r /tmp/requirements/base.txt

RUN mkdir -p /app/client
COPY ./client/ /app/client/
WORKDIR /app

# Disable core dumps
RUN echo "ulimit -c 0" >> /etc/profile

CMD ["dumb-init", "--", "python", "-m", "client.query_runner"]
