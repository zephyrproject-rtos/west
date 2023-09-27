FROM archlinux:latest
CMD ["/west/docker-testing/in-container-test.sh"]

RUN pacman -Syu --noconfirm \
           git \
           python-pip \
    && pacman -Scc --noconfirm

RUN pip3 install --break-system-packages tox
