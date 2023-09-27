ARG TARGET

FROM ${TARGET}

ARG TARGET
ENV WEST_TARGET=${TARGET}

RUN dnf install -y \
        git \
        python3-pip \
    && dnf clean dbcache

RUN pip3 install tox
