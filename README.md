# TunedUp (MVP scaffold)

This repository contains an MVP scaffold for the TunedUp Name That Tune host app using YouTube IFrame playback.

Structure
- frontend/ — React + Vite app (host UI, YouTube player)
- backend/ — Minimal Express server to serve sample songs and production build

Quickstart (development)
1. Install dependencies (root uses Yarn/npm workspaces)
   - yarn install

2. Start the frontend dev server
   - cd frontend
   - npm run dev

Open the dev URL printed by Vite (usually http://localhost:5173).

Production preview (optional)
1. Build frontend: cd frontend && npm run build
2. Start backend: cd backend && npm start
3. Open http://localhost:3000 (backend serves built frontend)

Notes
- This is an MVP scaffold: Phase 1 (standard round) buzzer buttons and YouTube snippet playback are implemented in the host screen.
- Sample songs are in frontend/public/sample_songs.json — replace with your own YouTube IDs and start/end seconds.

Next steps I can take (I will proceed if you confirm):
- Add Phase 2 Bid-A-Note logic and UI
- Add Phase 3 Golden Medley timer and flow
- Implement fuzzy answer matching and host accept/reject modal
- Add simple persistence (save/load session JSON via backend)
