FROM ubuntu:22.04
CMD ["/west/docker-testing/in-container-test.sh"]

RUN apt-get update \
    && apt-get install -y \
        git \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install tox
