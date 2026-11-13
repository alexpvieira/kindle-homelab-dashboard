FROM python:3.12-slim-bookworm
# Inter for text, DejaVu as fallback; util-linux (nsenter) is in the base image, for the reboot_host action.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-inter fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir --root-user-action=ignore pillow==11.3.0
# Icons: Font Awesome Free 6 solid (SIL OFL 1.1)
ADD https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.5.2/webfonts/fa-solid-900.ttf /fonts/fa-solid-900.ttf
COPY app.py /app.py
CMD ["python", "-u", "/app.py"]
