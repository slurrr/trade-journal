# AGENTS.md

## Project Intent

This repository is a private, single-user trade journal built on ApeX Omni data.
It aims to resemble Tradezella in functionality while remaining local, free, and flexible.

This is not a throwaway build, but it is intentionally under-constrained.

## Hard Invariants (do not violate)

- Ground truth comes from ApeX Omni data (fills, trades, or positions).
- Data ingestion is automated (no manual trade entry).
- Single-user, local-first (no auth, no cloud assumptions).
- All derived analytics must be explainable back to source data.

## Non-Goals (do not design for)

- Multi-user support
- SaaS features
- Social or sharing features
- Over-optimization for scale
- Premature abstractions

## Authority Hierarchy

1. Running code
2. Explicit invariants in this file
3. User instructions in the current task
4. Reference documents (non-binding)

Reference documents inform judgment but do not dictate design.

## Reference Usage Rules

- Reference documents live in `/reference/`
- Treat them as background, not specifications
- Do not mirror their structure or phased plans
- Pull only relevant sections when needed

## Design Philosophy

- Prefer clarity over cleverness
- Prefer additive changes over refactors
- Avoid speculative generalization
- Defer constraints unless correctness demands them

## Decision Records (Required When Semantics Are Chosen)

Some implementation choices materially affect analytics semantics
(e.g., what data is authoritative, how trades are reconstructed,
how MAE/MFE/ETD are computed).

When you make such a choice:

- Document it as a decision record under `/docs/decisions/`
- Keep the record short and explicit
- Prefer recording _after_ implementation if reality forced the decision

Do NOT create decision records for:

- File structure
- Naming
- Refactors
- Convenience choices

If unsure whether something is a decision:
Ask, or record it anyway (cheap to do).

## Working Style

- Propose before implementing when scope is non-trivial
- Make tradeoffs explicit
- If unsure, surface uncertainty instead of guessing

## What to Optimize For

- Correctness of trade reconstruction
- Transparency of metrics (e.g., MAE, MFE, ETD)
- Ease of future extension
- Developer comprehension

## What to Avoid

- Silent assumptions
- Hidden coupling
- Implicit state
- “This will be useful later” abstractions

## Development Environment (Invariant)

- A local virtual environment at `.venv/` is mandatory
- All tooling must run inside the active `.venv`
- Install the package in editable mode before development:
  - `pip install -e .`

Do not modify `sys.path` or bypass the environment.

## Tooling & Style (Binding)

All Python code MUST adhere to:

- ruff (using repo ruff.toml)
- pyright (default settings unless overridden)

Ruff + Pyright are hygiene only; no refactors requested
