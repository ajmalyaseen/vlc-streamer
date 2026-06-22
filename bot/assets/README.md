# Bot assets

Drop the `/start` banner image here as **`start.jpg`** (or `start.png`).

The `/start` command uses the image in this order:
1. `START_IMAGE` env var, if set — a public image URL or a path inside the container.
2. `bot/assets/start.jpg`
3. `bot/assets/start.png`
4. If none are found, `/start` falls back to a text-only welcome.

This folder is copied into the Docker image (`COPY bot ./bot`), so a file placed
here ships with the build automatically — no env var needed.
