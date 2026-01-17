# 0016 - Web UI architecture and tech

## Decision

Implement the local web UI as a FastAPI app that renders server-side Jinja templates, with Chart.js for simple client-side charts and no frontend framework.

## Rationale

This keeps the UI minimal and local-first, avoids new analytics code or a SPA build pipeline, and provides straightforward charting while reusing the existing Python reconstruction/metrics logic.

## Status

Accepted
