# Mixtape Share 📼

Digital mixtape sharing platform — create and share playlists with a unique link, passcode protection, and ZIP download.

## Features

- **Drag-and-drop upload** — audio files, folders, or ZIP archives
- **Auto cover detection** — JPG/PNG in uploads become the cover art
- **Dynamic theming** — player accent color extracted from cover art
- **Passcode protection** — optional per-mixtape
- **HTML5 player** — auto-advancing playlist with keyboard shortcuts
- **ZIP download** — recipients can download the full mixtape
- **QR codes** — one-click share link and QR generation

## Tech Stack

- **Backend:** FastAPI + SQLite
- **Frontend:** Jinja2 templates + vanilla JS
- **Deployment:** Docker (port 8081 → Apache reverse proxy)

## Development

```bash
git clone https://github.com/janhellion/mixtape-share
cd mixtape-share
docker compose up -d --build
```

## Configuration

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `DATABASE_URL` | `sqlite:////storage/app.db` | SQLite path |
| `MEDIA_DIR` | `/storage/music` | Audio storage |
| `COVERS_DIR` | `/storage/covers` | Cover images |
| `BASE_URL` | `https://mixtape.janhellion.com` | Public URL |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard UI |
| POST | `/api/upload` | Create mixtape (multipart) |
| GET | `/api/shares` | List all mixtapes |
| DELETE | `/api/share/{id}` | Delete mixtape |
| GET | `/share/{id}` | Player UI |
| GET | `/api/stream/{id}/{file}` | Stream audio |
| GET | `/api/download/{id}` | Download ZIP |
