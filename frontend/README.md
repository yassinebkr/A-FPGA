# FPGA/ASIC Agent Frontend

React/Vite interface for the autonomous FPGA/ASIC design agent. The model selector is populated by the backend `GET /api/models` registry; model choices are not hard-coded in this frontend.

The current backend registry exposes:

- Kimi K3 through the direct Kimi API (`KIMI_API_KEY`).
- Kimi K2.7 Code through OpenRouter (`OPENROUTER_API_KEY`).
- Kimi K3 through OpenRouter (`OPENROUTER_API_KEY`).

Set at least one provider key in `backend/.env`. Models without a configured key remain visible but unavailable in the selector. See the root [`README.md`](../README.md) for complete setup and engineering workflow documentation.

## Development

```bash
npm install
npm run dev
```

The frontend expects the backend at port 8000 on the same host.
