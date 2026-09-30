---
name: quota-display
description: "Display OpenRouter quota using the quota.bat batch file."
version: 1.0.0
author: Hermes Agent
platforms: [windows]
metadata:
  hermes:
    tags: [quota, openrouter, batch, display]
---

# Quota Display

This skill explains how to view your OpenRouter API quota using the provided batch file.

## Location of Batch File

The batch file is located at:
`A:/OpenDeamon/_setup_tmp/quota.bat`

## How to Run

1. Open Hermes terminal (or any terminal).
2. Run the batch file:
   ```
   quota.bat
   ```
   (You may need to provide the full path if not in PATH: `A:/OpenDeamon/_setup_tmp/quota.bat`)

The batch file executes the Python script `quota_check.py`, which reads your OpenRouter API key from `A:/AI/daemon/config/secrets.local.toml` and calls the OpenRouter auth endpoint to retrieve quota information.

## Output

The script prints lines such as:

- LABEL: <label>
- LIMIT: <limit>
- USAGE: <usage>
- LIMIT_RESET: <timestamp>
- FREE_TIER: <true/false>
- ... plus other fields like rate_limit, free_model_daily_requests, etc.

These values show your current quota limits and usage.

## Notes

- The batch file uses `@echo off` to suppress command echoing.
- Ensure your OpenRouter API key is correctly set in the secrets file.
- The script does not print your API key for security.