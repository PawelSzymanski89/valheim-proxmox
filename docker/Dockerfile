# Valheim dedicated server + admin panel, the same install as setup.sh, in a container
# that boots systemd. The panel drives the game through systemd (units, timers, the
# journal), so the container runs it as PID 1 instead of rewriting the panel around a
# process supervisor - see docker/README.md for what that costs and why it is worth it.
FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv dbus ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    # nothing in here is a real machine: no getty, no fsck, no time sync
    && systemctl mask getty.target console-getty.service systemd-logind.service \
       systemd-timesyncd.service e2scrub_reap.service \
       sys-kernel-config.mount sys-kernel-debug.mount sys-kernel-tracing.mount >/dev/null 2>&1 || true

COPY setup.sh /tmp/setup.sh
COPY panel /tmp/panel
# The regular installer, told to lay files down without starting anything: no game
# download (1.5 GB would bake a stale build into the image), no panel.env (its ntfy
# topic must differ per container), no `systemctl --now`. What it made moves aside to
# /opt/valheim-image; firstboot.sh brings it into the /opt/valheim volume at run time.
RUN SETUP_MODE=image bash /tmp/setup.sh && mv /opt/valheim /opt/valheim-image \
    && rm -rf /tmp/setup.sh /tmp/panel /var/lib/apt/lists/*

COPY docker/firstboot.sh /usr/local/bin/valheim-firstboot
COPY docker/entry.sh /usr/local/bin/valheim-entry
COPY docker/valheim-firstboot.service /etc/systemd/system/valheim-firstboot.service
RUN chmod +x /usr/local/bin/valheim-firstboot /usr/local/bin/valheim-entry \
    && systemctl enable valheim-firstboot.service \
    && mkdir -p /etc/systemd/system/valheim.service.d /etc/systemd/system/valheim-panel.service.d \
    && printf '[Unit]\nAfter=valheim-firstboot.service\nRequires=valheim-firstboot.service\n' \
         | tee /etc/systemd/system/valheim.service.d/firstboot.conf \
               /etc/systemd/system/valheim-panel.service.d/firstboot.conf >/dev/null

VOLUME ["/opt/valheim"]
EXPOSE 2456/udp 2457/udp 2460/tcp
# SIGRTMIN+3 is how systemd is asked to shut down; the game gets its SIGINT and saves
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/bin/valheim-entry"]
