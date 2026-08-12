# TunedUp (MVP scaffold + Phase 2 & 3)

This branch contains an extended MVP scaffold implementing:
- Phase 1: Standard round (previously implemented)
- Phase 2: Bid-A-Note (bidding-as-seconds) UI and flow
- Phase 3: Golden Medley timer flow
- Host answer modal with strict/easy modes and fuzzy matching
- Editable team names in the UI
- Save/load session endpoints (backend writes to backend/sessions.json)

Run the dev frontend as before (cd frontend && npm run dev). The backend now includes endpoints to save and load session state; run (cd backend && npm start) after building the frontend for a production preview.
