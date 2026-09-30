# Autonomous Tools — discover or create missing capabilities

The core decides when to invoke. NOT a fixed pipeline.

## When to call (core's judgment)
- Task requires capability NOT in current skills/native tools
- Current tools produce unacceptably low quality (quantified)
- No existing free/keyless solution found after search

## What it does
1. Web search for existing tools (GitHub, npm, PyPI, APIs, HF Spaces)
2. If found: wrap as skill pack (minimal adapter)
3. If not found: generate via opencode worker (spec + tests)
5. Verify: run tests, benchmark quality
6. Register: write to functions/<name>/, update config.yaml skills.external_dirs

## Quality Gates (MUST pass)
- [ ] Impossibility documented (what native tools tried, why failed)
- [ ] Search performed (queries logged, top 5 candidates evaluated)
- [ ] Free/keyless/Open Source only (license check)
- [ ] Generated code passes its own tests + 1 integration test
- [ ] Quality >= threshold (defined per task) or explicit human override

## Safety
- Never installs system packages, never writes outside A:\OpenDeamon
- No secrets in generated code (keys via env only)
- Rollback on failure: delete functions/<name>/, revert config

## Interface (called by core via delegate_task or opencode-worker)

### discover(tool_name: str, requirements: dict) -> dict
Searches web for existing free/keyless solutions.
Returns: {found: bool, candidates: [...], best: {...}, search_log: [...]}

### create(spec: dict) -> dict
Generates tool via opencode worker.
spec: {name, description, interface, requirements, test_cases}
Returns: {success: bool, path: str, test_results: [...]}

### verify(tool_path: str, test_cases: list) -> dict
Runs tests, checks quality threshold.
Returns: {passed: bool, metrics: {}, details: [...]}

### register(tool_path: str) -> dict
Adds to functions/, updates config.yaml.
Returns: {registered: bool, skill_name: str}