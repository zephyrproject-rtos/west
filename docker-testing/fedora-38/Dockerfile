FROM fedora:38
CMD ["/west/docker-testing/in-container-test.sh"]

RUN dnf install -y \
        git \
        python3-pip \
    && dnf clean dbcache

RUN pip3 install tox
