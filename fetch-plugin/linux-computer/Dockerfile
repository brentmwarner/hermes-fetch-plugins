FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DISPLAY=:1 \
    FETCH_GEOMETRY=1280x800 \
    FETCH_WALLPAPER=/usr/share/fetch/wallpaper.png

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        adwaita-icon-theme \
        ca-certificates \
        curl \
        dbus-x11 \
        fonts-dejavu-core \
        gnome-themes-extra \
        hsetroot \
        librsvg2-bin \
        openssl \
        thunar \
        tigervnc-standalone-server \
        x11-utils \
        x11-xserver-utils \
        xauth \
        xfce4 \
        xfce4-terminal \
    && rm -f \
        /etc/xdg/autostart/light-locker.desktop \
        /etc/xdg/autostart/xscreensaver.desktop \
        /etc/xdg/autostart/xfce4-screensaver.desktop \
        /etc/xdg/autostart/xfce4-tips.desktop \
        /etc/xdg/autostart/xfce4-tips-autostart.desktop \
    && rm -rf /var/lib/apt/lists/*

# Interactive desktop browser. Hermes stays on the host and docker-execs
# fetch-chrome so headed browser_* / computer_use windows open here.
RUN arch="$(dpkg --print-architecture)" \
    && apt-get update \
    && if [ "$arch" = "amd64" ] \
          && curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
          && apt-get install -y --no-install-recommends /tmp/chrome.deb; then \
         rm -f /tmp/chrome.deb; \
       else \
         apt-get install -y --no-install-recommends epiphany-browser; \
         rm -f /tmp/chrome.deb; \
       fi \
    && rm -rf /var/lib/apt/lists/*

RUN if id ubuntu >/dev/null 2>&1; then \
      usermod -l fetch ubuntu \
      && groupmod -n fetch ubuntu \
      && usermod -d /home/fetch -m fetch; \
    else \
      useradd --create-home --shell /bin/bash --uid 1000 fetch; \
    fi \
    && mkdir -p /usr/share/fetch /usr/share/fetch/skel \
    && (dbus-uuidgen --ensure=/etc/machine-id || true)

COPY branding/wallpaper.png /usr/share/fetch/wallpaper.png
COPY branding/fetch_logo.svg /usr/share/fetch/fetch_logo.svg
COPY xfce-config/ /usr/share/fetch/skel/config/
COPY entrypoint.sh /usr/local/bin/fetch-computer-entrypoint

# Official Fetch F-mark is monochrome black. Recolor to near-white and rasterize
# for the XFCE panel; keep the SVG path geometry unchanged.
RUN sed 's/fill="black"/fill="#f7f7f8"/g' /usr/share/fetch/fetch_logo.svg \
      > /tmp/fetch_logo_white.svg \
    && rsvg-convert -w 256 -h 256 /tmp/fetch_logo_white.svg \
      -o /usr/share/fetch/fetch_logo.png \
    && rm -f /tmp/fetch_logo_white.svg \
    && chmod 0755 /usr/local/bin/fetch-computer-entrypoint \
    && chmod 0644 /usr/share/fetch/wallpaper.png \
      /usr/share/fetch/fetch_logo.svg \
      /usr/share/fetch/fetch_logo.png \
    && chown -R fetch:fetch /home/fetch /usr/share/fetch \
    && if [ -x /usr/bin/google-chrome ]; then \
         printf '%s\n' \
           '#!/bin/sh' \
           'exec /usr/bin/google-chrome --no-sandbox --disable-dev-shm-usage "$@"' \
           > /usr/local/bin/fetch-chrome; \
       else \
         printf '%s\n' \
           '#!/bin/sh' \
           'exec /usr/bin/epiphany "$@"' \
           > /usr/local/bin/fetch-chrome; \
       fi \
    && chmod 0755 /usr/local/bin/fetch-chrome

USER fetch
WORKDIR /home/fetch
ENTRYPOINT ["/usr/local/bin/fetch-computer-entrypoint"]
