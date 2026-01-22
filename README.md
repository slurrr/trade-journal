# ApeX Trade Journal

A private, local-first trade journal based on the popular Tradezella product that automatically imports data from
ApeX Omni and provides analytics similar to Tradezella.

This project is optimized for:

- Personal use
- Transparency
- Extendability
- Low operational complexity

## What This Is

- An automated trade journal
- A post-trade analytics tool
- A local dashboard for understanding trading behavior

## What This Is Not

- A SaaS product
- A social platform
- A polished clone with full feature parity
- A locked-down or over-specified system

## Core Capabilities (current / intended)

- Automatic import from ApeX Omni
- Trade reconstruction from source data
- Per-trade and aggregate analytics
- Metrics such as MAE, MFE, ETD, expectancy
- Browser-based local dashboard

## Project Status

Active development.
Structure and features may evolve as the system grows.

## Repository Structure (high-level)

- `reference/` — Non-binding research and background material
- `src/` — Application code (subject to change)
- `data/` — temporary local storage
- Root docs — Guardrails and contributor context

## Contributing

This is primarily a personal project.
If you are contributing:

- Keep changes additive where possible
- Avoid large refactors without discussion
- Favor clarity over abstraction

## Design Principles

- Ground truth first
- Explainability over optimization
- Guardrails, not handcuffs

## Environment Notes

- `.env` is for **secrets** only (API keys and passphrases). See `.env.example`.
- General app settings live in `config/app.toml` (see `config/app.toml.example`).
- Optional multi-account config lives in `config/accounts.toml` (see `config/accounts.toml.example`).
