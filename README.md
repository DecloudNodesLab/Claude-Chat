# Claude Workspace

A self-hosted web interface for working with the Anthropic Claude API inside a Docker container.

## Description

Claude Workspace is a minimalist, fully functional web interface for Claude that runs in a single Docker container without requiring any additional privileges. The interface provides chat with Claude, file uploads, and a built-in terminal in the browser.

## Features

- **Chat with Claude** — full conversation support with history and multiple chats
- **Claude tools** — Claude can read/write files, browse directories, and run commands in `/workspace`
- **File uploads** — upload files directly from your computer into `/workspace`
- **Built-in terminal** — a full PTY terminal in the browser (xterm.js)
- **Shared shell context** — Claude’s commands are visible in the user’s terminal
- **Bilingual interface** — Russian and English
- **Basic Auth** — access protection with username and password
- **Single container** — does not require Docker Compose and does not require privileged mode

**Container directories:**

| Path | Purpose |
|------|---------|
| `/workspace` | Working directory: user files, scripts, project data |
| `/data` | Application service data: chat history (`/data/chats/*.json`) |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | *(required)* | Anthropic API key |
| `CLAUDE_MODEL` | `claude-opus-4-5` | Claude model |
| `BASIC_AUTH_USERNAME` | `admin` | Login username |
| `BASIC_AUTH_PASSWORD` | `changeme` | Login password |
| `APP_HOST` | `0.0.0.0` | Server address |
| `APP_PORT` | `8000` | Server port |
| `DEFAULT_LOCALE` | `en` | Default language (`en` or `ru`) |
| `WORKSPACE_DIR` | `/workspace` | Working directory |
| `DATA_DIR` | `/data` | Application data directory |

## How to Open the Interface

After starting the container, open this URL in your browser: **http://localhost:8000**

Your browser will ask for a username and password (HTTP Basic Auth). Enter the values of `BASIC_AUTH_USERNAME` and `BASIC_AUTH_PASSWORD`.

## Working with the Terminal

The terminal at the bottom of the page is a full PTY bash terminal in the browser:

- Default working directory: `/workspace`
- Supports colors, Tab completion, and command history (`↑`/`↓`)
- Resizes automatically and when dragging the divider
- Reconnects automatically if the connection is interrupted

## Security Recommendations

1. **Change the password** — do not use `changeme` in production
2. **Use HTTPS** — put nginx/caddy in front as a reverse proxy with TLS
3. **Restrict access** — do not expose port 8000 directly to the internet
4. **The API key** never reaches the frontend — backend only
6. **Isolation** — the container does not require and does not use privileged mode

## Troubleshooting

**Terminal does not connect:**
- Check logs: `docker logs claude-workspace`
- Make sure the WebSocket connection is not blocked by a proxy
- For nginx, add the `Upgrade` and `Connection` headers

**Claude does not respond:**
- Check `ANTHROPIC_API_KEY`
- Verify that the `CLAUDE_MODEL` exists and is available
- Open `/health` — it should return `{"status":"ok"}`

**File upload error:**
- Check access permissions for `/workspace` inside the container
- Make sure the volume is mounted with write permissions

**PTY issues on some systems:**
- Make sure the container is not started with `--read-only`
- Check that `/dev/ptmx` is available (standard for Docker)

## Support team

**AKT** `akash1l7hk799vrug3fgkvu4fvww8zlnejrz9ck8mwgy`
**USDT(trc-20)** `TWbMqmDqKPzRjy2JBZbCsunb9krymPBtjZ`
**ETH** `0xD08C659fF3DD69DED970dF080D6427c057438868`
**BTC** `bc1q0xk5rh5pasz9ecxpvwdr8empgvg54hv5su8533`
