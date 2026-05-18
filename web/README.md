# AlphaGrid dashboard (Next.js)

Dark Bloomberg-terminal-style frontend for the AlphaGrid API.

## Local dev

```bash
cd web
npm install
npm run dev               # http://localhost:3000

# In a separate terminal — start the API
uvicorn api.main:app --reload --port 8000
```

By default the frontend reads from `http://localhost:8000` (REST) and
`ws://localhost:8000/live` (WebSocket). Override via `.env.local`:

```
NEXT_PUBLIC_API_URL=https://your-azure-vm.example.com
NEXT_PUBLIC_WS_URL=wss://your-azure-vm.example.com
```

## Deploy to Vercel

```bash
cd web
vercel
```

Vercel auto-deploys every push to GitHub when connected. Set the two
`NEXT_PUBLIC_*` env vars in the Vercel project settings pointing to your
Azure VM's public IP / domain.

## Build for self-host (Azure VM, Nginx static serve)

```bash
cd web
npm install
npm run build              # outputs .next/standalone
node .next/standalone/server.js     # listens on $PORT (default 3000)
```

## Theme

| Token | Hex | Usage |
|---|---|---|
| `bg`     | `#0a0a0a` | page background |
| `card`   | `#111111` | panels |
| `border` | `#222222` | divisions |
| `text`   | `#e5e5e5` | primary text |
| `muted`  | `#666666` | labels |
| `pos`    | `#00c48c` | positive P&L |
| `neg`    | `#ff4d4f` | negative P&L |
| `accent` | `#3b82f6` | agent-fired pulse |
| `danger` | `#ef4444` | kill-switch banner |
| `warn`   | `#fbbf24` | paper mode, in-flight |

All numbers use `font-mono` + `tabular-nums` (Tailwind `num` class).
