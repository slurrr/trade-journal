# 0028 – Tags and Strategy Taxonomy

## Decision

Use a single `tags` table with a `type` field to represent strategies and other tag categories (strategy, setup, mistake, context, note, etc.). Trades map to tags via `trade_tags`.

## Rationale

A unified tag system keeps the schema simple while allowing a predefined, updatable list of strategy tags and additional taxonomies without separate tables. The `type` field supports filtering and grouping in analytics while avoiding premature abstraction.

## Status

Accepted – 2026-01-21
