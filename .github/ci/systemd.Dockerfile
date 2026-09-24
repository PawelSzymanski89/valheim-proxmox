# A bare Debian 12 that boots systemd - the stand-in for an LXC the installer made, so the
# upgrade test goes through setup.sh the way a real server does (not the Docker image path,
# which cannot update itself).
FROM debian:12
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv dbus ca-certificates curl openssl \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl mask getty.target console-getty.service systemd-logind.service \
       systemd-timesyncd.service >/dev/null 2>&1 || true
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
