ARG TARGET

FROM ${TARGET}

ARG TARGET
ENV WEST_TARGET=${TARGET}

RUN pacman -Syu --noconfirm \
           git \
           python-pip \
    && pacman -Scc --noconfirm

RUN pip3 install --break-system-packages tox
